#!/usr/bin/env python3
"""Probe a DLNA renderer's start-up behaviour without Music Assistant.

Acts as both DLNA controller and media server:
  * serves a generated test signal (1 beep per second) over HTTP, with a
    configurable format, first-byte delay, transfer mode and pacing
  * sends Stop / SetAVTransportURI / Play to the renderer
  * logs every transport-state change (polled and GENA-evented) and every HTTP
    request the renderer makes, on one timeline
  * flags any PAUSED/STOPPED state the renderer enters without being told to

Single run:
  python dlna_probe.py --radio Kitchen --format mp3:44100 --delay 5
Format matrix (all codecs x rates x bit depths, results in CSV):
  python dlna_probe.py --radio Kitchen --matrix
Series (same format repeated, e.g. for delay or warm/cold behaviour):
  python dlna_probe.py --ip 192.0.2.10 --format mp3:44100 --delays 0,3,6,9 --repeat 2

Format spec: codec[:rate[:bits[:key=value...]]]   codecs: mp3 flac wav lpcm aac m4a wma
  options: mime=<Content-Type/protocolInfo mime>  pn=<DLNA.ORG_PN value or *>
           didl=1 (add sampleFrequency/bitsPerSample/nrAudioChannels to DIDL res)
           level=<flac compression 0-12>  block=<flac frame size>  br=<lossy bitrate, e.g. 128k>
           noise=<noise amplitude, default 0.03>
  e.g. lpcm:48000:16:didl=1   flac:44100:16:level=0   mp3:44100:16:br=128k
"""
import argparse
import asyncio
import csv
import html
import socket
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import aiohttp

T0 = time.monotonic()
VERBOSE_STATUS = "--verbose-status" in sys.argv
if VERBOSE_STATUS:
    sys.argv.remove("--verbose-status")


def now() -> float:
    return time.monotonic() - T0


LAST_LOG = time.monotonic()


import faulthandler  # noqa: E402

faulthandler.dump_traceback_later(120, repeat=True)


def log(msg: str) -> None:
    global LAST_LOG
    LAST_LOG = time.monotonic()
    print(f"t={now():7.2f}  {msg}", flush=True)
    # re-arm: if the process (even a blocked event loop) is silent for 120 s, dump all thread stacks
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(120, repeat=True)


DLNA_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
FAIL_STATES = ("PAUSED_PLAYBACK", "STOPPED", "NO_MEDIA_PRESENT")

MATRIX = [
    "mp3:44100", "mp3:48000", "mp3:32000",
    "wav:44100:16", "wav:48000:16", "wav:44100:24", "wav:96000:16",
    "lpcm:44100", "lpcm:48000",
    "flac:44100:16", "flac:48000:16", "flac:44100:24",
    "aac:44100", "aac:48000",
    "m4a:44100",
    "wma:44100",
]


class Fmt:
    """A concrete stream format: codec, sample rate, bit depth."""

    def __init__(self, spec: str):
        parts = spec.split(":")
        self.codec = parts[0]
        self.rate = int(parts[1]) if len(parts) > 1 else 44100
        self.bits = int(parts[2]) if len(parts) > 2 else 16
        self.opts = dict(kv.split("=", 1) for kv in parts[3:])
        if self.codec not in ("mp3", "flac", "wav", "lpcm", "l24", "aac", "m4a", "alac", "wma",
                              "heaac", "heaac2", "heaac2mp4", "ogg", "opus", "m3u", "asx", "wax", "pls"):
            raise ValueError(f"unknown codec {self.codec}")

    def __str__(self):
        return ":".join([self.codec, str(self.rate), str(self.bits),
                         *(f"{k}={v}" for k, v in self.opts.items())])

    @property
    def ext(self):
        if "ext" in self.opts:
            return self.opts["ext"]
        return {"lpcm": "pcm", "l24": "pcm", "aac": "aac", "alac": "m4a",
                "heaac": "aac", "heaac2": "aac", "heaac2mp4": "m4a", "opus": "opus"}.get(self.codec, self.codec)

    PLAYLISTS = ("m3u", "asx", "wax", "pls")

    @property
    def mime(self):
        if "mime" in self.opts:
            return self.opts["mime"]
        return {
            "mp3": "audio/mpeg",
            "flac": "audio/x-flac",
            "wav": "audio/x-wav",
            "lpcm": f"audio/L16;rate={self.rate};channels={self.opts.get('ch', '2')}",
            "l24": f"audio/L24;rate={self.rate};channels=2",
            "alac": "audio/mp4",
            "aac": "audio/vnd.dlna.adts",
            "heaac": "audio/vnd.dlna.adts",
            "heaac2": "audio/vnd.dlna.adts",
            "heaac2mp4": "audio/mp4",
            "ogg": "audio/ogg",
            "opus": "audio/ogg",
            "m3u": "audio/x-mpegurl",
            "asx": "application/asx",
            "wax": "audio/x-ms-wax",
            "pls": "audio/x-scpls",
            "m4a": "audio/mp4",
            "wma": "audio/x-ms-wma",
        }[self.codec]

    @property
    def pn(self):
        if "pn" in self.opts:
            return "*" if self.opts["pn"] == "*" else f"DLNA.ORG_PN={self.opts['pn']}"
        return {"mp3": "DLNA.ORG_PN=MP3", "lpcm": "DLNA.ORG_PN=LPCM"}.get(self.codec, "*")

    def ffmpeg_args(self):
        s16 = self.bits == 16
        o = self.opts
        flac_extra = ([*(["-compression_level", o["level"]] if "level" in o else []),
                       *(["-frame_size", o["block"]] if "block" in o else [])])
        return {
            "mp3": ["-c:a", "libmp3lame",
                    *(["-q:a", o["vbr"]] if "vbr" in o else ["-b:a", o.get("br", "320k")]), "-f", "mp3"],
            "flac": ["-c:a", "flac", "-sample_fmt", "s16" if s16 else "s32",
                     *([] if s16 else ["-bits_per_raw_sample", "24"]), *flac_extra, "-f", "flac"],
            "wav": ["-c:a", "pcm_s16le" if s16 else "pcm_s24le", "-f", "wav"],
            "lpcm": ["-c:a", "pcm_s16be", "-f", "s16be"],
            "l24": ["-c:a", "pcm_s24be", "-f", "s24be"],
            "alac": ["-c:a", "alac", "-sample_fmt", "s16p" if s16 else "s32p",
                     "-movflags", "+faststart", "-f", "mp4"],
            "aac": ["-c:a", "aac", "-b:a", o.get("br", "256k"), "-f", "adts"],
            "m4a": ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "mp4"],
            "wma": ["-c:a", "wmav2", "-b:a", "192k", "-f", "asf"],
            "ogg": ["-c:a", "vorbis", "-strict", "-2", "-q:a", "5", "-f", "ogg"],
            "opus": ["-c:a", "libopus", "-b:a", o.get("br", "128k"), "-f", "ogg"],
            # HE-AAC via macOS AudioToolbox; eAAC+ = HE-AAC v2 (SBR + parametric stereo)
            "heaac": ["-c:a", "aac_at", "-sample_fmt", "s16", "-profile:a", "4", "-b:a", o.get("br", "64k"), "-f", "adts"],
            "heaac2": ["-c:a", "aac_at", "-sample_fmt", "s16", "-profile:a", "28", "-b:a", o.get("br", "32k"), "-f", "adts"],
            "heaac2mp4": ["-c:a", "aac_at", "-sample_fmt", "s16", "-profile:a", "28", "-b:a", o.get("br", "32k"),
                          "-movflags", "+faststart", "-f", "mp4"],
        }[self.codec]

    def forced_length(self) -> int:
        """Music Assistant's calculate_content_length(fmt, 12 * 3600)."""
        pcm = self.rate * (self.bits // 8) * 2 * 12 * 3600
        br = {"mp3": 320, "aac": 256, "m4a": 256, "wma": 192, "heaac": 64, "heaac2": 32, "ogg": 160, "opus": 128,
              "heaac2mp4": 32}.get(self.codec)
        if br:
            return br * 1000 // 8 * 12 * 3600
        if self.codec in ("flac", "alac"):
            return int(pcm * 0.747)
        return pcm

    def playlist(self, inner_url: str) -> bytes:
        nl = "\r\n" if self.opts.get("crlf") == "1" else "\n"
        if self.codec == "m3u":
            head = "" if self.opts.get("plain") == "1" else f"#EXTM3U{nl}#EXTINF:-1,probe{nl}"
            return f"{head}{inner_url}{nl}".encode()
        if self.codec == "pls":
            return (f"[playlist]{nl}NumberOfEntries=1{nl}File1={inner_url}{nl}Title1=probe{nl}"
                    f"Length1=-1{nl}Version=2{nl}").encode()
        return (f'<asx version="3.0"><title>probe</title><entry><title>probe</title>'
                f'<ref href="{inner_url}"/></entry></asx>\n').encode()

    def make(self, seconds: int, outdir: Path, signal: str = "noise") -> bytes:
        """signal "beep": 880 Hz beep per second, near-silent otherwise, so it
        compresses to a few KB/s in FLAC/AAC. "noise": the same beeps over
        low-level white noise, giving music-like bitrates for every codec."""
        enc = "".join(f"_{k}{v}" for k, v in sorted(self.opts.items())
                      if k in ("level", "block", "br", "noise", "ch", "vbr"))
        path = outdir / f"{signal}_{seconds}s_{self.codec}_{self.rate}_{self.bits}{enc}.{self.ext}"
        if not path.exists():
            outdir.mkdir(parents=True, exist_ok=True)
            beep = "0.2*sin(2*PI*880*t)*lt(mod(t\\,1)\\,0.12)"
            left, right = beep, beep
            if signal == "noise":
                amp = self.opts.get("noise", "0.03")
                left += f"+{amp}*(2*random(0)-1)"
                right += f"+{amp}*(2*random(1)-1)"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"aevalsrc={left}|{right}:s={self.rate}:d={seconds}",
                 *(["-ac", self.opts["ch"]] if "ch" in self.opts else []),
                 *self.ffmpeg_args(), str(path)],
                check=True)
        data = path.read_bytes()
        if self.codec == "wav" and "hdr" in self.opts:
            # emulate a live WAV stream whose length is unknown when the header is written
            size = {"ff": b"\xff\xff\xff\xff", "0": b"\x00\x00\x00\x00"}[self.opts["hdr"]]
            d = data.find(b"data")
            data = data[:4] + size + data[8:d + 4] + size + data[d + 8:]
        return data


# ---------------------------------------------------------------- HTTP server

class Server:
    """Minimal HTTP server: full control over headers, pacing and logging."""

    def __init__(self, args):
        self.args = args
        self.fmt = None
        self.data = b""
        self.delay = 0.0
        self.requests = 0
        self.first_get_at = None
        self.bytes_sent = 0
        self.on_notify = None

    def set_media(self, fmt: Fmt, data: bytes, delay: float):
        self.fmt, self.data, self.delay = fmt, data, delay
        self.first_get_at = None
        self.bytes_sent = 0
        self.progress = []  # (monotonic time, bytes sent) of the current GET
        self.inner = None   # (Fmt, data) of the stream a playlist points to

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")[0]
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, path, version = (lines[0].split(" ", 2) + ["", ""])[:3]
        hdrs = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        try:
            if method == "NOTIFY":
                await self.handle_notify(reader, writer, hdrs)
            elif path.startswith("/stream"):
                await self.handle_stream(writer, method, hdrs, peer, version, path)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def handle_notify(self, reader, writer, hdrs):
        if hdrs.get("transfer-encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int((await reader.readline()).strip() or b"0", 16)
                if size == 0:
                    break
                body += await reader.readexactly(size)
                await reader.readline()
        else:
            body = await reader.readexactly(int(hdrs.get("content-length", 0)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        if self.on_notify:
            self.on_notify(body)

    async def handle_stream(self, writer, method, hdrs, peer, version="", path=""):
        a = self.args
        fmt, data, mode, pace = self.fmt, self.data, a.mode, a.pace
        if self.inner and "/inner" in path:
            fmt, data = self.inner           # the stream a playlist points to
            mode, pace = "length", "burst"
        elif fmt.codec in Fmt.PLAYLISTS:
            mode, pace = "length", "burst"   # the playlist file itself
        self.requests += 1
        n = self.requests
        if method == "GET" and self.first_get_at is None:
            self.first_get_at = now()
        shown = {k: v for k, v in hdrs.items()
                 if k in ("range", "user-agent", "getcontentfeatures.dlna.org",
                          "transfermode.dlna.org", "icy-metadata")}
        log(f"HTTP#{n} {method} {version} from {peer} {shown}")

        seekable = fmt.opts.get("seek") == "1"
        if seekable:
            mode = "length"   # a seekable resource has a size and is served with Content-Length
        op = "01" if seekable else "00"
        flags = f"DLNA.ORG_OP={op};DLNA.ORG_CI=0;DLNA.ORG_FLAGS={a.flags}"
        offset = 0
        rng = hdrs.get("range", "")
        if seekable and rng.startswith("bytes="):
            try:
                offset = int(rng[6:].split("-", 1)[0] or 0)
            except ValueError:
                offset = 0
            offset = min(offset, len(data))
        features = flags if fmt.pn == "*" else f"{fmt.pn};{flags}"
        dlna_hdrs = [] if fmt.opts.get("nodlna") == "1" else [
            f"contentFeatures.dlna.org: {features}", "transferMode.dlna.org: Streaming"]
        status = "HTTP/1.1 206 Partial Content" if (seekable and rng) else "HTTP/1.1 200 OK"
        if fmt.opts.get("icystatus") == "1":
            status = "ICY 200 OK"   # SHOUTcast v1 style status line
        resp = [status,
                f"Content-Type: {fmt.mime}",
                *dlna_hdrs,
                f"Accept-Ranges: {'bytes' if seekable else 'none'}",
                *(["icy-name: probe", "icy-br: 320", "icy-pub: 0"] if fmt.opts.get("icystatus") == "1" else []),
                "Cache-Control: no-cache",
                "Connection: close",
                "Server: dlna_probe"]
        if seekable:
            resp.append(f"Content-Length: {len(data) - offset}")
            if rng:
                resp.append(f"Content-Range: bytes {offset}-{len(data) - 1}/{len(data)}")
        elif mode == "length":
            resp.append(f"Content-Length: {len(data)}")
        elif mode == "forced":
            # like MA's forced_content_length: size of 12 hours of this format
            resp.append(f"Content-Length: {fmt.forced_length()}")
        elif mode == "chunked":
            resp.append("Transfer-Encoding: chunked")
        if seekable:
            log(f"HTTP#{n} -> {status.split(' ', 1)[1]}, offset {offset} of {len(data)}")
        if a.header_delay:
            await asyncio.sleep(a.header_delay)
        writer.write(("\r\n".join(resp) + "\r\n\r\n").encode())
        await writer.drain()
        if method == "HEAD":
            return

        if self.delay:
            await asyncio.sleep(self.delay)
            log(f"HTTP#{n} first audio byte sent after {self.delay}s delay")

        bps = len(data) / (a.audio_seconds if fmt.codec not in Fmt.PLAYLISTS else 1)
        data = data[offset:]   # Range request: serve from the requested byte offset
        self.progress = []
        sent = 0
        start = time.monotonic()
        try:
            while sent < len(data):
                if pace == "realtime":
                    # stay at most `prebuffer` seconds ahead of real time
                    allowed = int(bps * (a.speed * (time.monotonic() - start) + a.prebuffer))
                    if allowed <= sent:
                        await asyncio.sleep(0.05)
                        continue
                    chunk = data[sent:min(allowed, sent + 8192)]
                else:
                    chunk = data[sent:sent + 8192]
                if mode == "chunked":
                    writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                else:
                    writer.write(chunk)
                await writer.drain()
                sent += len(chunk)
                self.bytes_sent = max(self.bytes_sent, sent)
                self.progress.append((time.monotonic(), sent))
            if mode == "chunked":
                writer.write(b"0\r\n\r\n")
                await writer.drain()
            log(f"HTTP#{n} stream complete ({sent} bytes)")
        except (ConnectionError, OSError):
            log(f"HTTP#{n} renderer closed connection after {sent} bytes "
                f"(~{sent / bps:.1f}s audio, {time.monotonic() - start:.1f}s wall)")


# ------------------------------------------------------------ UPnP controller

class RendererHung(Exception):
    pass


class Renderer:
    """UPnP client that never has more than one request outstanding.

    The renderer is known to lock up (needing a power cycle) when it gets
    several requests in quick succession, so every request goes through a
    gate: one at a time, at least `min_interval` apart, and never with a
    timeout shorter than MIN_TIMEOUT.
    """

    MIN_TIMEOUT = 10.0

    def __init__(self, session, ip, port, min_interval=1.0, hung_after=120.0):
        self.s = session
        self.base = f"http://{ip}:{port}/"
        self.urls = {}
        self.name = "?"
        self.lock = asyncio.Lock()
        self.min_interval = min_interval
        self.hung_after = hung_after
        self.last_req_end = 0.0
        self.last_ok = time.monotonic()
        self.requests = 0

    async def _request(self, method, url, timeout, **kw):
        timeout = max(timeout, self.MIN_TIMEOUT)
        async with self.lock:
            wait = self.last_req_end + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.requests += 1
            try:
                async with self.s.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
                    text = await r.text()
                    self.last_ok = time.monotonic()
                    return r.status, r.headers, text
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if time.monotonic() - self.last_ok > self.hung_after:
                    raise RendererHung(f"no answer from renderer for {time.monotonic() - self.last_ok:.0f}s")
                raise
            finally:
                self.last_req_end = time.monotonic()

    async def load(self):
        async with self.s.get(self.base + "nmrDescription.xml") as r:
            desc = await r.text()
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        root = ET.fromstring(desc)
        self.name = root.findtext(".//d:friendlyName", namespaces=ns)
        fw = [el.text for el in root.iter() if el.tag.endswith("X_firmwareString")]
        for svc in root.iterfind(".//d:service", ns):
            st = svc.findtext("d:serviceType", namespaces=ns)
            self.urls[st] = (self.base + svc.findtext("d:controlURL", namespaces=ns).lstrip("/"),
                             self.base + svc.findtext("d:eventSubURL", namespaces=ns).lstrip("/"))
        log(f"renderer: {self.name} fw={fw[0] if fw else '?'}")

    async def call(self, service, action, timeout=10.0, **args):
        body = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
        env = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'}
        status, _, text = await self._request("POST", self.urls[service][0], timeout,
                                              data=env.encode(), headers=headers)
        out = {}
        for el in ET.fromstring(text).iter():
            if len(el) == 0 and el.text is not None:
                out[el.tag.split("}")[-1]] = el.text
        if status != 200:
            raise RuntimeError(f"HTTP {status} {out.get('errorCode')} {out.get('errorDescription')}")
        return out

    async def idle(self):
        """Wait until the next request would be sent without gate delay."""
        async with self.lock:
            wait = self.last_req_end + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_req_end = 0.0

    async def wait_responsive(self, limit=120.0):
        """The renderer's UPnP stack blocks while it struggles with a stream;
        probe gently (one request, >=10s timeout, 10s apart) until it answers."""
        t = now()
        while now() - t < limit:
            try:
                await self.call(AVT, "GetTransportInfo", InstanceID=0)
                return now() - t
            except RendererHung:
                raise
            except Exception:
                await asyncio.sleep(10.0)
        return None

    async def subscribe(self, callback):
        try:
            _, headers, _ = await self._request("SUBSCRIBE", self.urls[AVT][1], 10.0, headers={
                "CALLBACK": f"<{callback}>", "NT": "upnp:event", "TIMEOUT": "Second-3600"})
            return headers.get("SID")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            log(f"GENA subscribe failed: {err}")
            return None

    async def unsubscribe(self, sid):
        if sid:
            try:
                await self._request("UNSUBSCRIBE", self.urls[AVT][1], 10.0, headers={"SID": sid})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass


def res_attrs(fmt: Fmt) -> str:
    out = ""
    if fmt.opts.get("didl") == "1":
        out += f' sampleFrequency="{fmt.rate}" bitsPerSample="{fmt.bits}" nrAudioChannels="2"'
    if fmt.opts.get("seek") == "1" and getattr(fmt, "size", None):
        d = int(fmt.duration_s)
        out += f' size="{fmt.size}" duration="{d // 3600}:{d // 60 % 60:02d}:{d % 60:02d}.000"'
    return out


def pi_features(fmt: Fmt) -> str:
    """4th protocolInfo field. With seek=1 it carries the full DLNA feature string like MiniDLNA."""
    if fmt.opts.get("seek") != "1" and fmt.opts.get("piop") != "1":
        return fmt.pn
    full = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    return full if fmt.pn == "*" else f"{fmt.pn};{full}"


def didl(url, fmt: Fmt):
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1"><dc:title>DLNA probe</dc:title>'
            f'<upnp:class>{fmt.opts.get("cls", "object.item.audioItem.musicTrack")}</upnp:class>'
            f'<res protocolInfo="http-get:*:{html.escape(fmt.opts.get("pimime", fmt.mime))}:{pi_features(fmt)}"'
            f'{res_attrs(fmt)}>{html.escape(url)}</res>'
            '</item></DIDL-Lite>')


# ------------------------------------------------------------------ run logic

class Timeline:
    def __init__(self):
        self.state = None
        self.events = []  # (t, source, state)
        self.play_sent_at = None
        self.stop_sent_at = None

    def update(self, source, state):
        if state and state != self.state:
            self.state = state
            self.events.append((now(), source, state))
            flag = ""
            if self.play_sent_at is not None and self.stop_sent_at is None and state in FAIL_STATES:
                flag = "   <-- UNSOLICITED (no pause/stop sent)"
            log(f"STATE {state} [{source}]{flag}")


def parse_notify(body: bytes, tl: Timeline, verbose: bool):
    try:
        for el in ET.fromstring(body).iter():
            if el.tag.endswith("LastChange") and el.text:
                vals = {e.tag.split("}")[-1]: e.get("val")
                        for e in ET.fromstring(el.text).iter() if e.get("val") is not None}
                if verbose:
                    log(f"GENA { {k: v for k, v in vals.items() if k in ('TransportState', 'TransportStatus', 'CurrentTransportActions')} }")
                tl.update("event", vals.get("TransportState"))
    except ET.ParseError as err:
        log(f"GENA unparsable NOTIFY: {err}")


async def run_once(args, rdr, server, local_ip, fmt: Fmt, delay: float, label: str) -> dict:
    log(f"===== {label}: {fmt} mode={args.mode} delay={delay}s")
    if fmt.codec in Fmt.PLAYLISTS:
        inner = Fmt("mp3:44100")
        server.inner = (inner, inner.make(args.audio_seconds, Path(__file__).parent / "media", args.signal))
        data = fmt.playlist(f"http://{local_ip}:{args.port}/stream/{label}.inner.mp3")
    else:
        server.inner = None
        data = fmt.make(args.audio_seconds, Path(__file__).parent / "media", args.signal)
        fmt.size, fmt.duration_s = len(data), args.audio_seconds
    log(f"media: {len(data) / args.audio_seconds / 1000:.1f} kB/s ({args.signal})")
    server.set_media(fmt, data, delay)
    tl = Timeline()
    server.on_notify = lambda body: parse_notify(body, tl, args.verbose)
    sid = await rdr.subscribe(f"http://{local_ip}:{args.port}/gena")

    try:
        await rdr.call(AVT, "Stop", InstanceID=0)
    except Exception:
        pass
    await asyncio.sleep(1.0)
    if args.volume is not None:
        await rdr.call(RC, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=args.volume)

    url = f"http://{local_ip}:{args.port}/stream/{label}.{fmt.ext}"
    result = {"label": label, "format": str(fmt), "mode": args.mode, "pace": args.pace,
              "delay": delay, "kBps": round(len(data) / args.audio_seconds / 1000, 1)}
    try:
        await rdr.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url,
                       CurrentURIMetaData=didl(url, fmt))
    except Exception as err:
        log(f"SetAVTransportURI rejected: {err}")
        result.update(result="URI rejected", detail=str(err))
        await rdr.unsubscribe(sid)
        return result

    play = {}

    async def send_play():
        # this renderer holds the SOAP reply until playback has started
        try:
            await rdr.call(AVT, "Play", timeout=args.play_timeout, InstanceID=0, Speed=1)
            play["t"] = now()
            log(f"Play acknowledged after {play['t'] - tl.play_sent_at:.2f}s")
        except RendererHung:
            raise
        except Exception as err:
            play["t_err"] = now()
            play["err"] = f"{err.__class__.__name__} {err}".strip()
            log(f"Play failed after {play['t_err'] - tl.play_sent_at:.2f}s: {play['err']}")

    status_task = None
    if args.status_poll:
        status_task = asyncio.create_task(poll_homestatus(rdr, args.status_poll))
    await rdr.idle()  # so the Play timings below are not skewed by the gate
    log("Play")
    tl.play_sent_at = now()
    play_task = asyncio.create_task(send_play())

    last_pos, playing_since = None, None
    end = now() + args.observe
    while now() < end:
        if play_task.done() and play_task.exception():
            raise play_task.exception()
        if not play_task.done():
            # the renderer is blocked on Play: don't pile up requests, rely on GENA events
            await asyncio.sleep(0.25)
            continue
        try:
            ti = await rdr.call(AVT, "GetTransportInfo", InstanceID=0)
            tl.update("poll", ti.get("CurrentTransportState"))
            pi = await rdr.call(AVT, "GetPositionInfo", InstanceID=0)
            if pi.get("RelTime") != last_pos:
                last_pos = pi.get("RelTime")
                if args.verbose:
                    log(f"position {last_pos}")
        except RendererHung:
            raise
        except Exception as err:
            log(f"poll: {err.__class__.__name__} {err}")
        if tl.state == "PLAYING" and playing_since is None:
            playing_since = now()
        # finished early: played long enough, or Play definitively failed
        if playing_since and now() - playing_since >= args.hold:
            break
        if "err" in play:
            break
        await asyncio.sleep(args.poll)

    tl.stop_sent_at = now()
    if status_task:
        status_task.cancel()
    if not play_task.done():
        play_task.cancel()
    try:
        await rdr.call(AVT, "Stop", InstanceID=0)
    except Exception:
        pass
    await rdr.unsubscribe(sid)

    t_play = tl.play_sent_at
    first_playing = next((t for t, _, s in tl.events if s == "PLAYING" and t >= t_play), None)
    unsolicited = [(round(t - t_play, 2), s) for t, _, s in tl.events
                   if t_play <= t < tl.stop_sent_at and s in FAIL_STATES]
    # bytes the renderer had received when it started playing
    start_kb = None
    if first_playing:
        before = [b for t, b in server.progress if t - T0 <= first_playing]
        start_kb = round(before[-1] / 1000) if before else 0
    # steady-state read rate vs nominal stream rate: once its buffer is full the
    # renderer reads exactly as fast as it plays, so ~0.92 = 48 kHz played at 44.1
    ratio = None
    win = [(t, b) for t, b in server.progress if T0 + tl.stop_sent_at - args.rate_window <= t <= T0 + tl.stop_sent_at]
    if args.pace == "burst" and first_playing and len(win) > 2 and win[-1][0] - win[0][0] > args.rate_window / 2:
        rate = (win[-1][1] - win[0][1]) / (win[-1][0] - win[0][0])
        ratio = round(rate / (len(server.data) / args.audio_seconds), 3)
    result.update(
        start_kB=start_kb,
        read_ratio=ratio,
        play_reply_s=round(play["t"] - t_play, 2) if "t" in play else None,
        playing_after_s=round(first_playing - t_play, 2) if first_playing else None,
        first_get_s=round(server.first_get_at - t_play, 2) if server.first_get_at else None,
        bytes_read=server.bytes_sent,
        last_position=last_pos,
        unsolicited=" ".join(f"{s}@{t}" for t, s in unsolicited),
        detail=play.get("err", ""),
    )
    if first_playing and not unsolicited:
        result["result"] = "OK"
    elif first_playing:
        result["result"] = "played, then stopped"
    else:
        result["result"] = "FAIL"
    log(f"----- {label}: {result['result']}  play_reply={result['play_reply_s']}s "
        f"playing_after={result['playing_after_s']}s {result['unsolicited']} {result['detail']}")

    recovered = await rdr.wait_responsive()
    if recovered is None:
        log("renderer did not become responsive again within 120s")
    elif recovered > args.min_interval + 1:
        log(f"renderer UPnP unresponsive for another {recovered:.1f}s")
    result["recovery_s"] = round(recovered, 1) if recovered is not None else None
    return result


async def poll_homestatus(rdr, interval):
    """Poll the vendor HTTP interface (port 8889) independently of UPnP:
    one request at a time, `interval` seconds apart, 10 s timeout."""
    url = rdr.base.rsplit(":", 1)[0] + ":8889/HOMESTATUS"
    last = None
    while True:
        t = time.monotonic()
        try:
            async with rdr.s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = " ".join((await r.text()).split())
            dt = time.monotonic() - t
            if body != last or dt > 1.0 or VERBOSE_STATUS:
                log(f"HOMESTATUS {dt * 1000:.0f} ms {body}")
                last = body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            log(f"HOMESTATUS no answer after {time.monotonic() - t:.1f}s ({err.__class__.__name__})")
            last = None
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - t)))


def print_table(results):
    cols = ["label", "format", "kBps", "mode", "pace", "delay", "result", "play_reply_s", "playing_after_s",
            "start_kB", "read_ratio",
            "first_get_s", "recovery_s", "unsolicited", "detail"]
    rows = [[str(r.get(c, "") if r.get(c) is not None else "-") for c in cols] for r in results]
    widths = [max(len(c), *(len(row[i]) for row in rows)) for i, c in enumerate(cols)]
    print()
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for row in rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", help="renderer address (overrides --radio and discovery)")
    p.add_argument("--radio", help="renderer name (friendlyName); resolved via FIDELIO_RADIOS, radios.conf or discovery")
    p.add_argument("--upnp-port", type=int, default=49153)
    p.add_argument("--port", type=int, default=8765, help="local HTTP port for stream + GENA")
    p.add_argument("--format", default="mp3:44100", help="codec[:rate[:bits]], comma-separated for several")
    p.add_argument("--matrix", action="store_true", help="test the built-in list of formats")
    p.add_argument("--signal", choices=["noise", "beep"], default="noise",
                   help="noise = beeps over white noise (realistic bitrates), beep = beeps only")
    p.add_argument("--mode", choices=["chunked", "length", "none", "forced"], default="chunked",
                   help="HTTP body framing: chunked, exact Content-Length, neither (close-delimited), "
                        "or forced = Content-Length of 12 h of audio like MA's forced_content_length")
    p.add_argument("--flags", default="01700000000000000000000000000000",
                   help="DLNA.ORG_FLAGS; MA uses 81700000000000000000000000000000 (sender paced)")
    p.add_argument("--delay", type=float, default=0.0, help="seconds between headers and first audio byte")
    p.add_argument("--delays", help="comma-separated list of delays to run as a series")
    p.add_argument("--header-delay", type=float, default=0.0, help="seconds before sending response headers")
    p.add_argument("--pace", choices=["realtime", "burst"], default="realtime",
                   help="realtime = live-stream pacing like MA; burst = as fast as the renderer reads")
    p.add_argument("--speed", type=float, default=1.0,
                   help="realtime pace factor; MA uses 1.02 (live sources) and 1.03 (flow stream)")
    p.add_argument("--prebuffer", type=float, default=2.0, help="seconds sent ahead of real time (realtime pace)")
    p.add_argument("--audio-seconds", type=int, default=120, help="length of generated test signal")
    p.add_argument("--observe", type=float, default=45.0, help="max seconds to observe after Play")
    p.add_argument("--rate-window", type=float, default=12.0,
                   help="seconds at the end of a burst run used to measure the read rate")
    p.add_argument("--hold", type=float, default=12.0, help="seconds to keep playing once PLAYING is seen")
    p.add_argument("--play-timeout", type=float, default=60.0, help="how long to wait for the Play SOAP reply")
    p.add_argument("--poll", type=float, default=1.0, help="state poll interval")
    p.add_argument("--min-interval", type=float, default=1.0,
                   help="minimum seconds between two requests to the renderer")
    p.add_argument("--status-poll", type=float, default=0.0,
                   help="also poll the vendor /HOMESTATUS on port 8889 every N seconds (0 = off)")
    p.add_argument("--volume", type=int, help="set renderer volume before playing")
    p.add_argument("--repeat", type=int, default=1, help="repeat the whole series N times")
    p.add_argument("--gap", type=float, default=5.0, help="idle seconds between runs")
    p.add_argument("--csv", help="write results to this CSV file")
    p.add_argument("-v", "--verbose", action="store_true", help="log GENA events and every position change")
    args = p.parse_args()
    from fidelio_discover import resolve
    args.ip = resolve(args.radio, args.ip)

    formats = [Fmt(s) for s in (MATRIX if args.matrix else args.format.split(","))]
    delays = [float(d) for d in args.delays.split(",")] if args.delays else [args.delay]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((args.ip, 1900))
        local_ip = s.getsockname()[0]

    server = Server(args)
    srv = await asyncio.start_server(server.handle, host="0.0.0.0", port=args.port)
    log(f"serving on {local_ip}:{args.port}")

    results = []
    # the renderer's UPnP stack resets reused keep-alive connections
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), connector=connector) as session:
        rdr = Renderer(session, args.ip, args.upnp_port, min_interval=args.min_interval)
        await rdr.load()
        try:
            vol = await rdr.call(RC, "GetVolume", InstanceID=0, Channel="Master")
            log(f"volume={vol.get('CurrentVolume')}")
        except Exception:
            pass
        n = 0
        try:
            for rep in range(args.repeat):
                for fmt in formats:
                    for delay in delays:
                        if n:
                            await asyncio.sleep(args.gap)
                        n += 1
                        results.append(await run_once(args, rdr, server, local_ip, fmt, delay, f"run{n}"))
        except RendererHung as err:
            log(f"ABORTED: {err}. The renderer may be hung and need a power cycle.")
        except KeyboardInterrupt:
            pass
        finally:
            log(f"{rdr.requests} UPnP requests sent in total")
            print_table(results)
            if args.csv and results:
                with open(args.csv, "w", newline="") as f:
                    keys = list(dict.fromkeys(k for r in results for k in r))
                    w = csv.DictWriter(f, fieldnames=keys)
                    w.writeheader()
                    w.writerows(results)
                print(f"\nCSV written to {args.csv}")
    srv.close()


def _dump_tasks(*_):
    """SIGUSR1: print every asyncio task's stack (where each one is waiting)."""
    import faulthandler
    faulthandler.dump_traceback(all_threads=True)
    try:
        for t in asyncio.all_tasks(LOOP):
            print(f"--- task {t.get_name()}", flush=True)
            t.print_stack(limit=8)
    except Exception as err:  # noqa: BLE001
        print(f"task dump failed: {err}", flush=True)


LOOP = None


async def _main_with_loop():
    global LOOP
    LOOP = asyncio.get_running_loop()
    import signal
    LOOP.add_signal_handler(signal.SIGUSR1, _dump_tasks)

    async def watchdog():
        # self-diagnosis: if nothing was logged for 90 s, dump where every task waits
        dumped = 0.0
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - LAST_LOG > 90 and LAST_LOG != dumped:
                print(f"t={now():7.2f}  WATCHDOG: no log output for 90 s, task dump follows", flush=True)
                _dump_tasks()
                dumped = LAST_LOG

    wd = asyncio.create_task(watchdog())
    try:
        await main()
    finally:
        wd.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(_main_with_loop())
    except KeyboardInterrupt:
        sys.exit(130)
