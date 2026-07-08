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
cpulimit -l 25 -- firefox-esr --headless "$url"