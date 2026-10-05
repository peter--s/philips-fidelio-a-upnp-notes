#!/usr/bin/env bash
# Refuse to publish files containing private network details.
# Usage: tools/check_private_data.sh [files...]   (default: all tracked/added files)
# As a git pre-commit hook:  ln -s ../../tools/check_private_data.sh .git/hooks/pre-commit
list_files() {
  if [ $# -gt 0 ]; then printf '%s\n' "$@"
  elif git rev-parse --git-dir >/dev/null 2>&1; then
    { git diff --cached --name-only --diff-filter=ACM; git ls-files; } 2>/dev/null
  else find . -type f -not -path './.git/*' -not -path '*/media/*' -not -name '*.pyc' -not -name radios.conf -not -name '*.local.conf' -not -name radio_diag.conf
  fi
}
pattern='\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b|\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b|github_pat_[A-Za-z0-9_]+'
# also regex-escaped forms such as 192\.168\.1\.2, and the names of your own speakers (from the local radios.conf)
pattern="$pattern"'|\b(10|192\\\.168|172\\\.(1[6-9]|2[0-9]|3[01]))\\\.[0-9]'
here=$(cd "$(dirname "$0")" && pwd)
for conf in "$here/radios.conf" "${RADIO_DIAG_CONF:-/homeassistant/radio_diag.conf}"; do
  [ -f "$conf" ] || continue
  while read -r name _; do
    case "$name" in ''|\#*) continue ;; esac
    pattern="$pattern|\b$name\b"
  done < "$conf"
done
hits=$(list_files "$@" | sort -u | tr '\n' '\0' | xargs -0 grep -nIE "$pattern" -- 2>/dev/null \
       | grep -v 'check_private_data.sh' | grep -vE '\.gitignore|radios\.example\.conf')
if [ -n "$hits" ]; then
  echo "Private network data found (private IPs, MAC addresses or tokens):"; echo "$hits"; exit 1
fi
echo "no private network data found"
