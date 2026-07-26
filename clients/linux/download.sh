#!/bin/bash
version=0.01
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-download.log"
echo Download Script Version $version | tee "$debug"
#------------------------------------------------------------
r_count=0
dlfile=($(< /usr/local/scripts/downloads.txt))
r_count=${#dlfile[@]}
if [[ $r_count -eq 0 ]]; then
  echo "No downloads listed in /usr/local/scripts/downloads.txt — skipping" | tee -a "$debug"
  exit 0
fi
rn_dl=$((RANDOM % r_count))
url=${dlfile[rn_dl]}
sleep 1
wget --waitretry=10 --read-timeout=20 --show-progress -O /tmp/file.tmp "$url" | tee -a "$debug"
