"""Local vs remote, HTTP vs HTTPS, redirects: what URLs will the renderer play?"""
import asyncio, socket, sys
from pathlib import Path
from types import SimpleNamespace
import aiohttp
sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, Fmt, Renderer, Server, didl, log, now  # noqa: E402

from fidelio_discover import resolve  # noqa: E402
IP = resolve(sys.argv[1] if len(sys.argv) > 1 else None)
REMOTE = "commondatastorage.googleapis.com/codeskulptor-demos/DDR_assets/Kangaroo_MusiQue_-_The_Neverwritten_Role_Playing_Game.mp3"


class RedirServer(Server):
    """Probe server plus /redir?to=<url> -> 302."""
    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        line = head.split(b"\r\n", 1)[0].decode()
        method, path = line.split(" ")[:2]
        if path.startswith("/redir/"):
            target = {"local": f"http://{LOCAL}:8773/stream/local.mp3", "remote": f"http://{REMOTE}"}[path.split("/")[2]]
            log(f"redirect server: {method} {path} -> 302 {target}")
            writer.write(f"HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain(); writer.close(); return
        # hand everything else to the normal probe server
        r2 = asyncio.StreamReader(); r2.feed_data(head); r2.feed_eof()
        await super().handle(r2, writer)


async def main():
    global LOCAL
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((IP, 1900)); LOCAL = so.getsockname()[0]
    args = SimpleNamespace(mode="length", pace="burst", prebuffer=2.0, speed=1.0, header_delay=0.0,
                           audio_seconds=120, flags="01700000000000000000000000000000")
    server = RedirServer(args)
    fmt = Fmt("mp3:44100"); server.set_media(fmt, fmt.make(120, Path(__file__).parent / "media", "beep"), 0.0)
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=8773)
    cases = [("1 local by IP (control)", f"http://{LOCAL}:8773/stream/local.mp3"),
             ("2 remote HTTP by name", f"http://{REMOTE}"),
             ("3 remote HTTPS by name", f"https://{REMOTE}"),
             ("4 local 302 -> local", f"http://{LOCAL}:8773/redir/local"),
             ("5 local 302 -> remote HTTP", f"http://{LOCAL}:8773/redir/remote")]
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = Renderer(s, IP, 49153, min_interval=0.2); await r.load()
        f = Fmt("mp3:44100:16:seek=1")   # musicTrack, audio/mpeg, OP=01 like a media server
        for name, url in cases:
            log(f"===== case {name}: {url}")
            await r.call(AVT, "Stop", InstanceID=0); await asyncio.sleep(1)
            t = now()
            try:
                await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, f))
                await r.call(AVT, "Play", timeout=45, InstanceID=0, Speed=1)
                log(f"case {name}: Play OK after {now() - t:.2f}s")
            except Exception as e:  # noqa: BLE001
                log(f"case {name}: Play FAILED after {now() - t:.2f}s: {e}")
            await asyncio.sleep(6)
            ti = await r.call(AVT, "GetTransportInfo", InstanceID=0)
            pi = await r.call(AVT, "GetPositionInfo", InstanceID=0)
            log(f"case {name}: RESULT state={ti.get('CurrentTransportState')} pos={pi.get('RelTime')} "
                f"duration={pi.get('TrackDuration')}")
        await r.call(AVT, "Stop", InstanceID=0)
    srv.close()

asyncio.run(main())
