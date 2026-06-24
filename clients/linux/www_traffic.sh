#!/bin/bash
version=.02
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-www-traffic.log"
echo WWW_Traffic Script Version $version | tee "$debug"
wwwfile=($(< /usr/local/scripts/websites.txt))
rn_www=$((RANDOM % ${#wwwfile[@]}))
url="${wwwfile[$rn_www]}"
cpulimit -l 25 -- firefox-esr --headless "$url"