#!/bin/bash
version=0.01
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-dhcp-fail.log"
echo "DHCP Fail Script Version $version" | tee "$debug"
#------------------------------------------------------------
# dhcp_fail — intentional DHCP-failure generator (replaces the old MAC spoof).
#
# The client's real wifi/wired connection and its NORMAL MAC are left
# UNTOUCHED. We do not change the adapter MAC, the NM profile, or the lease.
# Instead we fire crafted DHCPDISCOVERs as fire-and-forget UDP packets (no
# reply waited for — the failure is the point, mirror of dns_fail.sh):
#
#   A) -> the REAL DHCP server (detected from the NM lease), carrying a FORGED
#      identity 00:01:00:00:<o5>:<o6>. The good server rejects/ignores the
#      unknown id.
#   B) -> a dead server (default 10.10.10.10), carrying the REAL mac. Nothing
#      responds (timeout).
#
# The contrast (bad-id vs good-server, good-id vs dead-server) is the insight
# signal. The identity rides in BOTH chaddr and option-61 (see dhcp_fire.py).
#
# Loop model: 100 attempts then EXIT. The outer sim loop (simulation.sh
# `for z in {1..100}` + run_simulation) relaunches this each iteration, which
# re-detects the real DHCP server fresh. "100 attempts then restart the sims."
#------------------------------------------------------------
VERBOSE=0
for _a in "$@"; do case "$_a" in --verbose|-v) VERBOSE=1 ;; esac; done

source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'

# Resolve the active iface — wifi first, then wired (mirror simulation.sh).
iface=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
[[ -z "$iface" ]] && iface=$(ip -br a | grep "enp\|eno\|eth0\|ens" | cut -d ' ' -f '1')
if [[ -z "$iface" ]]; then
  echo "$(date) dhcp_fail: no usable iface — exiting" | tee -a "$debug" "$log"
  exit 1
fi

real_mac=$(cat /sys/class/net/"$iface"/address 2>/dev/null)
if [[ -z "$real_mac" ]]; then
  echo "$(date) dhcp_fail: cannot read $iface MAC — exiting" | tee -a "$debug" "$log"
  exit 1
fi

# Forged identity: fixed 00:01 prefix (recognizable) + real last two octets
# (keeps every client unique).
forged_mac="00:01:00:00:$(echo "$real_mac" | awk -F: '{print $5":"$6}')"

# Dead server (never responds) — configurable via [address] dhcp_fail_dead_server.
dead_server=$(get_value 'address' 'dhcp_fail_dead_server')
[[ -z "$dead_server" ]] && dead_server="10.10.10.10"

# Rate: attempts per minute -> pause between attempts. Mirror dns_fail.
rate_per_minute=$(get_value 'simulation' 'dhcp_fail_rate')
[[ -z "$rate_per_minute" ]] && rate_per_minute=600
(( rate_per_minute < 60 )) && rate_per_minute=60
pause_between=$(awk "BEGIN { printf \"%.3f\", 60 / $rate_per_minute }")

#------------------------------------------------------------
# Detect the real DHCP server from the NM lease on $iface.
# nmcli DHCP4.OPTIONS looks like "key = value key2 = value2 ..."; normalize
# spaces around '=' so we can split on spaces and grep the server id.
#------------------------------------------------------------
detect_dhcp_server() {
  local opts srv
  opts=$(nmcli -g DHCP4.OPTIONS device show "$iface" 2>/dev/null)
  srv=$(echo "$opts" | sed 's/ *= */=/g' | tr ' ' '\n' \
        | grep -oiE '^dhcp_server_identifier=[0-9.]+' | cut -d= -f2 | head -1)
  [[ -z "$srv" ]] && srv=$(echo "$opts" | sed 's/ *= */=/g' | tr ' ' '\n' \
        | grep -oiE '^next_server=[0-9.]+' | cut -d= -f2 | head -1)
  echo "$srv"
}

real_server=$(detect_dhcp_server)
echo "$(date) dhcp_fail start: iface=$iface real_mac=$real_mac forged=$forged_mac " \
     "real_server=${real_server:-<none>} dead=$dead_server rate=${rate_per_minute}/min" \
     | tee -a "$debug"

fire() {
  # $1 = dst server IP, $2 = identity mac (chaddr + opt-61)
  if (( VERBOSE )); then
    printf '%s [dhcp discover -> %-15s id=%s]\n' "$(date '+%H:%M:%S')" "$1" "$2"
    python3 /usr/local/scripts/dhcp_fire.py --iface "$iface" --dst "$1" --mac "$2" --verbose
  else
    python3 /usr/local/scripts/dhcp_fire.py --iface "$iface" --dst "$1" --mac "$2" >/dev/null 2>&1
  fi
}

if (( VERBOSE )); then
  echo "[verbose] iface=$iface real_mac=$real_mac forged=$forged_mac"
  echo "[verbose] real_server=${real_server:-<none>} dead=$dead_server pause=${pause_between}s"
  echo "----------------------------------------"
fi

# 100 attempts, then exit — the sim loop relaunches (re-detecting the server).
for i in {1..100}; do
  # A) real server + forged id (skip only if we couldn't detect the server)
  [[ -n "$real_server" ]] && fire "$real_server" "$forged_mac"
  # B) dead server + real mac
  fire "$dead_server" "$real_mac"
  (( VERBOSE )) || sleep "$pause_between"
done

echo "$(date) dhcp_fail: 100 attempts fired — exiting (sim loop will relaunch)" | tee -a "$debug"