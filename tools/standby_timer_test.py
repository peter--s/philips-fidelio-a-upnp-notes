#!/usr/bin/env python3
"""Does a command during idle restart the 10-minute auto-standby timer?

Both radios are muted, play a short clip and stop. After --after minutes the test radio receives
a RenderingControl command (SetVolume with its unchanged value); the control radio gets nothing.
/HOMESTATUS is polled every 5 s until both are in standby; afterwards both are unmuted again.

  python standby_timer_test.py --test Kitchen --control Bedroom --after 4
"""
import argparse, asyncio, socket, sys, time
from pathlib import Path
from types import SimpleNamespace
import aiohttp
sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, RC, Fmt, Renderer, Server, didl, log  # noqa: E402
from fidelio_discover import resolve  # noqa: E402


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", required=True); p.add_argument("--control", required=True)
    p.add_argument("--after", type=float, default=4.0, help="minutes after Stop to send the command")
    p.add_argument("--port", type=int, default=8790)
    a = p.parse_args()
    radios = {"test": resolve(a.test), "control": resolve(a.control)}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((radios["test"], 1900)); local = so.getsockname()[0]
    sargs = SimpleNamespace(mode="chunked", pace="realtime", prebuffer=2.0, speed=1.0, header_delay=0.0,
                            audio_seconds=120, flags="01700000000000000000000000000000")
    server = Server(sargs); fmt = Fmt("mp3:44100")
    server.set_media(fmt, fmt.make(120, Path(__file__).parent / "media", "beep"), 0.0)
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=a.port)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = {k: Renderer(s, ip, 49153, min_interval=0.2) for k, ip in radios.items()}
        for x in r.values():
            await x.load()
        vol = {}
        for k, x in r.items():
            vol[k] = (await x.call(RC, "GetVolume", InstanceID=0, Channel="Master"))["CurrentVolume"]
            await x.call(RC, "SetMute", InstanceID=0, Channel="Master", DesiredMute=1)
            url = f"http://{local}:{a.port}/stream/{k}.mp3"
            await x.call(AVT, "Stop", InstanceID=0)
            await x.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, fmt))
            await x.call(AVT, "Play", timeout=60, InstanceID=0, Speed=1)
        await asyncio.sleep(5)
        stop = {}
        for k, x in r.items():
            await x.call(AVT, "Stop", InstanceID=0)
            stop[k] = time.monotonic()
            log(f"{x.name} ({k}) stopped, muted, volume {vol[k]}")
        sent, standby = False, {}
        while len(standby) < 2 and time.monotonic() - min(stop.values()) < 40 * 60:
            if not sent and time.monotonic() - stop["test"] >= a.after * 60:
                await r["test"].call(RC, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=vol["test"])
                log(f"{r['test'].name} (test): SetVolume({vol['test']}) sent {a.after} min after Stop")
                sent = True
            for k, x in r.items():
                if k in standby:
                    continue
                async with s.get(f"http://{radios[k]}:8889/HOMESTATUS", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if "'value':1" in (await resp.text()).replace(" ", "").replace("\n", ""):
                        standby[k] = (time.monotonic() - stop[k]) / 60
                        log(f"{x.name} ({k}) entered standby {standby[k]:.2f} min after Stop")
            await asyncio.sleep(5)
        for k, x in r.items():
            await x.call(RC, "SetMute", InstanceID=0, Channel="Master", DesiredMute=0)
            log(f"{x.name} ({k}) unmuted")
    srv.close()

asyncio.run(main())
