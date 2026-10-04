#!/usr/bin/env python3
"""Try to reproduce the renderer lock-up in controlled, escalating stages.

Each stage runs short play cycles (a few seconds of MP3) while a poller hits
the UPnP interface at the stage's pace/concurrency. A watchdog declares a hang
when UPnP stays silent for --hang-after seconds outside an expected Play block.
On a hang, load stops immediately, the device is diagnosed (ping, TCP ports,
description XML, SOAP, vendor /HOMESTATUS, SSDP) and a recovery ladder is
walked from gentle to less gentle, stopping at the first step that works.

  python hang_hunt.py --radio Kitchen
  python hang_hunt.py --ip 192.0.2.10 --stages 6,7,8
"""
import argparse
import asyncio
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
import dlna_probe as dp  # noqa: E402
from dlna_probe import AVT, Fmt, Renderer, Server, didl, log, now  # noqa: E402


class TrackingServer(Server):
    """Probe server that remembers open connections so they can be dropped."""

    def __init__(self, args):
        super().__init__(args)
        self.conns = set()

    async def handle(self, reader, writer):
        self.conns.add(writer)
        try:
            await super().handle(reader, writer)
        finally:
            self.conns.discard(writer)

    def drop_all(self):
        n = len(self.conns)
        for w in list(self.conns):
            try:
                w.transport.abort()
            except Exception:
                pass
        return n


class StressRenderer(Renderer):
    """Renderer client whose pacing, concurrency and timeout floor can be lowered."""

    def __init__(self, session, ip, port):
        super().__init__(session, ip, port, min_interval=1.0, hung_after=1e9)
        self.sem = asyncio.Semaphore(1)
        self.spacing = asyncio.Lock()
        self.last_send = 0.0
        self.stats = []  # (ok, latency, error class)

    def configure(self, interval, concurrency, min_timeout=10.0):
        self.min_interval = interval
        self.sem = asyncio.Semaphore(concurrency)
        self.MIN_TIMEOUT = min_timeout
        self.stats = []

    async def _request(self, method, url, timeout, **kw):
        timeout = max(timeout, self.MIN_TIMEOUT)
        async with self.sem:
            async with self.spacing:
                wait = self.last_send + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.last_send = time.monotonic()
            self.requests += 1
            t = time.monotonic()
            try:
                async with self.s.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
                    text = await r.text()
                self.last_ok = time.monotonic()
                self.stats.append((True, self.last_ok - t, ""))
                return r.status, r.headers, text
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self.stats.append((False, time.monotonic() - t, err.__class__.__name__))
                raise


# ------------------------------------------------------------------ diagnosis

async def tcp_open(ip, port, timeout=5.0):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return "open"
    except asyncio.TimeoutError:
        return "timeout"
    except OSError as err:
        return f"refused ({err.errno})"


LOCAL_IP = "0.0.0.0"


def ssdp_seen(ip, wait=3.0):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOCAL_IP))
        s.bind((LOCAL_IP, 0))
        s.settimeout(0.5)
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + wait
        while time.time() < end:
            try:
                _, a = s.recvfrom(4096)
                if a[0] == ip:
                    return True
            except socket.timeout:
                pass
    return False


async def diagnose(ip, upnp_port, session):
    d = {}
    ping = subprocess.run(["ping", "-c", "1", "-t", "2", ip], capture_output=True, text=True)
    d["ping"] = "ok" if ping.returncode == 0 else "no reply"
    d["tcp_upnp"] = await tcp_open(ip, upnp_port)
    d["tcp_8889"] = await tcp_open(ip, 8889)
    for name, url in (("description", f"http://{ip}:{upnp_port}/nmrDescription.xml"),
                      ("homestatus", f"http://{ip}:8889/HOMESTATUS")):
        t = time.monotonic()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = " ".join((await r.text()).split())
            d[name] = f"{r.status} in {time.monotonic() - t:.2f}s" + (f" {body}" if name == "homestatus" else "")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            d[name] = f"FAIL {err.__class__.__name__} after {time.monotonic() - t:.1f}s"
    d["ssdp"] = "answers" if await asyncio.to_thread(ssdp_seen, ip) else "silent"
    return d


async def soap_alive(ip, upnp_port, session, timeout=10.0):
    env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:GetTransportInfo xmlns:u="{AVT}"><InstanceID>0</InstanceID></u:GetTransportInfo></s:Body></s:Envelope>')
    try:
        async with session.post(f"http://{ip}:{upnp_port}/upnp/control/AVTransport1", data=env,
                                headers={"Content-Type": 'text/xml; charset="utf-8"',
                                         "SOAPACTION": f'"{AVT}#GetTransportInfo"'},
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            text = await r.text()
        state = text.split("<CurrentTransportState>")[1].split("<")[0] if "<CurrentTransportState>" in text else "?"
        return True, state
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        return False, err.__class__.__name__


# ------------------------------------------------------------------ stress

class Hang(Exception):
    pass


class Watchdog:
    def __init__(self, rdr, hang_after):
        self.rdr = rdr
        self.hang_after = hang_after
        self.expect_block_until = 0.0  # monotonic; UPnP silence expected before this

    def check(self):
        silent = time.monotonic() - self.rdr.last_ok
        if silent > self.hang_after and time.monotonic() > self.expect_block_until:
            raise Hang(f"UPnP silent for {silent:.0f}s")


async def poller(rdr, wd, stop, concurrency):
    """Keep up to 2x concurrency polls queued; the renderer client paces them."""
    inflight = set()
    actions = ("GetTransportInfo", "GetPositionInfo", "GetMediaInfo")
    i = 0
    while not stop.is_set():
        wd.check()
        if len(inflight) < 2 * concurrency:
            action = actions[i % len(actions)]
            i += 1
            task = asyncio.create_task(rdr.call(AVT, action, InstanceID=0))
            task.add_done_callback(lambda t: (inflight.discard(t), t.exception() if not t.cancelled() else None))
            inflight.add(task)
        await asyncio.sleep(0.005)
    for t in inflight:
        t.cancel()


async def play_cycle(rdr, server, fmt, local_ip, port, label, wd, clip_s, play_timeout):
    url = f"http://{local_ip}:{port}/stream/{label}.{fmt.ext}"
    for action, kw in (("Stop", {}),
                       ("SetAVTransportURI", dict(CurrentURI=url, CurrentURIMetaData=didl(url, fmt))),
                       ("Play", dict(Speed=1))):
        t = time.monotonic()
        if action == "Play":
            wd.expect_block_until = time.monotonic() + play_timeout + server.delay + 10
        try:
            await rdr.call(AVT, action, timeout=play_timeout if action == "Play" else 10.0, InstanceID=0, **kw)
        except Exception as err:
            log(f"  {label} {action} failed after {time.monotonic() - t:.1f}s: {err.__class__.__name__} {err}")
    wd.expect_block_until = 0.0
    await asyncio.sleep(clip_s + 1.0)


def summarize(stats):
    if not stats:
        return "no requests"
    ok = [lat for good, lat, _ in stats if good]
    errs = {}
    for good, _, e in stats:
        if not good:
            errs[e] = errs.get(e, 0) + 1
    lat = (f"median {statistics.median(ok) * 1000:.0f} ms, max {max(ok) * 1000:.0f} ms" if ok else "no successes")
    return f"{len(stats)} requests, {len(ok)} ok ({lat}), errors {errs or 'none'}"


async def tarpit(reader, writer):
    """Accept a GENA NOTIFY and never answer it (a dead or hung subscriber)."""
    await asyncio.sleep(3600)


STAGES = [
    # name, interval between requests, concurrency, special
    ("gate 1.0 s", 1.0, 1, None),
    ("gate 0.5 s", 0.5, 1, None),
    ("gate 0.25 s", 0.25, 1, None),
    ("gate 0.1 s", 0.1, 1, None),
    ("gate 0 s (back to back)", 0.0, 1, None),
    ("2 parallel", 0.0, 2, None),
    ("4 parallel", 0.0, 4, None),
    ("8 parallel", 0.0, 8, None),
    ("dead GENA subscriber", 1.0, 1, "dead_gena"),
    ("MA-like abandoned Play (5 s timeout, instant retry)", 1.0, 1, "abandon"),
    ("wake from standby + MA start with early retry", 0.0, 1, "wake"),
    ("wake from standby + MA start + parallel 5 s polls", 0.0, 4, "wake_storm"),
]


async def vendor(session, ip, path):
    async with session.get(f"http://{ip}:8889/{path}", timeout=aiohttp.ClientTimeout(total=15)) as r:
        return " ".join((await r.text()).split())


async def ensure_standby(session, ip, sleep_s):
    status = await vendor(session, ip, "HOMESTATUS")
    if "'value':0" in status.replace(" ", ""):
        log(f"  CTRL$STANDBY -> {await vendor(session, ip, 'CTRL$STANDBY')}")
        await asyncio.sleep(3)
        status = await vendor(session, ip, "HOMESTATUS")
    log(f"  standby confirmed: {status}; sleeping {sleep_s:.0f}s")
    await asyncio.sleep(sleep_s)
    return status


async def ma_start(rdr, server, fmt, local_ip, port, label, local_log, retries=3):
    """Music Assistant's DLNA play_media: Stop -> SetAVTransportURI -> Play, 5 s
    timeout each (library default); on error: force-poll, resubscribe, retry at once."""
    for attempt in range(1 + retries):
        url = f"http://{local_ip}:{port}/stream/{label}a{attempt}.{fmt.ext}"
        t0 = time.monotonic()
        failed = None
        for action, kw in (("Stop", {}),
                           ("SetAVTransportURI", dict(CurrentURI=url, CurrentURIMetaData=didl(url, fmt))),
                           ("Play", dict(Speed=1))):
            t = time.monotonic()
            try:
                await rdr.call(AVT, action, timeout=5.0, InstanceID=0, **kw)
                local_log(f"  {label} try{attempt} {action} ok after {time.monotonic() - t:.2f}s")
            except Exception as err:
                failed = f"{action} {err.__class__.__name__}"
                local_log(f"  {label} try{attempt} {action} FAILED after {time.monotonic() - t:.2f}s: {err.__class__.__name__}")
                break
        if not failed:
            return attempt, time.monotonic() - t0
        # MA: catch_request_errors sets force_poll -> immediate poll + (re)subscribe
        for action in ("GetTransportInfo",):
            try:
                await rdr.call(AVT, action, timeout=5.0, InstanceID=0)
            except Exception as err:
                local_log(f"  {label} force-poll {action} failed: {err.__class__.__name__}")
        try:
            await rdr._request("SUBSCRIBE", rdr.urls[AVT][1], 5.0, headers={
                "CALLBACK": f"<http://{local_ip}:{port}/gena>", "NT": "upnp:event", "TIMEOUT": "Second-300"})
        except Exception as err:
            local_log(f"  {label} resubscribe failed: {err.__class__.__name__}")
    return None, None


async def run_wake_stage(args, no, storm, rdr, server, fmt, local_ip, wd, diag_session):
    sargs = server.args
    sargs.mode, sargs.pace, sargs.prebuffer, sargs.speed = "chunked", "realtime", 3.0, 1.03  # MA flow stream
    server.set_media(fmt, server.data, 0.0)
    for c in range(args.wake_cycles):
        if args.standby_wait >= 0:
            await ensure_standby(diag_session, args.ip, args.standby_wait)
        else:
            log(f"  no standby step; vendor status {await vendor(diag_session, args.ip, 'HOMESTATUS')}")
        stop = asyncio.Event()
        poll_task = asyncio.create_task(poller(rdr, wd, stop, 4)) if storm else None
        wd.expect_block_until = time.monotonic() + 60
        t = time.monotonic()
        attempt, took = await ma_start(rdr, server, fmt, local_ip, args.port, f"s{no}w{c}", log)
        wake_get = (server.first_get_at - (t - dp.T0)) if server.first_get_at else None
        log(f"  wake cycle {c}: " + (f"started on attempt {attempt} after {took:.1f}s" if attempt is not None
                                     else "NOT started after all retries")
            + (f"; first GET {wake_get:.1f}s after start" if wake_get is not None else "; no GET"))
        await asyncio.sleep(args.clip + 4)
        stop.set()
        if poll_task:
            await asyncio.gather(poll_task, return_exceptions=True)
        wd.expect_block_until = 0.0
        ok, state = await soap_alive(args.ip, args.upnp_port, diag_session, timeout=args.hang_after)
        log(f"  after wake cycle {c}: UPnP {'alive (' + state + ')' if ok else 'DEAD'}; "
            f"{await diagnose(args.ip, args.upnp_port, diag_session)}")
        if not ok:
            raise Hang(f"no answer after wake cycle {c}")
        server.first_get_at = None


async def recover(args, rdr, server, session, sids):
    ip, up = args.ip, args.upnp_port

    async def alive():
        ok, state = await soap_alive(ip, up, session)
        return ok, state

    async def standby_cycle():
        d = await diagnose(ip, up, session)
        log(f"    before standby toggle: {d}")
        base = f"http://{ip}:8889/"
        async with session.get(base + "HOMESTATUS", timeout=aiohttp.ClientTimeout(total=10)) as r:
            status = await r.text()
        if "'value':0" in status.replace(" ", ""):  # on -> standby -> on
            for _ in range(2):
                async with session.get(base + "CTRL$STANDBY", timeout=aiohttp.ClientTimeout(total=15)) as r:
                    log(f"    CTRL$STANDBY -> {' '.join((await r.text()).split())}")
                await asyncio.sleep(15)
        else:  # already in standby -> on
            async with session.get(base + "CTRL$STANDBY", timeout=aiohttp.ClientTimeout(total=15)) as r:
                log(f"    CTRL$STANDBY -> {' '.join((await r.text()).split())}")
            await asyncio.sleep(15)

    async def upnp_stop():
        await soap_call(session, ip, up, "Stop")

    async def unsubscribe():
        for sid in list(sids):
            try:
                async with session.request("UNSUBSCRIBE", f"http://{ip}:{up}/upnp/event/AVTransport1",
                                           headers={"SID": sid}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    log(f"    UNSUBSCRIBE {sid} -> {r.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                log(f"    UNSUBSCRIBE {sid} -> {err.__class__.__name__}")

    async def source_mp3link():
        async with session.get(f"http://{ip}:8889/mp3link", timeout=aiohttp.ClientTimeout(total=15)) as r:
            log(f"    /mp3link -> HTTP {r.status}")
        await asyncio.sleep(10)

    ladder = [
        ("wait 30 s", lambda: asyncio.sleep(30)),
        ("drop our open HTTP/stream connections", lambda: asyncio.sleep(0, server.drop_all())),
        ("UPnP Stop", upnp_stop),
        ("unsubscribe our GENA subscriptions", unsubscribe),
        ("vendor source switch to MP3-Link via :8889", source_mp3link),
        ("vendor standby off/on via :8889", standby_cycle),
        ("wait 120 s", lambda: asyncio.sleep(120)),
    ]
    for name, step in ladder:
        log(f"  RECOVERY step: {name}")
        try:
            await step()
        except Exception as err:
            log(f"    step error: {err.__class__.__name__} {err}")
        await asyncio.sleep(5)
        ok, state = await alive()
        d = await diagnose(ip, up, session)
        log(f"    UPnP {'ALIVE (' + state + ')' if ok else 'still dead (' + state + ')'}; {d}")
        if ok:
            return name
    return None


async def soap_call(session, ip, up, action):
    env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{AVT}"><InstanceID>0</InstanceID></u:{action}></s:Body></s:Envelope>')
    async with session.post(f"http://{ip}:{up}/upnp/control/AVTransport1", data=env,
                            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{AVT}#{action}"'},
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
        log(f"    {action} -> HTTP {r.status}")


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", help="renderer address (overrides --radio and discovery)")
    p.add_argument("--radio", help="renderer name; resolved via FIDELIO_RADIOS, radios.conf or discovery")
    p.add_argument("--upnp-port", type=int, default=49153)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--tarpit-port", type=int, default=8767)
    p.add_argument("--stages", help="comma-separated stage numbers (1-based), default all")
    p.add_argument("--cycles", type=int, default=5, help="play cycles per stage")
    p.add_argument("--clip", type=int, default=3, help="clip length in seconds (<=5)")
    p.add_argument("--hang-after", type=float, default=40.0, help="UPnP silence that counts as a hang")
    p.add_argument("--continue-after-recovery", action="store_true")
    p.add_argument("--wake-cycles", type=int, default=3, help="standby/wake cycles per wake stage")
    p.add_argument("--standby-wait", type=float, default=60.0, help="seconds in standby before each wake")
    args = p.parse_args()
    from fidelio_discover import resolve
    args.ip = resolve(args.radio, args.ip)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((args.ip, 1900))
        local_ip = s.getsockname()[0]
    global LOCAL_IP
    LOCAL_IP = local_ip

    sargs = SimpleNamespace(mode="length", pace="burst", prebuffer=2.0, speed=1.0, header_delay=0.0,
                            audio_seconds=args.clip, flags="01700000000000000000000000000000")
    server = TrackingServer(sargs)
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=args.port)
    pit = await asyncio.start_server(tarpit, host="0.0.0.0", port=args.tarpit_port)
    fmt = Fmt("mp3:44100")
    data = fmt.make(args.clip, Path(__file__).parent / "media", "beep")
    selected = [int(x) for x in args.stages.split(",")] if args.stages else list(range(1, len(STAGES) + 1))
    sids = set()
    results = []

    diag_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True, limit=0)) as session:
        rdr = StressRenderer(session, args.ip, args.upnp_port)
        await rdr.load()
        log(f"baseline diagnosis: {await diagnose(args.ip, args.upnp_port, diag_session)}")
        wd = Watchdog(rdr, args.hang_after)
        n = 0
        try:
            for no in selected:
                name, interval, conc, special = STAGES[no - 1]
                play_timeout, delay, min_timeout = 30.0, 0.0, 10.0
                if special == "abandon":
                    play_timeout, delay, min_timeout = 5.0, 8.0, 5.0
                server.set_media(fmt, data, delay)
                sargs.mode, sargs.pace = ("chunked", "realtime") if special == "abandon" else ("length", "burst")
                rdr.configure(interval, conc, min_timeout)
                rdr.last_ok = time.monotonic()
                log(f"===== stage {no}: {name}")
                if special == "dead_gena":
                    for _ in range(2):  # "SUBSCRIBE_TWICE" quirk: two dead subscribers
                        _, h, _ = await rdr._request("SUBSCRIBE", rdr.urls[AVT][1], 10.0, headers={
                            "CALLBACK": f"<http://{local_ip}:{args.tarpit_port}/dead>", "NT": "upnp:event",
                            "TIMEOUT": "Second-1800"})
                        sids.add(h.get("SID"))
                        log(f"  dead subscriber registered: {h.get('SID')}")
                if special in ("wake", "wake_storm"):
                    rdr.configure(0.0 if special == "wake" else 0.5, 4 if special == "wake_storm" else 1, 5.0)
                    server.set_media(fmt, data, 0.0)
                    await run_wake_stage(args, no, special == "wake_storm", rdr, server, fmt, local_ip, wd,
                                         diag_session)
                    summary = summarize(rdr.stats)
                    log(f"----- stage {no} done: {summary}")
                    results.append((no, name, summary, "ok"))
                    continue
                stop = asyncio.Event()
                poll_task = None if special == "abandon" else asyncio.create_task(poller(rdr, wd, stop, conc))
                try:
                    for c in range(args.cycles):
                        n += 1
                        if poll_task and poll_task.done() and poll_task.exception():
                            raise poll_task.exception()
                        await play_cycle(rdr, server, fmt, local_ip, args.port, f"s{no}c{c}", wd,
                                         args.clip, play_timeout)
                        wd.check()
                finally:
                    stop.set()
                    if poll_task:
                        await asyncio.gather(poll_task, return_exceptions=True)
                # settle: the device must answer a plain request within hang_after
                ok, state = await soap_alive(args.ip, args.upnp_port, diag_session, timeout=args.hang_after)
                summary = summarize(rdr.stats)
                log(f"----- stage {no} done: {summary}; afterwards UPnP {'alive (' + state + ')' if ok else 'DEAD'}")
                results.append((no, name, summary, "ok" if ok else "HANG"))
                if not ok:
                    raise Hang("no answer after stage")
                await asyncio.sleep(5)
        except Hang as err:
            log(f"!!!!! HANG detected in stage {no}: {err}")
            results.append((no, STAGES[no - 1][0], summarize(rdr.stats), f"HANG: {err}"))
            log(f"diagnosis: {await diagnose(args.ip, args.upnp_port, diag_session)}")
            how = await recover(args, rdr, server, diag_session, sids)
            log(f"RECOVERED by: {how}" if how else "NOT RECOVERED - power cycle needed")
            results.append((no, "recovery", how or "failed", ""))
        finally:
            for sid in sids:
                try:
                    async with diag_session.request("UNSUBSCRIBE", rdr.urls[AVT][1], headers={"SID": sid},
                                                    timeout=aiohttp.ClientTimeout(total=10)):
                        pass
                except Exception:
                    pass
            print("\n=== summary")
            for row in results:
                print(" | ".join(str(x) for x in row))
            log(f"{rdr.requests} UPnP requests in total")
    await diag_session.close()
    srv.close()
    pit.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
