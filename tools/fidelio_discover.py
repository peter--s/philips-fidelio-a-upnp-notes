#!/usr/bin/env python3
"""Find Philips Fidelio AW-series speakers on the LAN and resolve a radio name to an address.

Resolution order used by all tools (first match wins):
  1. an explicit address (--ip, or an IPv4 address given where a radio is expected)
  2. environment variable FIDELIO_RADIOS, e.g.  FIDELIO_RADIOS="Kitchen=192.0.2.10,Bedroom=192.0.2.11"
  3. a config file with "name address" lines: $FIDELIO_RADIOS_FILE, ./radios.conf, or
     radios.conf next to this script (never committed, see .gitignore)
  4. SSDP discovery: active M-SEARCH plus passive listening for the speakers' ~50 s announcements

  python fidelio_discover.py                    list speakers (discovery, up to 60 s)
  python fidelio_discover.py --save radios.conf  ...and write them to a config file
"""
import argparse
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

SSDP = ("239.255.255.250", 1900)
SEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 3\r\n"
          "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
SIGNATURE = "PhilipsIntelSDK"   # SSDP SERVER header of the Fidelio AW series


def local_ip() -> str:
    """Address of the interface that routes to the LAN multicast group."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(SSDP)
        return s.getsockname()[0]


def describe(location: str) -> dict | None:
    """Fetch a device description; return identity if it is a Fidelio AW speaker."""
    try:
        xml = urllib.request.urlopen(location, timeout=10).read().decode("utf-8", "replace")
    except OSError:
        return None

    def tag(name):
        m = re.search(rf"<(?:\w+:)?{name}>([^<]*)</", xml)
        return m.group(1).strip() if m else ""
    upc = tag("UPC")
    if "Philips" not in tag("manufacturer") or not upc.startswith("AW"):
        return None
    return {"name": tag("friendlyName"), "ip": location.split("/")[2].split(":")[0], "model": upc,
            "firmware": tag("X_firmwareString"), "location": location}


def discover(timeout: float = 60.0, expect: int = 0, quiet: bool = True) -> list[dict]:
    """Active search first, then passive listening until `timeout` or `expect` speakers are found."""
    me = local_ip()
    found: dict[str, dict] = {}
    seen_locations: set[str] = set()

    def consider(data: bytes, src: str):
        text = data.decode("utf-8", "replace")
        if SIGNATURE not in text:
            return
        loc = next((line.split(":", 1)[1].strip() for line in text.split("\r\n")
                    if line.lower().startswith("location:")), "")
        if not loc or loc in seen_locations:
            return
        seen_locations.add(loc)
        info = describe(loc)
        if info and info["ip"] not in found:
            found[info["ip"]] = info
            if not quiet:
                print(f"  found {info['name']} ({info['model']}, {info['firmware']}) at {info['ip']}",
                      file=sys.stderr, flush=True)

    # active: search replies arrive as unicast to this socket
    act = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    act.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(me))
    act.bind((me, 0))
    act.settimeout(0.5)
    # passive: announcements arrive on the multicast group
    pas = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    pas.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        pas.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    pas.bind(("", 1900))
    pas.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                   struct.pack("4s4s", socket.inet_aton(SSDP[0]), socket.inet_aton(me)))
    pas.settimeout(0.5)
    end = time.time() + timeout
    next_search = 0.0
    try:
        while time.time() < end and not (expect and len(found) >= expect):
            if time.time() >= next_search:       # repeat the search a few times (replies are unreliable)
                act.sendto(SEARCH, SSDP)
                next_search = time.time() + 15
            for sock in (act, pas):
                try:
                    data, addr = sock.recvfrom(8192)
                    consider(data, addr[0])
                except socket.timeout:
                    pass
            # without an expected count, stop once the search phase is quiet and 55 s of
            # announcements (one full cycle) have been heard
            if not expect and found and time.time() > end - timeout + 55:
                break
    finally:
        act.close()
        pas.close()
    return sorted(found.values(), key=lambda r: r["name"].lower())


def read_config(path: Path) -> dict[str, str]:
    radios = {}
    if path and path.is_file():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name, _, ip = line.rpartition(" ")
                if name and ip:
                    radios[name.strip()] = ip.strip()
    return radios


def configured() -> dict[str, str]:
    """Radios from FIDELIO_RADIOS or the first config file found."""
    env = os.environ.get("FIDELIO_RADIOS", "")
    if env:
        return dict(item.split("=", 1) for item in env.split(",") if "=" in item)
    for cand in (os.environ.get("FIDELIO_RADIOS_FILE"), "radios.conf", Path(__file__).parent / "radios.conf"):
        if cand and Path(cand).is_file():
            return read_config(Path(cand))
    return {}


def is_ip(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def resolve(radio: str | None = None, ip: str | None = None) -> str:
    """Return the address for --ip / a radio name / the only speaker found. Exit with a hint otherwise."""
    if ip:
        return ip
    if radio and is_ip(radio):
        return radio
    conf = configured()
    if radio:
        for name, addr in conf.items():
            if name.lower() == radio.lower():
                return addr
    elif len(conf) == 1:
        return next(iter(conf.values()))
    print("discovering Fidelio speakers (up to 60 s)...", file=sys.stderr, flush=True)
    found = discover(quiet=False)
    if radio:
        for r in found:
            if r["name"].lower() == radio.lower():
                return r["ip"]
        sys.exit(f"radio '{radio}' not found; found: {', '.join(r['name'] for r in found) or 'none'}")
    if len(found) == 1:
        return found[0]["ip"]
    names = ", ".join(f"{r['name']}" for r in found) or "none"
    sys.exit(f"several or no speakers ({names}); choose one with --radio NAME or --ip ADDRESS")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--expect", type=int, default=0, help="stop as soon as this many speakers were found")
    p.add_argument("--save", help="write 'name address' lines to this config file")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    radios = discover(a.timeout, a.expect, quiet=False)
    if a.json:
        print(json.dumps(radios, indent=2))
    else:
        for r in radios:
            print(f"{r['name']:20} {r['ip']:15} {r['model']:10} {r['firmware']}")
    if a.save:
        Path(a.save).write_text("# name address  (generated by fidelio_discover.py; do not commit)\n"
                                + "".join(f"{r['name']} {r['ip']}\n" for r in radios))
        print(f"saved {len(radios)} radio(s) to {a.save}", file=sys.stderr)


if __name__ == "__main__":
    main()
