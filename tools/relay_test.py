"""Relay a real Icecast stream to the renderer re-framed as HTTP/1.1 chunked; then play it."""
import asyncio, socket, sys
import aiohttp
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from dlna_probe import AVT, Fmt, Renderer, didl, log, now  # noqa: E402

from fidelio_discover import resolve  # noqa: E402
IP, UPSTREAM = resolve(sys.argv[1]), sys.argv[2]
FRAMING = sys.argv[3] if len(sys.argv) > 3 else "chunked"   # chunked | close


async def relay(reader, writer):
    req = await reader.readuntil(b"\r\n\r\n")
    line = req.split(b"\r\n", 1)[0].decode()
    log(f"renderer -> relay: {line}")
    hdr = "HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nConnection: close\r\n"
    hdr += "Transfer-Encoding: chunked\r\n\r\n" if FRAMING == "chunked" else "\r\n"
    writer.write(hdr.encode()); await writer.drain()
    if line.startswith("HEAD"):
        writer.close(); return
    sent = 0
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(UPSTREAM, headers={"User-Agent": "Mozilla/5.0"}) as up:
                log(f"relay <- upstream: HTTP {up.status} {up.headers.get('Content-Type')} "
                    f"TE={up.headers.get('Transfer-Encoding')} CL={up.headers.get('Content-Length')}")
                async for chunk in up.content.iter_chunked(8192):
                    writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n" if FRAMING == "chunked" else chunk)
                    await writer.drain(); sent += len(chunk)
    except (ConnectionError, OSError) as e:
        log(f"renderer closed relay after {sent} bytes ({e.__class__.__name__})")


async def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((IP, 1900)); local = so.getsockname()[0]
    srv = await asyncio.start_server(relay, host="0.0.0.0", port=8772)
    url = f"http://{local}:8772/relay.mp3"
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as s:
        r = Renderer(s, IP, 49153, min_interval=0.2); await r.load()
        f = Fmt("mp3:44100:16:cls=object.item.audioItem.audioBroadcast")
        await r.call(AVT, "Stop", InstanceID=0)
        await r.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl(url, f))
        t = now()
        try:
            await r.call(AVT, "Play", timeout=45, InstanceID=0, Speed=1)
            log(f"Play OK after {now() - t:.2f}s (framing={FRAMING})")
        except Exception as e:  # noqa: BLE001
            log(f"Play FAILED after {now() - t:.2f}s: {e}")
        for _ in range(4):
            await asyncio.sleep(3)
            ti = await r.call(AVT, "GetTransportInfo", InstanceID=0)
            pi = await r.call(AVT, "GetPositionInfo", InstanceID=0)
            log(f"state={ti.get('CurrentTransportState')} pos={pi.get('RelTime')}")
        await r.call(AVT, "Stop", InstanceID=0)
    srv.close()

asyncio.run(main())
