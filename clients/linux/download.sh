#!/bin/bash
version=.03
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-download.log"
echo Download Script Version $version | tee "$debug"
#------------------------------------------------------------
r_count=0
dlfile=($(< /usr/local/scripts/downloads.txt))
r_count=${#dlfile[@]}
rn_dl=$((RANDOM % r_count))
url=${dlfile[rn_dl]}
sleep 1
wget --waitretry=10 --read-timeout=20 --show-progress -O /tmp/file.tmp "$url" | tee -a "$debug"
