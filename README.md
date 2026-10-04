# philips-fidelio-a-upnp-notes

Measured behaviour of the **Philips Fidelio A wireless Hi-Fi family** (A1 AW1000, A2 AW2000, A3 AW3000,
A5 AW5000, A9 AW9000) as DLNA/UPnP MediaRenderers, plus their undocumented vendor HTTP interface on
port 8889. **Tested: A3 (AW3000/12) and A5 (AW5000/12) with firmware VH5.80S.** A1, A2 and A9 share
the firmware line but are untested here; see the family table in §2.

It does **not** cover the SoundAvia AD7000W (AirPlay line) or Streamium players such as the NP2500
or MCi-series.

| Legend | Meaning |
|---|---|
| ✔ | tested repeatedly |
| ◐ | observed once, by ear only, or derived from a third-party log |
| ⚠ | assumption or untested |

Test setup (Sep/Oct 2026): two AW5000/12 units and one AW3000/12, all running VH5.80S. The controller
was a Python probe on the same LAN that served generated test signals itself and logged every request
the speaker made ([`tools/`](#tools)). Pitch was checked by ear and by the speaker's read rate. Note for reproducing: keep the test host
from sleeping (e.g. `caffeinate -i` on macOS); host sleep made test runs stall. Real-world
cross-checks came from Music Assistant 2.10.4 and Home Assistant 2026.9 logs.

**A3 vs. A5:** identical in every test run on both models: format matrix, start thresholds, framing,
MIME handling, wake-up, menus and vendor API. The only difference seen was network latency, which is
attributable to Wi-Fi reception. ✔

---

## 1. What this adds to existing information

| Topic | Already public | Added here |
|---|---|---|
| Formats | Philips lists MP3, WMA, AAC/m4a, eAAC+, FLAC, ALAC, L-PCM; 32/44.1/48/96 kHz [1][2]; FLAC "below 96 kHz and 16 bit" [3]; aggregator sites also list OGG [13] | Test matrix per format, rate and bit depth: 24-bit unusable in every format; above 48 kHz only AAC works; L16 at 48 kHz plays at the wrong speed; `audio/flac` and `audio/L24` are rejected; eAAC+ decodes but fails as a low-bitrate live stream; OGG Vorbis/Opus and playlists unsupported; mono L16 plays at double speed; MIME aliases (§5) |
| Start latency | – | Fixed byte thresholds per codec; the **Play reply is held back** until audio plays (§4.2) |
| Pause | – | Enabled only by `DLNA.ORG_OP=01` in the DIDL protocolInfo; resume continues on the same connection; Seek unsupported (§4.2) |
| Standby | – | Auto-standby after 10 min (restarted by commands, not by polling); wake-up adds 0.3–1.9 s (§4.3) |
| HTTP streaming | MIME `audio/x-flac` needed for FLAC on a Streamium NP2500 [6]; A9: "needs a proxy for online content" [11] | Request pattern, framing support, no ICY; remote HTTP works, but no HTTPS, no redirects, no Icecast/SHOUTcast framing, so internet radio needs a proxy (§4.4, §4.5) |
| Twonky quirk flags for the Fidelio A9 [11] | `SUBSCRIBE_TWICE`, `POLL_AVT_STATE`, `NEEDS_STOP_4_AV`, `IGNORE_STOP_AFTER_PLAY`, `ACCEPT_SAME_SEQ`, seek flags | None of these behaviours observed on the A5; Seek unsupported anyway (§4.1, §4.2) |
| SSDP | – | Announcement interval and origin, boot sequence (§3) |
| Vendor API :8889 | Paths for standby, volume, sources used by homebridge-fidelio [5] | Response formats, **bass/treble control**, further settings, Wi-Fi status, no auto-standby setting (§6) |
| Robustness | Users report occasional lock-ups needing a power cycle | Not reproducible by load, parallelism, dead subscribers, abandoned requests or standby wake-ups (§7) |
| Controller interaction | – | Why Music Assistant's DLNA provider times out on these devices (§8) |

---

## 2. Identification

### Fidelio A family
| Model | Type | Inputs / outputs (Philips) | Status here |
|---|---|---|---|
| A1 – AW1000 | wireless Hi-Fi link, no amplifier | digital + analogue out, analogue in | ⚠ untested |
| A2 – AW2000 | wireless Hi-Fi receiver with amplifier for passive speakers | digital coax/optical + analogue in, subwoofer out | ⚠ untested; more sources, so likely more vendor-API source paths |
| A3 – AW3000 | single speaker, 36 W | analogue in | ✔ tested |
| A5 – AW5000 | single speaker, 60 W | analogue in | ✔ tested |
| A9 – AW9000 | stereo speaker pair, 100 W | digital + analogue in | ⚠ untested; reported to differ in formats (24/48 WAV, no MP3) [11] |

Firmware is released for the whole series (e.g. H5.10S for AW9000/5000/3000/2000/1000) [4]. No A4, A6,
A7 or A8 was found. Regional variants differ in the suffix (`/05 /10 /12 /37 /55 /79 /97` per the AW5000
manual) [12]. Sources: Philips product pages [19][20], press coverage [21].

### Tested devices

| Item | Value | |
|---|---|---|
| Device description | `http://<ip>:49153/nmrDescription.xml` | ✔ |
| SSDP `SERVER` | `Windows2000/0.0 UPnP/1.0 PhilipsIntelSDK/1.4 DLNADOC/1.50` | ✔ |
| UDN | `uuid:F00DBABE-AA5E-BABA-DADA-<MAC, 12 hex digits>`; `serialNumber` = MAC | ✔ |
| `modelName` / `modelNumber` / `UPC` | A5: `A` / `5` / `AW5000/12`; A3: `A` / `3` / `AW3000/12` | ✔ |
| Type number on the device's own info page | `AW5000/10` resp. `AW3000/10` (differs from `UPC`) | ✔ |
| DLNA class | `dlna:X_DLNADOC` = `DMR-1.50` | ✔ |
| Philips extensions (`pns:` namespace) | `X_firmwareString` (`VH5.80S`), `X_firmwareInteger` (`580`), `X_capabilitiesFileURL`, `X_deviceStatusURL` (both :8889), `X_installID` | ✔ |
| `presentationURL` | `http://<ip>:8889/index.htm` | ✔ |

UPnP services, all at port 49153:

| Service | Control URL | Event URL |
|---|---|---|
| AVTransport:1 | `upnp/control/AVTransport1` | `upnp/event/AVTransport1` |
| RenderingControl:1 | `upnp/control/RenderingControl1` | `upnp/event/RenderingControl1` |
| ConnectionManager:1 | `upnp/control/ConnectionManager1` | `upnp/event/ConnectionManager1` |

---

## 3. Discovery (SSDP)

| Behaviour | Detail | |
|---|---|---|
| `ssdp:alive` | every **50 s** (measured 50–54 s), sent 3× about 1 s apart, `CACHE-CONTROL: max-age=200`, no `BOOTID`; also sent in standby | ✔ |
| Timing origin | **driven by the speaker's own timer**: unsolicited NOTIFY messages at a constant period, with no correlation to the searches seen on the LAN (only 3–11 % of announcements fell within 2 s after a search, less than chance) | ✔ |
| Boot | `ssdp:byebye` first, then `ssdp:alive` 1–2 s later | ✔ |
| Power loss | no `byebye` is sent | ✔ |
| M-SEARCH responses | one unit repeatedly did not answer `MediaRenderer:1` searches while it kept announcing itself | ◐ |

Searches seen on the test LAN, for context: router (AVM FRITZ!Box) every ~10 s with five search
targets, Music Assistant every 5 min 4 s, Home Assistant `ssdp:all` every 10 min. Replies to searches
are unicast, so reply reliability towards other hosts could not be observed.

Consequence: controllers that rely only on periodic M-SEARCH may miss a device for one or more search
intervals. Listening to NOTIFY is more reliable.

---

## 4. UPnP behaviour

### 4.1 Connection handling
- **Persistent HTTP connections are not supported for SOAP/GENA.** A second request on a reused
  keep-alive connection is reset. Use one TCP connection per request. ✔
- GENA eventing works (`LastChange`). ✔
- `CurrentTransportActions`: empty in `NO_MEDIA_PRESENT`, `Play,Stop,Pause` after
  SetAVTransportURI, **`Play,Stop,Seek` while playing**. ✔
- `NextAVTransportURI` reports `NOT_IMPLEMENTED`, so there is no gapless queueing (use one continuous stream). ✔
- Service descriptions (SCPD): AVTransport has Play, Stop, Pause, SetAVTransportURI, the Get… actions
  and GetDeviceCapabilities/GetTransportSettings, but **no `Seek`, `Next`, `Previous` or `SetPlayMode`**,
  although `CurrentTransportActions` lists "Seek" while playing (a Seek call returns 710).
  RenderingControl has only **Volume (0–64, step 1), Mute and presets** (`ListPresets` →
  `FactoryDefaults`); no bass, treble, loudness or balance via UPnP (those live in the vendor API, §6). ✔
- `SetVolume`/`GetVolume` work (tested 9 → 11 → 9); the UPnP scale equals the vendor scale (0–64). ✔
- A new `SetAVTransportURI` is accepted **during playback without a prior Stop** (0.09 s); playback
  continues until the next `Play`, which switches to the new URL. ✔
- One GENA subscription is enough; `SEQ` counts up strictly (0…40 in a test, no duplicates); state
  changes arrive as events; no spurious `STOPPED` after `PLAYING`. ✔

### 4.2 Play, Pause, Stop
- **The Play reply is sent only after playback has started.** Until then the UPnP interface answers
  no requests, including `Stop` and status polls. ✔
- Playback starts once a codec-specific **amount of data** has arrived (A5 and A3 within ±2 %):

| Stream | Data received when PLAYING | Typical Play reply at real-time delivery |
|---|---|---|
| MP3 (64–320 kbit/s) | 203–240 kB | 320 kbit/s: 4.0–4.3 s with 2 s sent ahead, 6.0 s with nothing ahead; 128 kbit/s: 13.4 s with nothing ahead; **64 kbit/s: 23.9 s** |
| MP3 32 kbit/s | not reached | **fails after 30 s (501)** |
| AAC (ADTS) / AAC in MP4 | about 220 kB ◐ | about 6 s at 28 kB/s |
| HE-AAC v1 (64 kbit/s) | 206 kB | **23.7 s** at 8.2 kB/s |
| HE-AAC v2 / eAAC+ (32 kbit/s) | not reached | **fails after 30 s (501)** at 4.2 kB/s |
| ALAC 16-bit | about 500 kB ◐ | 6.1 s at 64 kB/s |
| FLAC 16-bit | 778–792 kB | 9.5–10.9 s at 64–73 kB/s |
| WAV / L16 16-bit | about 1.0–1.2 MB | 4.0–5.4 s |

- Sending audio ahead of real time shortens the start: with 5 s sent immediately, MP3 starts in
  1.4 s and WAV in 2.0 s. ✔
- If the first audio byte is delayed, the Play reply comes after delay + about 4 s; tested up to a
  12 s delay. ✔
- If the threshold is never reached, Play fails after about **30 s** with **HTTP 500 / UPnP error 501**. ✔
- **Pause depends only on the DIDL metadata**: the renderer offers and accepts `Pause` only if the
  `res@protocolInfo` 4th field passed with SetAVTransportURI contains **`DLNA.ORG_OP=01`**, e.g.
  `http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000`. ✔
  - With it, `CurrentTransportActions` is `Play,Stop,Pause`; Pause is answered in 0.1–0.2 s
    (`PAUSED_PLAYBACK`, `/ELAPSE` `play: 2`); `Play` resumes in about 0.5 s **on the same HTTP
    connection**: no new request, no Range request. This also works for a live, chunked, non-seekable
    stream: the renderer simply stops reading while paused. ✔
  - Without it (`OP=00` or no DLNA field), `CurrentTransportActions` is `Play,Stop,Seek`, `Pause` is
    rejected with **UPnP error 501**, and `Play` during playback **restarts the stream from 0**. ✔
  - The HTTP side doesn't matter: `Accept-Ranges`, `Content-Length` and the `contentFeatures.dlna.org`
    header neither enable nor disable Pause. ✔
  - A `DLNA.ORG_PN` profile name is not required: `DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=…`
    with `upnp:class` `audioBroadcast` works as well. ✔
  - First observed with a MiniDLNA file played via the Hi-Fi Cast app, which sends exactly such
    metadata (`OP=01`, `size`, `duration`); then reproduced with the probe. ✔
  - **Music Assistant** already sends `DLNA.ORG_OP=01` in its DIDL, hard-coded and independent of
    any setting: flow/radio streams as `audioBroadcast` with `FLAGS=0170…`, tracks as `musicTrack`
    with `duration` and `FLAGS=0150…` [17]. Its HTTP header `contentFeatures.dlna.org` says `OP=00`,
    which the renderer ignores for Pause. The DLNA player option "Replace Pause with Stop" turns
    Pause into Stop.
- **`Seek`** (`Unit=REL_TIME`) fails with **UPnP error 710** (seek mode not supported) in every
  variant, and the renderer stops playback. ✔
- DLNA `ORG_FLAGS` with or without the sender-paced bit makes no difference. ✔

### 4.3 Standby and wake-up
**Auto-standby:** the speaker enters standby **10 minutes after playback stops**, identical on A3 and
A5 (measured 10.01–10.08 min in 8 idle periods; 5–20 s polling of `/HOMESTATUS`). ✔
- A RenderingControl command during idle (`SetVolume`, **even with the unchanged value**) **restarts the
  10-minute timer**: 14.04 min after Stop with `SetVolume` at minute 4 vs. 10.08 min for the control
  speaker. ✔ (One earlier idle period with a volume change after 17 s ended at 10.05 min, slightly early
  for a restart; within 20 s polling resolution. ◐)
- Status polling (HA every 10 s, `/HOMESTATUS`) does **not** prevent standby. ✔
- The timeout is not configurable via any API (§6); volume and mute survive standby and wake-up. ✔
- The device also **boots into standby**; standby exits automatically on Play. ✔

**Wake-up latency** (MP3, real-time delivery; times from Play; first value A5, second value A3). ✔

| State before Play | first request (HEAD) | PLAYING |
|---|---|---|
| on | 0.35 / 0.45 s | 4.06 / 4.19 s |
| standby, just entered | 1.57 / 1.49 s | 5.31 / 5.20 s |
| standby 5 min | 0.67 / 2.05 s | 4.44 / 5.73 s |
| standby 20 min | 2.29 / 2.06 s | 6.05 / 5.73 s |
| standby 60 min | 2.13 / 2.08 s | 5.85 / 5.84 s |
| standby several hours (separate test) | 2.2 / 2.0 s | 6.0 / 5.7 s |
| after power-on | 2.2 s (A5) | – |

- Waking adds **+0.3 to +1.9 s**, varying between wake-ups and speakers without a clean pattern; from
  about 20 min of standby it settles at **about +1.7–1.9 s** (PLAYING after about 5.8 s). The Play reply
  then exceeds a 5 s client timeout even with immediate audio. ✔
- While in standby, UPnP and `/HOMESTATUS` keep answering quickly (20–130 ms); only the stream start is
  delayed. While waking, `/HOMESTATUS` may take about 1 s. ✔
- One wake-up on one A5 (29 Sep) showed a HEAD, then a stall of about 28 s before the GET with UPnP
  unresponsive for about 30 s; not reproduced in 10 later wake-ups. ◐
- A freshly booted unit fetched the stream right after SetAVTransportURI, before Play; derived from
  Music Assistant logs. ◐

### 4.4 How the renderer fetches the stream
- `HEAD` (`User-Agent: NSPlayer/10.1`, `Connection: close`), then `GET` (`User-Agent: Streamium/1.0`,
  keep-alive), both HTTP/1.1. Never sends `Range` or `Icy-MetaData`, so ICY metadata is never requested. ✔

| Response framing | Result |
|---|---|
| `Transfer-Encoding: chunked` | ✔ |
| exact `Content-Length` | ✔ |
| oversized `Content-Length` (12 h of audio, up to 7.6 GB, beyond 32 bit) | ✔ |
| no length and not chunked (close-delimited) | ✘ Play is answered in about 0.5 s, then nothing plays |
| status line `ICY 200 OK` (SHOUTcast v1 style), otherwise chunked | ✘ same: Play answered in 0.6 s, then nothing plays |

### 4.5 Remote URLs, HTTPS, redirects, internet radio
Remote (internet) URLs are **not** blocked as such: a plain remote HTTP file plays. They fail for
three independent reasons. ✔

| URL | Result |
|---|---|
| local file by IP | ✔ |
| **remote HTTP file by host name** (Google Storage MP3, `Content-Length`, no redirect) | ✔ plays (Play reply 2.1 s); DNS via the router works |
| same remote file over **HTTPS** | ✘ Play answered in 0.7 s, then `STOPPED` |
| local URL answering **`302 Found`** → local file | ✘ the renderer sends only `HEAD`, **does not follow the redirect**; Play fails after 30 s (501) |
| local URL answering `302` → remote HTTP file | ✘ same |
| Icecast radio over HTTP (NPO Radio 1, `Server: Icecast 2.4`, `audio/mpeg`, no length, not chunked) | ✘ Play answered in 1.7 s, then `STOPPED` |
| radio behind an HTTP 302 redirect (Deutschlandfunk) | ✘ same |
| same Icecast radio over **HTTPS** | ✘ Play fails after 30 s (501) |
| **Same Icecast stream relayed by a local proxy re-framed as `chunked`** | ✔ plays (Play reply 4.0 s) |
| Same stream relayed unchanged (close-delimited) | ✘ Play answered in 0.7 s, then `STOPPED` |

So remote URLs fail because of (1) **no HTTPS**, (2) **redirects are not followed** (this includes
every `http://` → `https://` upgrade and the redirecting entry URLs of many radio stations and CDNs) and,
for internet radio, (3) the **close-delimited HTTP framing used by Icecast/SHOUTcast servers** (and the
`ICY` status line of SHOUTcast v1). Neither the remote address nor the stream content is the problem. ✔ This matches the A9 page ("need a proxy to access online content") [11] and a 2013
BubbleUPnP report: an AW5000 user got no sound from SHOUTcast even with "Force Shoutcast Proxy" and an
"action timeout" on Play; the developer suggested waiting 30–60 s [15]. Both fit the blocking Play
and the start thresholds (§4.2). Controllers that serve their own chunked stream (e.g. Music Assistant)
are unaffected.

History: no source indicates that HTTPS or redirects ever worked via DLNA on these devices. Reports
from 2013 already required a proxy for online content [11][15]; the 2013 firmware notes (H4.95S–H5.63S)
mention only built-in internet radio and app fixes [4]; BubbleUPnP later added its own handling for
"http requests redirecting to https" (search summary only) [18]. Likely explanation (⚠ inference): the
renderer never handled them; plain-HTTP remote URLs, common in 2013, worked, and fail today mostly
because services moved to HTTPS and redirecting entry URLs.

---

## 5. Format support (UPnP streaming, A5; A3 identical where tested)

Each cell gives the result and how it was verified: read rate (machine) and/or by ear.

| Format | 32 kHz | 44.1 / 48 kHz | 88.2 kHz | 96 kHz | 24-bit |
|---|---|---|---|---|---|
| MP3 (MPEG-1 Layer III) | ✔ | ✔ (also mono, VBR, 32–320 kbit/s) | n/a | n/a | n/a |
| MP3 (MPEG-2/2.5: 8, 16, 22.05, 24 kHz) | ✔ all four (machine) | | | | |
| WMA (Std) | ✔ (by ear) | ✔ 44.1 | n/a | n/a | n/a |
| AAC ADTS | ✔ | ✔ | ✔ (by ear ◐) | ✘ (B) | n/a |
| AAC in MP4 | | ✔ 44.1 | | ✔ (by ear ◐) | n/a |
| HE-AAC v1 (SBR), ADTS | | ✔ (by ear) | | | n/a |
| HE-AAC v2 / eAAC+ (SBR + PS), ADTS or MP4 | | ✔ (by ear); fails as a 32 kbit/s live stream (§4.2) | | | n/a |
| ALAC in MP4 | ✔ | ✔ 44.1 | | ◐ by ear once "maybe faster", once "normal" | ✘ plays **distorted** (44.1, 48) |
| FLAC | ✔ | ✔ | ✘ (B) | ✘ (B); also 176.4/192 | ✘ |
| WAV (PCM) | ✔ | ✔ (also mono; correct pitch) | ✘ (B) | ✘ (B); also 176.4/192 | ✘ (B) at 44.1 and 48 |
| L16 raw PCM, stereo | | ✔ 44.1; **48 kHz plays at 44.1** (C) | | | |
| L16 raw PCM, **mono** | | **played as stereo** (D) | | | |
| L24 raw PCM | | ✘ rejected (714) | | | |
| OGG Vorbis, Opus | | ✘ (E) | | | |

- **(B)** Play is accepted after about 1.5 s; the renderer reads about 1 MB, closes the connection
  and stays `STOPPED`. Nothing is reported to the controller. ✔
- **(C)** L16 at 48 kHz plays about 8 % too slow and low, whatever the signalling: rate in the MIME
  type, DIDL `sampleFrequency`, with or without `DLNA.ORG_PN=LPCM`. Verified by ear (A5 and A3) and
  by read rate. Use WAV for 48 kHz PCM. ✔
- **(D)** `audio/L16;rate=…;channels=1` is played as stereo: ×2 speed (one octave up) at 44.1 kHz,
  ×1.84 at 48 kHz. Verified by ear and read rate (2.05). Mono WAV plays correctly. ✔
- **(E)** `audio/ogg` is rejected by SetAVTransportURI (714). As `application/octet-stream` it is
  fetched, then Play fails after 30 s (501). Aggregator spec pages list OGG for the AW3000/AW5000 [13];
  Philips' own documents don't [1][2]; the sink protocol list contains no OGG type; a user report on the
  A9 also says no OGG Vorbis [11]. ✔
- WAV headers with placeholder sizes (`0xFFFFFFFF` as ffmpeg writes to a pipe, or `0`) work. ✔
- Three runs (WMA 32 kHz, MP3 VBR, MP3 32 kbit/s) once stopped by themselves 6–16 s after starting.
  None of six repeats with the test host prevented from sleeping showed it, so this is probably a
  test-setup artefact. ◐

### MIME types
| Declared type | Result |
|---|---|
| `audio/mpeg`, `audio/mp3` | ✔ |
| `audio/vnd.dlna.adts`, `audio/aac`, `audio/x-aac`, `audio/aacp` (AAC-LC and HE-AAC v2) | ✔ (a short crack at playback start with `audio/x-aac` and `audio/aacp`, ◐ by ear once) |
| `audio/mp4`, `audio/x-m4a` | ✔ |
| `audio/x-ms-wma`, `audio/wma`, `audio/asf` | ✔ |
| `audio/x-wav`, `audio/wav` (the latter not advertised) | ✔ |
| `audio/x-flac` | ✔ |
| `audio/flac`, `audio/L24`, `audio/ogg` | ✘ SetAVTransportURI → **UPnP error 714** |

### Playlists (advertised in the sink protocol list)
The UPnP renderer **does not resolve playlists**. Every playlist pointed to a working MP3 stream on
the same server; that inner URL was never requested in any variant. ✔

| Variant | Renderer behaviour | Result |
|---|---|---|
| M3U as `audio/x-mpegurl` or `audio/mpegurl`; with or without DLNA headers; DIDL protocolInfo `audio/mpeg`; `upnp:class` `object.item.audioItem.audioBroadcast` or `object.item.playlistItem`; plain M3U without `#EXTM3U`, CRLF | `HEAD` only, playlist never fetched | ✘ Play fails after 30 s (501) |
| M3U served and declared as `audio/mpeg` | fetches the playlist (`GET`) but treats the text as MP3 data | ✘ Play answered in 0.5 s, nothing plays |
| M3U as `application/octet-stream` | `HEAD` only | ✘ 501 |
| ASX `application/asx`, WAX `audio/x-ms-wax` | `HEAD` only | ✘ 501 |
| ASX as `video/x-ms-asf` | – | ✘ rejected (714) |
| PLS `audio/x-scpls` (not advertised, yet not rejected) | `HEAD` only | ✘ 501 |

A media-server device profile reportedly flags Philips Streamium audio renderers with `AV:no-m3u` [14].
Users reporting working M3U radio most likely had the playlist resolved by the controller (e.g.
BubbleUPnP's proxy [15], or Music Assistant's radio providers) or used the device's built-in internet
radio. ⚠ Not verified. Controllers should resolve playlists and send the stream URL itself.

**Home Assistant (`dlna_dmr`)**: `media_content_type` only selects the DIDL `upnp:class`
(`playlist` → `object.item.playlistItem`, `music` → `object.item.audioItem.musicTrack`). The MIME type
comes from a `HEAD` request to the URL [16]. A *direct* stream URL therefore plays with either type
(tested: MP3 with `playlistItem` class ✔); an M3U URL fails with either (✔).

Not tested: MMS / `application/x-mms-framed`, `application/sdp`, `application/x-msmetafile`,
`application/vnd.ms.wms-hdr.asfv1`; WMA Pro/Lossless (no encoder available); WMA at 48 kHz; ALAC
48 kHz/16-bit and 88.2 kHz; mono AAC; HE-AAC at higher bitrates or via internet-radio servers with an
initial burst. HE-AAC files were encoded with macOS AudioToolbox (`ffmpeg -c:a aac_at -profile:a 4|28`).

**Usable lossless:** WAV, FLAC and ALAC at 16-bit / ≤ 48 kHz. WAV starts fastest, FLAC slowest (§4.2).

---

## 6. Vendor HTTP API (port 8889)

This is the backend of the built-in web UI. All calls are `GET`. Responses are either **JavaScript
object literals** (single quotes, some keys unquoted, not valid JSON) or HTML pages from which values
must be scraped. Settings writes always return the generic settings HTML with HTTP 200, so **check
success by reading the value back**.

### 6.1 Status (read-only) ✔
| Endpoint | Response |
|---|---|
| `/HOMESTATUS` | `{'command':'STANDBY','value':0\|1,playstate:0}`: `value` 0 = on, 1 = standby; `playstate` stays 0 during DLNA playback. Keeps answering (10–205 ms) while UPnP is blocked, so it works as an independent liveness check. |
| `/ELAPSE` | playing/paused: `{'command':'ELAPSE','value':<position s>,'mute':0,'shuffle':0,'repeat':0,'play':<1 playing, 2 paused>,'volume':<0–64>,'TotalTime':<s, 65535 = unknown>,'favstatus':0}`; idle: `{'command':'NOTHING','value':0}`; after stop: `{'command':'BACK','status':'SUCCESS',…}` |
| `/DeviceCapabilities.json`, `/DeviceStatus.json` | identical: firmware, source list with switch paths, standby entry (same on A3 and A5) |
| `/settings$14$02$01$0` | Wi-Fi page; embedded `{…,'current':<Wi-Fi channel>,'strength':<0–100?>}` (scale ⚠) |
| `/settings$08$02$01$0` | device info page: type number, name, firmware, IP setup, SSID, encryption, channel, MAC, **product registration ID**. Sensitive, don't publish. |

### 6.2 Sound: bass and treble ✔ (tested on A5, same page on A3)
| Action | Request |
|---|---|
| set bass | `/settings$04$03$01$1$B<level>` |
| set treble | `/settings$04$03$01$1$T<level>` |
| read both | `/soundsettings`, then parse `var bLevel = (100*<n>)/20` and `var tLevel = (100*<n>)/20` |

`<level>` is **0–20**, with 10 as the neutral centre mark. Changes apply immediately.
Example: `curl 'http://<ip>:8889/settings$04$03$01$1$B12'` (quote the `$`).

### 6.3 Further settings (paths from the web UI; ⚠ not written in tests)
| Setting | Request |
|---|---|
| shuffle off/on | `/settings$02$03$01$1$0` / `…$1` |
| repeat menu | `/settings$02$03$02$0` |
| device name, presets | `/settings$11$03$0N$1` (N = 1…4: PhilipsAW5000, Living_Room, Kitchen, Bedroom) |
| device name, free text | `/settings$11$03$05$1$<name>`: the UI keeps `[A-Za-z0-9]`, percent-encodes the rest, < 64 characters |
| language | `/settings$13$03$NN$1` (00 English, 01 German, 02 French, 03 Italian, 04 Spanish, 05 Dutch, 06 Portuguese, 07 Swedish, 08 Russian, 09/10 Chinese) |
| standby | `/CTRL$STANDBY` **toggles**; check `/HOMESTATUS` first [5] |
| volume | `/VOLUME$VAL$<0–64>` [5] |
| source | TuneIn `/nav$03$01$001$0`; legacy partner services `/nav$09$02$00{1..4}$0` (probably defunct); analogue input `/mp3link` |

Menus not touched: Registration, Firmware upgrade, **Factory reset** (`/settings$09$…`).
The settings menu has **no auto-standby / sleep-timer entry** on A3 or A5. ✔

---

## 7. Robustness

None of these produced a lock-up. ✔

| Load pattern | Result |
|---|---|
| Polling at 1.0 → 0.1 s and back-to-back; 2/4/8 parallel connections (up to about 55 req/s, 5,219 requests) | 0 errors, median latency 23–103 ms |
| 100 ms state polling during playback (UPnP + :8889) | no effect |
| Two GENA subscribers that accept the NOTIFY connection but never answer | no effect |
| Play abandoned after 5 s with an immediate Stop / SetAVTransportURI / Play retry, 5 rounds | Stop is blocked as well; recovers on its own |
| Standby → wake with controller-style retries and parallel polls; first play after power-on | slow, but recovers on its own |

- Field lock-ups (the speaker stays unusable until power-cycled) **remain unexplained**.
- A blocked device (pending Play or wake-up) can be told apart from a hung one: UPnP stays silent for
  at most about 30 s while ping, TCP 49153 and `/HOMESTATUS` keep answering. Silence beyond about 45 s
  is a hang candidate. ⚠ The threshold is derived, not taken from a captured hang.

---

## 8. Notes for controller implementers

| Do | Because |
|---|---|
| Allow **≥ 35 s** for `Play` (and for `Stop` sent shortly after a Play) | §4.2, §4.3. Music Assistant's DLNA provider uses async_upnp_client's default of **5 s** [7][8] and fails starts, especially for live sources with only a 0.5 s opening burst [9] |
| Send a few seconds of audio ahead of real time at stream start | shortens the start from about 6 s to about 1.5 s |
| Use one request at a time, without keep-alive | §4.1 |
| Stream MP3, or 16-bit WAV ≤ 48 kHz, chunked | §4.4, §5 |
| Never send 24-bit, FLAC/WAV above 48 kHz, L16 at 48 kHz, mono L16, `audio/flac`, `audio/L24` or OGG; resolve playlists (M3U/PLS/ASX) before sending | §5 |
| Keep live streams at ≥ 128 kbit/s, or send a large head start | low bitrates reach the start threshold too late or never (§4.2) |
| To get a working Pause, put `DLNA.ORG_OP=01` into the DIDL `protocolInfo`, and keep the HTTP stream open (and buffered) while paused; don't use Seek | §4.2 |
| Hand the renderer a final `http://` URL (resolve redirects, no HTTPS) or proxy the stream | §4.5 |
| Listen to SSDP NOTIFY rather than relying on M-SEARCH only | §3; Music Assistant searches every 300 s only [10] |

---

## 9. Open questions
- Cause of the field lock-ups, and whether software recovery works (candidates: UPnP Stop, `/mp3link`,
  standby off/on). `tools/radio_diag.sh` captures the state and tries those steps.
- A1, A2 and A9: untested. A1/A2 likely behave like A3/A5 as renderers; firmware H5.10S was released jointly for AW9000, AW5000, AW3000,
  AW2000 and AW1000 (release notes, via search summary) [4]. ⚠
  The A9 (AW9000) appears to differ [11].
- The 30 s wake stall (seen once on 29 Sep).
- ALAC at 96 kHz: correct speed or not (by ear once "maybe faster", once "normal").
- The occasional self-stops after 6–16 s (§5): test-setup artefact or device behaviour.
- The UPnP volume range (the SCPD was not read).

---

## Requirements
- **Python ≥ 3.10** with **aiohttp** (`requirements.txt`); `fidelio_discover.py` needs only the standard library.
- **ffmpeg** to generate test signals: `libmp3lame` (MP3), `libopus` (Opus), built-in FLAC/ALAC/AAC/WMA/Vorbis
  encoders; HE-AAC v1/v2 uses the macOS AudioToolbox encoder (`aac_at`), so only on macOS.
- The test host must be on the speakers' LAN segment (SSDP multicast, speaker fetches streams from it)
  and must not sleep during runs (`caffeinate -i` on macOS).
- `tools/radio_diag.sh` (Home Assistant terminal add-on): bash ≥ 4, `curl`, `jq`, `ha` CLI, `timeout`,
  `ping`; `python3` optional (discovery).

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/fidelio_discover.py --save tools/radios.conf
.venv/bin/python tools/dlna_probe.py --radio <name> --format mp3:44100
```

## Tools

### Finding your speakers (no addresses in the code)
All tools take `--radio NAME` (the speaker's friendly name) or `--ip ADDRESS`; the small test scripts
take a name or an address as their first argument. Resolution order:
1. an explicit address;
2. environment variable `FIDELIO_RADIOS="Kitchen=192.0.2.10,Bedroom=192.0.2.11"`;
3. a config file with `name address` lines: `$FIDELIO_RADIOS_FILE`, `./radios.conf` or `tools/radios.conf`
   (template: `radios.example.conf`; ignored by `.gitignore`);
4. SSDP discovery (active search plus the speakers' ~50 s announcements, matched by the `PhilipsIntelSDK`
   server header and a Philips `AW…` UPC). Takes up to 60 s.

`python tools/fidelio_discover.py --save tools/radios.conf` lists the speakers and writes the config file.
`tools/radio_diag.sh` keeps its own list in `/homeassistant/radio_diag.conf`: it is refreshed by a short
discovery on every run (`--discover` for a full 60 s, `--no-discover` to skip), and entries not seen
are kept, so a hung speaker that stopped announcing itself is still diagnosed with its last known address.
`tools/check_private_data.sh` refuses private IPs, MAC addresses and tokens (usable as a git pre-commit hook).
Examples in this repository use documentation addresses (RFC 5737, `192.0.2.x`).

### Tool list
- `tools/standby_study.py`: auto-standby timing and wake-up latency in cycles (baseline, then W minutes in standby).
- `tools/standby_timer_test.py`: does a command during idle restart the auto-standby timer (test vs. control speaker).
- `tools/fidelio_discover.py`: speaker discovery and name → address resolution used by all tools.
- `tools/dlna_probe.py`: DLNA controller and test media server. Formats (incl. ALAC, L24, HE-AAC, OGG Vorbis, Opus, mono, VBR, playlists), framing,
  pacing, delay, DIDL/MIME variants, read-rate pitch check, `/HOMESTATUS` polling, stack dumps on `SIGUSR1` or when stalled.
- `tools/hang_hunt.py`: staged load and lock-up hunt with diagnosis and a recovery ladder.
- `tools/pause_test.py`: Play → Pause → Resume → Seek → Stop sequence with 100 ms state polling (format spec incl. `seek=1` or `piop=1`).
- `tools/verify_pfa9.py`: checks volume via UPnP, GENA behaviour, URL change without Stop and direct internet-radio playback.
- `tools/remote_test.py`: local vs. remote, HTTP vs. HTTPS and redirect URLs.
- `tools/relay_test.py`: relays a real internet-radio stream to the renderer, re-framed chunked or unchanged.
- `tools/radio_diag.sh`: Home Assistant terminal script to run when a speaker hangs. It diagnoses all
  channels, collects HA and Music Assistant logs, and attempts recovery.

## References
1. Philips AW5000/10 product leaflet — <https://www.documents.philips.com/assets/20231128/71e02c3cd6fc474ea9f3b0c800795d86.pdf>
2. Philips FAQ "What music files does my Philips speaker support?" (AW1000/AW2000/AW5000) — <https://www.philips.co.uk/c-f/XC000011697/what-music-files-does-my-philips-speaker-support>
3. Philips FAQ "Is FLAC supported?" (Streamium MCi models) — <https://www.philips.sa/en/c-f/XC000009406/is-flac-file-format-supported-on-my-philips-device>
4. Toengels Philips Blog, Fidelio AW-series firmware releases (H4.95S–H5.63S, 2013) — <https://toengel.net/philipsblog/2013/03/13/philips-neue-firmware-h5-10s-fr-fidelio-aw-serie/>, <https://toengel.net/philipsblog/2013/10/17/philips-neue-firmware-version-h5-63s-fr-fidelio-aw-serie/>
5. homebridge-fidelio (A1/A3/A5/A9 control via :8889) — <https://github.com/mlaurijsse/homebridge-fidelio>
6. Serviio forum, Philips NP2500 needs `audio/x-flac` — <https://forum.serviio.org/viewtopic.php?f=27&t=9049>
7. async_upnp_client, default request timeout 5 s — <https://github.com/StevenLooman/async_upnp_client/blob/development/async_upnp_client/aiohttp.py>
8. Music Assistant DLNA provider — <https://github.com/music-assistant/server/tree/dev/music_assistant/providers/dlna>
9. Music Assistant stream pacing profiles — <https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/streams/constants.py>
10. Music Assistant SSDP discovery, `UPNP_DISCOVERY_INTERVAL = 300` — <https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/discovery/controller.py>
11. "Philips Fidelio HiFi Wireless A9 as a UPnP/DLNA Renderer" — <http://fabrice.gadaud.org/PFA9/>
12. Philips AW5000 user manual — <https://www.documents.philips.com/assets/20231126/c172b09f3cda4d45b0f6b0c600c17f91.pdf>
13. Aggregator spec summaries listing OGG, e.g. AW3000 — <https://www.manua.ls/philips/fidelio-aw3000/specifications>
14. MediaTomb discussion "Transcode profile per device" (Streamium `AV:no-m3u`; seen via search summary only) — <https://sourceforge.net/p/mediatomb/discussion/440750/thread/a717cf1c/>
15. BubbleUPnP thread, page 155 (April 2013): AW5000 user, "Force Shoutcast Proxy", "action timeout"; developer reply — <https://forum.xda-developers.com/t/app-5-0-v3-7-3-1-bubbleupnp-upnp-dlna-chromecast-control-point-and-renderer.1118891/page-155>
16. Home Assistant `dlna_dmr` (`media_player.py`, `const.py` `MEDIA_UPNP_CLASS_MAP`) and async_upnp_client `construct_play_media_metadata` — <https://github.com/home-assistant/core/tree/dev/homeassistant/components/dlna_dmr>, <https://github.com/StevenLooman/async_upnp_client/blob/development/async_upnp_client/profiles/dlna.py>
17. Music Assistant DIDL metadata (`create_didl_metadata`) — <https://github.com/music-assistant/server/blob/dev/music_assistant/helpers/upnp.py>
18. BubbleUPnP XDA thread page 156 (fix for http→https redirects; Philips AW5000 not entering PLAYING; search summary only) — <https://forum.xda-developers.com/t/app-5-0-v3-5-8-bubbleupnp-upnp-dlna-chromecast-control-point-and-renderer.1118891/page-156>
19. Philips Fidelio A1 wireless Hi-Fi link AW1000/10 — <https://www.philips.co.uk/c-p/AW1000_10/fidelio-a1-wireless-hi-fi-link>
20. Philips Fidelio A2 wireless Hi-Fi receiver AW2000/10 — <https://www.philips.co.uk/c-p/AW2000_10/fidelio-a2-wireless-hi-fi-receiver>
21. What Hi-Fi, IFA 2012: Philips expands Fidelio wireless hi-fi range (A1/A2) — <https://www.whathifi.com/news/ifa-2012-philips-expands-fidelio-wireless-hi-fi-range>

Music Assistant code references reflect the `dev` branch as of October 2026.

## License
- Code in `tools/`: [MIT](LICENSE).
- Documentation (this README and other text): [CC BY 4.0](LICENSE-docs). Attribution: "philips-fidelio-a-upnp-notes" with a link to this repository.
- Philips, Fidelio and Streamium are trademarks of their respective owners; this project is not affiliated with or endorsed by them.

