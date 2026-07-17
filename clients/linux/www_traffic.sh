#!/bin/bash
version=.02
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-www-traffic.log"
echo WWW_Traffic Script Version $version | tee "$debug"
wwwfile=($(< /usr/local/scripts/websites.txt))
if [[ ${#wwwfile[@]} -eq 0 ]]; then
  echo "No websites listed in /usr/local/scripts/websites.txt — skipping" | tee -a "$debug"
  exit 0
fi
rn_www=$((RANDOM % ${#wwwfile[@]}))
url="${wwwfile[$rn_www]}"
# cpulimit may not be installed yet on a fresh box (apt_update.sh installs it at
# the end of the outer loop). Fall back to a plain launch so the request fires.
if command -v cpulimit >/dev/null 2>&1; then
  cpulimit -l 10 -- firefox-esr --headless "$url"
else
  firefox-esr --headless "$url"
fi