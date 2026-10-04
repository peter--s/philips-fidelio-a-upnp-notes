"""Verify remaining claims of the Fidelio A9 page on an A3/A5:
volume via UPnP, GENA SEQ / event order, SetAVTransportURI without Stop, online streams."""
import asyncio, socket, sys, time
from pathlib import Path
from types import SimpleNamespace
import aiohttp
sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, RC, Fmt, Renderer, Server, didl, log, now, parse_notify, Timeline  # noqa

from fidelio_discover import resolve  # noqa: E402
IP = resolve(sys.argv[1] if len(sys.argv) > 1 else None)
ONLINE = ["http://icecast.omroep.nl/radio1-bb-mp3",
          "http://st01.sslstream.dlf.de/dlf/01/128/mp3/stream.mp3",
          "https://icecast.omroep.nl/radio1-bb-mp3"]


class SeqServer(Server):
    async def handle_notify(self, reader, writer, hdrs):
        self.last_seq = hdrs.get("seq")
        await super().handle_notify(reader, writer, hdrs)


async def vendor(s, path):
    async with s.get(f"http://{IP}:8889/{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
        return " ".join((await r.text()).split())


async def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((IP, 1900)); local = so.getsockname()[0]
    args = SimpleNamespace(mode="chunked", pace="realtime", prebuffer=2.0, speed=1.0, header_delay=0.0,
                           audio_seconds=120, flags="01700000000000000000000000000000")
    server = SeqServer(args)
    fmt = Fmt("mp3:44100"); data = fmt.make(120, Path(__file__).parent / "media", "beep")
    fmt.size, fmt.duration_s = len(data), 120
    server.set_media(fmt, data, 0.0)
    events = []

    def on_notify(body):
        tl = Timeline()
        parse_notify(body, tl, False)
        st = tl.state
        events.append((round(now(), 2), server.last_seq, st))
        log(f"GENA SEQ={server.last_seq} TransportState={st}")
    server.on_notify = on_notify
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=8771)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = Renderer(s, IP, 49153, min_interval=0.2); await r.load()

        async def state():
            return (await r.call(AVT, "GetTransportInfo", InstanceID=0)).get("CurrentTransportState")

        # --- 1. volume via UPnP
        v0 = int((await r.call(RC, "GetVolume", InstanceID=0, Channel="Master"))["CurrentVolume"])
        log(f"[volume] UPnP GetVolume={v0}, vendor ELAPSE: {await vendor(s, 'ELAPSE')}")
        await r.call(RC, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=v0 + 2)
        v1 = (await r.call(RC, "GetVolume", InstanceID=0, Channel="Master"))["CurrentVolume"]
        log(f"[volume] after SetVolume({v0 + 2}): GetVolume={v1}")
        await r.call(RC, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=v0)
        log(f"[volume] restored: GetVolume={(await r.call(RC, 'GetVolume', InstanceID=0, Channel='Master'))['CurrentVolume']}")

        # --- 2. GENA: one subscription, SEQ numbering, event order around Play
        sid = await r.subscribe(f"http://{local}:8771/gena")
        log(f"[gena] single SUBSCRIBE -> SID {sid}")
        await r.call(AVT, "Stop", InstanceID=0)
        urlA = f"http://{local}:8771/stream/a.mp3"
        await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=urlA, CurrentURIMetaData=didl(urlA, fmt))
        await r.call(AVT, "Play", timeout=60, InstanceID=0, Speed=1)
        await asyncio.sleep(8)
        log(f"[gena] events so far: {events}")

        # --- 3. SetAVTransportURI while PLAYING, without Stop (Twonky NEEDS_STOP_4_AV)
        urlB = f"http://{local}:8771/stream/b.mp3"
        t = now()
        try:
            await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=urlB, CurrentURIMetaData=didl(urlB, fmt))
            log(f"[nostop] SetAVTransportURI(B) while playing: OK after {now() - t:.2f}s, state={await state()}")
            mi = await r.call(AVT, "GetMediaInfo", InstanceID=0)
            log(f"[nostop] CurrentURI now: {mi.get('CurrentURI')}")
            await r.call(AVT, "Play", timeout=60, InstanceID=0, Speed=1)
            await asyncio.sleep(5)
            pi = await r.call(AVT, "GetPositionInfo", InstanceID=0)
            log(f"[nostop] after Play: state={await state()}, TrackURI={pi.get('TrackURI')}, pos={pi.get('RelTime')}")
        except Exception as err:  # noqa: BLE001
            log(f"[nostop] FAILED after {now() - t:.2f}s: {err}")
        await r.call(AVT, "Stop", InstanceID=0)
        await asyncio.sleep(3)
        log(f"[gena] all events: {events}")
        seqs = [int(e[1]) for e in events if e[1] and e[1].isdigit()]
        log(f"[gena] SEQ values: {seqs}  duplicates: {sorted({x for x in seqs if seqs.count(x) > 1})}")
        await r.unsubscribe(sid)

        # --- 4. online streams directly
        for url in ONLINE:
            log(f"===== [online] {url}")
            f2 = Fmt("mp3:44100:16:cls=object.item.audioItem.audioBroadcast")
            await r.call(AVT, "Stop", InstanceID=0)
            t = now()
            try:
                await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, f2))
                await r.call(AVT, "Play", timeout=45, InstanceID=0, Speed=1)
                log(f"[online] Play OK after {now() - t:.2f}s")
                await asyncio.sleep(6)
                pi = await r.call(AVT, "GetPositionInfo", InstanceID=0)
                log(f"[online] state={await state()} pos={pi.get('RelTime')}")
            except Exception as err:  # noqa: BLE001
                log(f"[online] FAILED after {now() - t:.2f}s: {err}; state={await state()}")
            await r.call(AVT, "Stop", InstanceID=0)
            await asyncio.sleep(3)
    srv.close()

asyncio.run(main())
