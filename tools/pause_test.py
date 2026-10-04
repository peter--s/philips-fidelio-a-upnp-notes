"""Play -> Pause -> Resume -> Seek -> Stop on one renderer, polling state every 100 ms.

  python pause_test.py <radio name | address> <format spec> [mode] [pace]
  e.g. python pause_test.py Kitchen mp3:44100:16:seek=1 length realtime
"""
import asyncio
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, Fmt, Renderer, Server, didl, log, now  # noqa: E402

from fidelio_discover import resolve  # noqa: E402
IP = resolve(sys.argv[1] if len(sys.argv) > 1 else None)
SPEC = sys.argv[2] if len(sys.argv) > 2 else "mp3:44100"
MODE = sys.argv[3] if len(sys.argv) > 3 else "chunked"
PACE = sys.argv[4] if len(sys.argv) > 4 else "realtime"
SECONDS = 120


async def vendor(s, path):
    async with s.get(f"http://{IP}:8889/{path}", timeout=aiohttp.ClientTimeout(total=10)) as r:
        return " ".join((await r.text()).split())


async def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((IP, 1900))
        local = so.getsockname()[0]
    args = SimpleNamespace(mode=MODE, pace=PACE, prebuffer=2.0, speed=1.0, header_delay=0.0,
                           audio_seconds=SECONDS, flags="01700000000000000000000000000000")
    server = Server(args)
    fmt = Fmt(SPEC)
    data = fmt.make(SECONDS, Path(__file__).parent / "media", "beep")
    fmt.size, fmt.duration_s = len(data), SECONDS
    server.set_media(fmt, data, 0.0)
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=8768)
    log(f"format {fmt} mode={MODE} pace={PACE} size={len(data)}")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = Renderer(s, IP, 49153, min_interval=0.1)
        await r.load()
        last = {}

        async def poll(label, secs):
            end = time.monotonic() + secs
            while time.monotonic() < end:
                ti = await r.call(AVT, "GetTransportInfo", InstanceID=0)
                pi = await r.call(AVT, "GetPositionInfo", InstanceID=0)
                el = await vendor(s, "ELAPSE")
                cur = (ti.get("CurrentTransportState"), pi.get("RelTime"), el)
                if cur[:2] != last.get("v", (None, None))[:2]:
                    log(f"[{label}] state={cur[0]} pos={cur[1]} | ELAPSE {cur[2]}")
                last["v"] = cur
                await asyncio.sleep(0.1)

        async def step(name, action, timeout=30, **kw):
            t = now()
            try:
                await r.call(AVT, action, timeout=timeout, InstanceID=0, **kw)
                log(f"{name} OK after {now() - t:.2f}s")
            except Exception as err:  # noqa: BLE001
                log(f"{name} FAILED after {now() - t:.2f}s: {err}")

        async def actions(label):
            a = await r.call(AVT, "GetCurrentTransportActions", InstanceID=0)
            log(f"actions {label}: {a.get('Actions')}")

        await r.call(AVT, "Stop", InstanceID=0)
        url = f"http://{local}:8768/stream/pause.{fmt.ext}"
        await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, fmt))
        await actions("after SetAVTransportURI")
        await step("Play", "Play", timeout=60, Speed=1)
        await actions("while playing")
        await poll("playing", 6)
        await step("Pause", "Pause")
        await poll("paused", 8)
        await actions("while paused")
        await step("Resume (Play)", "Play", timeout=60, Speed=1)
        await poll("resumed", 5)
        await step("Seek REL_TIME 0:01:00", "Seek", Unit="REL_TIME", Target="0:01:00")
        await poll("after seek", 5)
        await step("Stop", "Stop")
        await poll("stopped", 1.5)
    srv.close()


asyncio.run(main())
