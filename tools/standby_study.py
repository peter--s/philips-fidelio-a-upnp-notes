#!/usr/bin/env python3
"""Measure auto-standby timing and wake-up latency of a Fidelio AW speaker.

Per cycle: play a short clip, Stop, poll /HOMESTATUS until the speaker enters standby by itself
(-> auto-standby time), stay in standby for W minutes, then play again and time the wake-up.
Cycle 0 is a baseline play while the speaker is on.

  python standby_study.py --radio Kitchen --waits 0,5,20,60
"""
import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, Fmt, Renderer, Server, didl, log, now  # noqa: E402
from fidelio_discover import resolve  # noqa: E402


class TimingServer(Server):
    """Probe server that records when the renderer sends its first HEAD and GET."""

    def set_media(self, fmt, data, delay):
        super().set_media(fmt, data, delay)
        self.first_head_at = None

    async def handle_stream(self, writer, method, hdrs, peer, version="", path=""):
        if method == "HEAD" and self.first_head_at is None:
            self.first_head_at = now()
        await super().handle_stream(writer, method, hdrs, peer, version, path)


async def vendor(s, ip, path):
    t = time.monotonic()
    async with s.get(f"http://{ip}:8889/{path}", timeout=aiohttp.ClientTimeout(total=15)) as r:
        body = " ".join((await r.text()).split())
    return body, time.monotonic() - t


def standby(body: str) -> bool:
    return "'value':1" in body.replace(" ", "")


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--radio")
    p.add_argument("--ip")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--waits", default="0,5,20,60", help="minutes to stay in standby before each wake-up")
    p.add_argument("--poll", type=float, default=20.0, help="seconds between standby checks")
    p.add_argument("--max-idle", type=float, default=90.0, help="give up waiting for standby after N minutes")
    p.add_argument("--clip", type=float, default=6.0, help="seconds of playback per cycle")
    p.add_argument("--out", default="standby_study.jsonl")
    a = p.parse_args()
    ip = resolve(a.radio, a.ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((ip, 1900))
        local = so.getsockname()[0]
    sargs = SimpleNamespace(mode="chunked", pace="realtime", prebuffer=2.0, speed=1.0, header_delay=0.0,
                            audio_seconds=120, flags="01700000000000000000000000000000")
    server = TimingServer(sargs)
    fmt = Fmt("mp3:44100")
    data = fmt.make(120, Path(__file__).parent / "media", "beep")
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=a.port)
    out = Path(a.out)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = Renderer(s, ip, 49153, min_interval=0.2)
        await r.load()
        name = r.name

        async def play_and_time(label):
            server.set_media(fmt, data, 0.0)
            hs_before, hs_lat = await vendor(s, ip, "HOMESTATUS")
            t = time.monotonic()
            await r.call(AVT, "GetTransportInfo", InstanceID=0)
            soap_lat = time.monotonic() - t
            await r.call(AVT, "Stop", InstanceID=0)
            url = f"http://{local}:{a.port}/stream/{label}.mp3"
            await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, fmt))
            await r.idle()
            t_play = now()
            res = {"radio": name, "label": label, "standby_before": standby(hs_before),
                   "homestatus_latency_s": round(hs_lat, 3), "soap_latency_s": round(soap_lat, 3)}
            try:
                await r.call(AVT, "Play", timeout=60, InstanceID=0, Speed=1)
                res["play_reply_s"] = round(now() - t_play, 2)
            except Exception as err:  # noqa: BLE001
                res["play_error"] = str(err)
            # PLAYING via polling (the Play reply normally coincides with it)
            for _ in range(40):
                ti = await r.call(AVT, "GetTransportInfo", InstanceID=0)
                if ti.get("CurrentTransportState") == "PLAYING":
                    res["playing_s"] = round(now() - t_play, 2)
                    break
                await asyncio.sleep(0.25)
            res["first_head_s"] = round(server.first_head_at - t_play, 2) if server.first_head_at else None
            res["first_get_s"] = round(server.first_get_at - t_play, 2) if server.first_get_at else None
            await asyncio.sleep(a.clip)
            await r.call(AVT, "Stop", InstanceID=0)
            res["stopped_at"] = time.strftime("%H:%M:%S")
            log(f"[{name}] {label}: {res}")
            return res

        async def wait_for_standby():
            t0 = time.monotonic()
            while time.monotonic() - t0 < a.max_idle * 60:
                await asyncio.sleep(a.poll)
                body, _ = await vendor(s, ip, "HOMESTATUS")
                if standby(body):
                    return round((time.monotonic() - t0) / 60, 2)
            return None

        def record(rec):
            with out.open("a") as f:
                f.write(json.dumps(rec) + "\n")

        # baseline: play while on, then measure the auto-standby time
        res = await play_and_time("baseline_on")
        idle = await wait_for_standby()
        standby_since = time.monotonic()
        log(f"[{name}] auto-standby after {idle} min of idle")
        record(dict(res, idle_to_standby_min=idle))
        for w in [float(x) for x in a.waits.split(",")]:
            if idle is None:
                log(f"[{name}] no standby within {a.max_idle} min - stopping")
                break
            log(f"[{name}] staying in standby for {w} min")
            await asyncio.sleep(w * 60)
            res = await play_and_time(f"wake_after_{w:g}min")
            res["standby_duration_min"] = round((time.monotonic() - standby_since) / 60, 2)
            idle = await wait_for_standby()
            standby_since = time.monotonic()
            log(f"[{name}] auto-standby after {idle} min of idle")
            record(dict(res, idle_to_standby_min=idle))
    srv.close()
    log(f"[{name}] done")


if __name__ == "__main__":
    asyncio.run(main())
