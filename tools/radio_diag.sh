#!/usr/bin/env bash
# radio_diag.sh - diagnose (and if hung, try to recover) the Philips Fidelio/Streamium
# DLNA radios, and collect HA + Music Assistant logs. Run in the Advanced SSH & Web Terminal.
#   /homeassistant/radio_diag.sh                  diagnose all, recover radios found HUNG, logs of last 30 min
#   /homeassistant/radio_diag.sh --minutes 90     longer log window
#   /homeassistant/radio_diag.sh --recover Kitchen force recovery attempts on the radio "Kitchen"
#   /homeassistant/radio_diag.sh --no-recover     diagnose only (read-only)
#   /homeassistant/radio_diag.sh --discover       full SSDP discovery (60 s) before diagnosing
#   /homeassistant/radio_diag.sh --capture 120    also record the radios' traffic for 120 s (tcpdump)
#   /homeassistant/radio_diag.sh --ha-debug on    switch HA debug logging for DLNA/UPnP on (or off), then diagnose
#   further options: --no-logs  --no-ssdp  --no-discover  --no-zip
# Radios: "name address" lines in $RADIO_DIAG_CONF (default /homeassistant/radio_diag.conf).
#   Created/updated automatically by discovery; entries not seen are kept, so a hung radio that no
#   longer announces itself is still diagnosed with its last known address.
# Results: /homeassistant/radio_diag/<date-time>/ (report.txt, logs, raw responses) and <date-time>.zip
# States: OK, STUCK (PLAYING with frozen position, LED off: a Stop was refused with UPnP error 701
#   while the stream connection stayed open; a second Stop clears it), UPNP-SILENT/HUNG, OFFLINE.

CONF=${RADIO_DIAG_CONF:-/homeassistant/radio_diag.conf}
RADIOS=()
MA_SLUG=d5369777_music_assistant
UPNP_PORT=49153
VENDOR_PORT=8889
BLOCK_GRACE=45   # UPnP normally blocks up to ~30 s (wake from long standby, failed stream)
MINUTES=30; RECOVER=1; FORCE=""; DO_LOGS=1; DO_SSDP=1; DISCOVER=quick; CAPTURE=0; HA_DEBUG=""; DO_ZIP=1
PATCH_SCRIPT=/homeassistant/ma_patch_pr327.sh

while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) MINUTES=$2; shift ;;
    --recover) RECOVER=1
               if [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then FORCE=$2; shift; fi ;;
    --no-recover) RECOVER=0 ;;
    --no-logs) DO_LOGS=0 ;;
    --no-ssdp) DO_SSDP=0 ;;
    --discover) DISCOVER=full ;;
    --no-discover) DISCOVER=none ;;
    --capture) CAPTURE=$2; shift ;;
    --ha-debug) HA_DEBUG=$2; shift ;;
    --no-zip) DO_ZIP=0 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done

OUT=/homeassistant/radio_diag/$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"
exec > >(tee -a "$OUT/report.txt") 2>&1

ts()  { date '+%H:%M:%S'; }
say() { echo "$(ts) $*"; }
rcdesc() {
  case $1 in
    0) echo ok ;; 7) echo "connect failed" ;; 28) echo TIMEOUT ;;
    52) echo "empty reply" ;; 56) echo "connection reset" ;; *) echo "curl error $1" ;;
  esac
}
redact() { sed -E 's#(https?://)[^/@ ]+@#\1***@#g'; }   # e.g. tokens in git URLs
ha_api() {  # path [json body -> POST]
  [ -n "${SUPERVISOR_TOKEN:-}" ] || return 1
  if [ -n "${2:-}" ]; then
    curl -s -m 15 -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H 'Content-Type: application/json' \
         -d "$2" "http://supervisor/core/api/$1"
  else
    curl -s -m 15 -H "Authorization: Bearer $SUPERVISOR_TOKEN" "http://supervisor/core/api/$1"
  fi
}
conns() {  # ip -> TCP connections between this host (HA, MA, ...) and the radio
  { ss -tn 2>/dev/null || netstat -tn 2>/dev/null; } | grep -E "[ :]$(echo "$1" | sed 's/\./\\./g'):[0-9]+( |$)" \
    | awk '{print "      " $0}'
}

# --- requests to the radios: one at a time, never with a timeout below 10 s ------------
soap() {  # ip service action [args]; sets SOAP_RC SOAP_CODE SOAP_TIME SOAP_BODY
  local ip=$1 svc=$2 action=$3 args=${4:-<InstanceID>0</InstanceID>} f w
  f=$(mktemp)
  w=$(curl -s -m 15 -o "$f" -w '%{http_code} %{time_total}' \
        -H 'Content-Type: text/xml; charset="utf-8"' \
        -H "SOAPACTION: \"urn:schemas-upnp-org:service:$svc:1#$action\"" \
        --data "<?xml version=\"1.0\" encoding=\"utf-8\"?><s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body><u:$action xmlns:u=\"urn:schemas-upnp-org:service:$svc:1\">$args</u:$action></s:Body></s:Envelope>" \
        "http://$ip:$UPNP_PORT/upnp/control/${svc}1")
  SOAP_RC=$?
  SOAP_CODE=${w%% *}; SOAP_TIME=${w##* }
  SOAP_BODY=$(tr -d '\r\n' < "$f"); rm -f "$f"
}
xml() { echo "$SOAP_BODY" | grep -o "<$1>[^<]*" | head -1 | cut -d'>' -f2; }
vendor() {  # ip path; sets VENDOR_RC VENDOR_BODY
  VENDOR_BODY=$(curl -s -m 15 "http://$1:$VENDOR_PORT/$2")
  VENDOR_RC=$?
  VENDOR_BODY=$(echo "$VENDOR_BODY" | tr -s ' \r\n\t' ' ')
}
tcp() {
  timeout 10 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
  case $? in 0) echo open ;; 124) echo TIMEOUT ;; *) echo "refused/unreachable" ;; esac
}

declare -A STATUS
diagnose() {  # name ip
  local n=$1 ip=$2 p d drc vs s1 st pos1
  say "=== $n ($ip)"
  if ping -c 3 -W 2 "$ip" >/dev/null 2>&1; then p=ok; else p=FAIL; fi
  say "  ping: $p   neighbour: $( (ip neigh show "$ip" 2>/dev/null || arp -n "$ip" 2>/dev/null) | head -1)"
  say "  tcp $UPNP_PORT: $(tcp "$ip" $UPNP_PORT)   tcp $VENDOR_PORT: $(tcp "$ip" $VENDOR_PORT)"
  sleep 1
  d=$(curl -s -m 15 -o "$OUT/$n-description.xml" -w '%{http_code} in %{time_total}s' "http://$ip:$UPNP_PORT/nmrDescription.xml")
  drc=$?
  if [ $drc = 0 ]; then
    say "  description: $d, fw $(grep -o 'X_firmwareString>[^<]*' "$OUT/$n-description.xml" | cut -d'>' -f2)"
  else
    say "  description: $(rcdesc $drc)"
  fi
  sleep 1
  vendor "$ip" HOMESTATUS; vs=$VENDOR_RC
  say "  vendor /HOMESTATUS: $([ $vs = 0 ] && echo "$VENDOR_BODY  (value 0=on, 1=standby)" || rcdesc $vs)"
  sleep 1
  vendor "$ip" ELAPSE
  say "  vendor /ELAPSE: $([ $VENDOR_RC = 0 ] && echo "$VENDOR_BODY" || rcdesc $VENDOR_RC)"
  sleep 1
  soap "$ip" AVTransport GetTransportInfo; s1=$SOAP_RC
  if [ $s1 = 0 ]; then
    st=$(xml CurrentTransportState)
    say "  UPnP GetTransportInfo: HTTP $SOAP_CODE in ${SOAP_TIME}s, state=$st status=$(xml CurrentTransportStatus)"
    sleep 1; soap "$ip" AVTransport GetMediaInfo
    say "  UPnP GetMediaInfo: uri=$(xml CurrentURI | sed 's/&amp;/\&/g')"
    sleep 1; soap "$ip" AVTransport GetPositionInfo
    pos1=$(xml RelTime)
    say "  UPnP GetPositionInfo: $pos1 / $(xml TrackDuration)"
    sleep 1; soap "$ip" RenderingControl GetVolume "<InstanceID>0</InstanceID><Channel>Master</Channel>"
    say "  UPnP GetVolume: $(xml CurrentVolume)"
    STATUS[$n]="OK"
    if [ "$st" = PLAYING ]; then
      sleep 4; soap "$ip" AVTransport GetPositionInfo
      if [ $SOAP_RC = 0 ] && [ "$(xml RelTime)" = "$pos1" ]; then
        STATUS[$n]="STUCK: PLAYING but position frozen at $pos1 (LED off? stalled stream / refused Stop)"
      else
        say "  position advances ($pos1 -> $(xml RelTime))"
      fi
    fi
  else
    say "  UPnP GetTransportInfo: $(rcdesc $s1) after ${SOAP_TIME}s"
    if [ $p = FAIL ] && [ $drc != 0 ] && [ $vs != 0 ]; then
      STATUS[$n]="OFFLINE (no ping, no HTTP) - powered off or off the network?"
    else
      STATUS[$n]="UPNP-SILENT"
    fi
  fi
  say "  TCP connections with this host:"
  conns "$ip" | tee "$OUT/$n-connections.txt"
  [ -s "$OUT/$n-connections.txt" ] || say "      none"
  say "  -> ${STATUS[$n]}"
}

recover_stuck() {  # name ip: a Stop refused with 701 leaves PLAYING; a second Stop clears it
  local n=$1 ip=$2 i
  say "=== RECOVERY (stuck) $n ($ip)"
  for i in 1 2 3; do
    soap "$ip" AVTransport Stop
    say "  Stop #$i: HTTP $SOAP_CODE in ${SOAP_TIME}s $(xml errorCode | sed 's/^./UPnP error &/')"
    sleep 2; soap "$ip" AVTransport GetTransportInfo
    if [ "$(xml CurrentTransportState)" != PLAYING ]; then
      say "  -> $(xml CurrentTransportState). The LED stays off until the stream connection is closed"
      say "     (by MA, e.g. when it starts the next stream); see the TCP connections above."
      return 0
    fi
  done
  return 1
}

recover() {  # name ip
  local n=$1 ip=$2
  say "=== RECOVERY $n ($ip)"
  say "  step 1: UPnP Stop"
  soap "$ip" AVTransport Stop; say "    Stop: $(rcdesc $SOAP_RC)"
  check_alive "$ip" 15 && return 0
  say "  step 2: vendor source switch to MP3-Link (:8889/mp3link)"
  vendor "$ip" mp3link; say "    /mp3link: $(rcdesc $VENDOR_RC)"
  check_alive "$ip" 20 && return 0
  say "  step 3: vendor standby off/on (:8889/CTRL\$STANDBY toggles)"
  vendor "$ip" HOMESTATUS
  if [ $VENDOR_RC = 0 ]; then
    if echo "$VENDOR_BODY" | tr -d ' ' | grep -q "'value':0"; then
      vendor "$ip" 'CTRL$STANDBY'; say "    to standby: $VENDOR_BODY"; sleep 15
    fi
    vendor "$ip" 'CTRL$STANDBY'; say "    to on: $VENDOR_BODY"
  else
    say "    vendor interface dead - skipped"
  fi
  check_alive "$ip" 30 && return 0
  say "  step 4: wait 120 s"
  check_alive "$ip" 120 && return 0
  say "  NOT recovered by software - power cycle $n"
  return 1
}
check_alive() {  # ip wait
  sleep "$2"
  soap "$1" AVTransport GetTransportInfo
  if [ $SOAP_RC = 0 ]; then
    say "    -> UPnP ALIVE again (state $(xml CurrentTransportState)); MA re-discovers within <=5 min"
    return 0
  fi
  say "    -> still silent ($(rcdesc $SOAP_RC))"
  return 1
}

# --- radios: config file + SSDP discovery (Fidelio AW: SERVER header contains PhilipsIntelSDK) ---
discover_radios() {  # seconds -> prints "name address" lines
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$1" <<'PY'
import re, socket, struct, sys, time, urllib.request
dur = float(sys.argv[1]); grp = ("239.255.255.250", 1900)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.connect(grp); me = s.getsockname()[0]
q = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 3\r\n"
     "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
act = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
act.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(me)); act.bind((me, 0)); act.settimeout(0.5)
socks = [act]
try:  # passive listener; may fail if another SSDP stack holds port 1900 exclusively
    pas = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pas.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        pas.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    pas.bind(("", 1900))
    pas.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(grp[0]), socket.inet_aton(me)))
    pas.settimeout(0.5)
    socks.append(pas)
except OSError as err:
    print(f"(passive SSDP listening unavailable: {err}; using search only)", file=sys.stderr)
seen, found, end, nxt = set(), {}, time.time() + dur, 0
while time.time() < end:
    if time.time() >= nxt:
        act.sendto(q, grp); nxt = time.time() + 15
    for sk in socks:
        try:
            d, a = sk.recvfrom(8192)
        except socket.timeout:
            continue
        t = d.decode("utf-8", "replace")
        if "PhilipsIntelSDK" not in t:
            continue
        loc = next((l.split(":", 1)[1].strip() for l in t.split("\r\n") if l.lower().startswith("location:")), "")
        if not loc or loc in seen:
            continue
        seen.add(loc)
        try:
            x = urllib.request.urlopen(loc, timeout=10).read().decode("utf-8", "replace")
        except OSError:
            continue
        name = re.search(r"<friendlyName>([^<]*)<", x); upc = re.search(r"<UPC>([^<]*)<", x)
        if name and upc and upc.group(1).startswith("AW"):
            found[loc.split("/")[2].split(":")[0]] = name.group(1).strip().replace(" ", "_")
for ip, name in sorted(found.items(), key=lambda kv: kv[1].lower()):
    print(name, ip)
PY
}

load_radios() {
  local seen=() line name ip found
  declare -A addr=()
  if [ -f "$CONF" ]; then
    while read -r name ip _; do
      [[ -z "$name" || "$name" == \#* ]] && continue
      addr[$name]=$ip
    done < "$CONF"
  fi
  if [ "$DISCOVER" != none ]; then
    local secs=8; [ "$DISCOVER" = full ] && secs=60; [ ${#addr[@]} -eq 0 ] && secs=60
    say "discovering Fidelio speakers (${secs} s)..."
    command -v python3 >/dev/null 2>&1 || say "  python3 not available - discovery skipped"
    found=$(discover_radios $secs)
    while read -r name ip; do
      [ -n "$name" ] || continue
      if [ -z "${addr[$name]:-}" ]; then say "  found $name -> $ip"
      elif [ "${addr[$name]}" != "$ip" ]; then say "  updated $name: ${addr[$name]} -> $ip"; fi
      addr[$name]=$ip
    done <<< "$found"
    { echo "# name address - written by radio_diag.sh; entries not seen are kept as last known"
      for name in "${!addr[@]}"; do echo "$name ${addr[$name]}"; done | sort; } > "$CONF"
  fi
  for name in $(printf '%s\n' "${!addr[@]}" | sort); do RADIOS+=("$name ${addr[$name]}"); done
  if [ ${#RADIOS[@]} -eq 0 ]; then
    say "no radios known: create $CONF with 'name address' lines or run with --discover"; exit 1
  fi
  say "radios: ${RADIOS[*]}  (from $CONF)"
}

# ======================================================================================
say "radio_diag starting, output in $OUT"
load_radios
say "HA core: $(ha core info --raw-json 2>/dev/null | jq -r '.data.version // "?"')   MA app: $(ha apps info $MA_SLUG --raw-json 2>/dev/null | jq -c '.data | {version, state, watchdog}' 2>/dev/null)"
say "host: $(uptime)"
if [ -n "$HA_DEBUG" ]; then
  lvl=warning; [ "$HA_DEBUG" = on ] && lvl=debug
  if ha_api services/logger/set_level "{\"homeassistant.components.dlna_dmr\": \"$lvl\", \"async_upnp_client\": \"$lvl\", \"homeassistant.components.ssdp\": \"$lvl\"}" >/dev/null; then
    say "HA debug logging for dlna_dmr / async_upnp_client / ssdp set to $lvl (until HA restarts)"
  else
    say "could not set HA log levels (no SUPERVISOR_TOKEN, or the logger integration is not loaded)"
  fi
fi
if [ "$CAPTURE" -gt 0 ] 2>/dev/null; then
  if command -v tcpdump >/dev/null 2>&1; then
    filt=$(for r in "${RADIOS[@]}"; do set -- $r; printf 'host %s or ' "$2"; done); filt=${filt% or }
    timeout "$CAPTURE" tcpdump -i any -w "$OUT/radios.pcap" $filt >/dev/null 2>&1 &
    CAP_PID=$!
    say "capturing the radios' traffic for $CAPTURE s -> radios.pcap"
  else
    say "tcpdump not available - capture skipped"
  fi
fi
if [ -x "$PATCH_SCRIPT" ] && command -v docker >/dev/null 2>&1; then
  for t in ma ha; do
    "$PATCH_SCRIPT" status $t > "$OUT/patch_pr327_$t.txt" 2>&1 < /dev/null
    say "async_upnp_client #327 patch ($t): $(grep -m1 'async_upnp_client [0-9]' "$OUT/patch_pr327_$t.txt" | cut -d' ' -f2)," \
        "$(tail -n 1 "$OUT/patch_pr327_$t.txt")"
  done
fi
free -m 2>/dev/null | sed 's/^/    /'

say "--- HA media_player entities named Radio*"
ha_api states | jq -r '.[] | select(.entity_id | startswith("media_player."))
  | select((.attributes.friendly_name // "") | test("radio"; "i"))
  | "    \(.entity_id)\t\(.state)\t\(.attributes.friendly_name)"' 2>/dev/null \
  || say "    (no SUPERVISOR_TOKEN / HA API not reachable)"

for r in "${RADIOS[@]}"; do set -- $r; diagnose "$1" "$2"; sleep 2; done

silent=()
for r in "${RADIOS[@]}"; do set -- $r; [ "${STATUS[$1]}" = UPNP-SILENT ] && silent+=("$r"); done
if [ ${#silent[@]} -gt 0 ]; then
  say "UPnP silent on: ${silent[*]} - waiting ${BLOCK_GRACE}s (normal blocks last up to ~30 s), then re-checking"
  sleep $BLOCK_GRACE
  for r in "${silent[@]}"; do
    set -- $r
    soap "$2" AVTransport GetTransportInfo
    if [ $SOAP_RC = 0 ]; then
      STATUS[$1]="OK after a temporary block (state $(xml CurrentTransportState))"
    else
      vendor "$2" HOMESTATUS
      if [ $VENDOR_RC = 0 ]; then STATUS[$1]="HUNG: UPnP dead, vendor :8889 alive ($VENDOR_BODY)"
      else STATUS[$1]="HUNG: UPnP and vendor :8889 dead"; fi
    fi
    say "$1 -> ${STATUS[$1]}"
  done
fi

if [ $DO_SSDP = 1 ] && command -v python3 >/dev/null 2>&1; then
  say "--- listening 70 s for the radios' SSDP announcements (passive)"
  python3 - "${RADIOS[@]}" <<'PY'
import socket, struct, sys, time
radios = {r.split()[1]: r.split()[0] for r in sys.argv[1:]}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
except (AttributeError, OSError):
    pass
s.bind(("", 1900))
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
             struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton("0.0.0.0")))
s.settimeout(1)
end = time.time() + 70
seen = {ip: [] for ip in radios}
while time.time() < end:
    try:
        d, a = s.recvfrom(8192)
    except socket.timeout:
        continue
    if a[0] in radios and d.startswith(b"NOTIFY"):
        kind = "byebye" if b"ssdp:byebye" in d else "alive"
        if not seen[a[0]] or time.time() - seen[a[0]][-1][0] > 3:
            seen[a[0]].append((time.time(), kind))
for ip, ev in seen.items():
    txt = ", ".join(f"{time.strftime('%H:%M:%S', time.localtime(t))} {k}" for t, k in ev)
    print(f"    {radios[ip]:8} {len(ev)} announcement(s): {txt or 'NONE (normally one per ~50 s)'}")
PY
elif [ $DO_SSDP = 1 ]; then
  say "--- python3 not available, SSDP check skipped"
fi

if [ $RECOVER = 1 ]; then
  for r in "${RADIOS[@]}"; do
    set -- $r
    if [ -z "$FORCE" ] && [ "${STATUS[$1]#STUCK}" != "${STATUS[$1]}" ]; then
      if recover_stuck "$1" "$2"; then STATUS[$1]="${STATUS[$1]} -> cleared by a second Stop"
      else STATUS[$1]="${STATUS[$1]} -> still PLAYING after 3 Stops"; fi
      continue
    fi
    if [ -n "$FORCE" ]; then [ "$1" = "$FORCE" ] || continue
    else case "${STATUS[$1]}" in HUNG*) ;; *) continue ;; esac; fi
    if recover "$1" "$2"; then STATUS[$1]="${STATUS[$1]} -> RECOVERED by software"
    else STATUS[$1]="${STATUS[$1]} -> NOT recovered, power cycle needed"; fi
  done
fi

if [ $DO_LOGS = 1 ]; then
  tz=$(ha_api config 2>/dev/null | jq -r '.time_zone // empty' 2>/dev/null)
  FROM=$(TZ=${tz:-${TZ:-UTC}} date -d "@$(( $(date +%s) - MINUTES * 60 ))" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
  say "--- collecting logs since $FROM (${tz:-container time zone})"
  win() {
    sed 's/\x1b\[[0-9;]*m//g' | awk -v f="$FROM" '
      /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:/ { ts = $1 " " $2 }
      ts >= f'
  }
  pat='dlna|ssdp|upnp|F00DBABE'
  for r in "${RADIOS[@]}"; do set -- $r; pat="$pat|$1|$(echo "$2" | sed 's/\./\\./g')"; done
  ha core logs -n 50000 2>&1 | win | redact > "$OUT/ha_core.log"
  grep -iE "$pat" "$OUT/ha_core.log" > "$OUT/ha_core_radios.log"
  ha apps logs $MA_SLUG -n 50000 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | redact > "$OUT/music_assistant_full.log"
  win < "$OUT/music_assistant_full.log" > "$OUT/music_assistant.log"
  grep -iE "$pat|play_media|Flow stream|unavailable|not available|error|Spotify Connect|librespot|stalled|Stopping player|took [0-9.]+ seconds" \
    "$OUT/music_assistant.log" > "$OUT/music_assistant_radios.log"
  lv=$(grep -oE '\[music_assistant\.[^]]+\] Log level configured to [A-Z]+' "$OUT/music_assistant_full.log" | sort -u \
       | sed -E 's/\[music_assistant\.([^]]+)\] Log level configured to /\1=/' | tr '\n' ' ')
  say "    MA providers with a non-default log level (since MA start): ${lv:-none}"
  echo "$lv" | grep -q 'DLNA=DEBUG' || say "    hint: set the DLNA provider's log level to debug in MA (Settings > Providers > DLNA)"
  ha supervisor logs -n 5000 2>&1 | win | redact > "$OUT/supervisor.log"
  ha host logs -n 5000 2>&1 | redact | grep -iE 'out of memory|oom|killed process' > "$OUT/host_oom.log"
  rm -f "$OUT/music_assistant_full.log"
  for f in ha_core ha_core_radios music_assistant music_assistant_radios supervisor host_oom; do
    say "    $f.log: $(wc -l < "$OUT/$f.log") lines"
  done
  say "--- last radio-related HA lines"; tail -n 15 "$OUT/ha_core_radios.log" | sed 's/^/    /'
  say "--- last radio-related MA lines"; tail -n 25 "$OUT/music_assistant_radios.log" | sed 's/^/    /'
  [ -s "$OUT/host_oom.log" ] && { say "--- OOM kills found:"; sed 's/^/    /' "$OUT/host_oom.log"; }
fi

say "=== SUMMARY"
for r in "${RADIOS[@]}"; do set -- $r; say "    $1 ($2): ${STATUS[$1]}"; done
if [ -n "${CAP_PID:-}" ]; then
  say "waiting for the packet capture to finish..."; wait "$CAP_PID" 2>/dev/null
  say "    radios.pcap: $(du -h "$OUT/radios.pcap" 2>/dev/null | cut -f1)"
fi
say "all files: $OUT"
if [ $DO_ZIP = 1 ]; then
  sleep 1   # let tee flush report.txt
  zip_out="$OUT.zip"
  if command -v zip >/dev/null 2>&1; then
    (cd "$(dirname "$OUT")" && zip -qr "$zip_out" "$(basename "$OUT")")
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2], sys.argv[3])" \
      "$zip_out" "$(dirname "$OUT")" "$(basename "$OUT")"
  else
    zip_out="$OUT.tar.gz"; tar -czf "$zip_out" -C "$(dirname "$OUT")" "$(basename "$OUT")"
  fi
  [ -s "$zip_out" ] && say "archive: $zip_out ($(du -h "$zip_out" | cut -f1))" || say "archive could not be created"
fi
