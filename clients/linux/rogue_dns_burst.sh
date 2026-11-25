#!/bin/bash
version=0.01
source '/usr/local/scripts/ini-parser.sh'
source '/usr/local/scripts/common.sh'
# common.sh has its own version variable; report this script's fleet baseline.
sim_version_banner 'rogue_dns_burst.sh' '0.01'
process_ini_file '/usr/local/scripts/simulation.conf'
log=/usr/local/scripts/sim.log
state_file=/usr/local/scripts/rogue_dns_burst.state
rogue_dns_burst_server=$(get_value 'address' 'rogue_dns_burst_server')
rogue_dns_burst_dhcp_interval=$(get_value 'simulation' 'rogue_dns_burst_dhcp_interval')
rogue_dns_burst_duration=$(get_value 'simulation' 'rogue_dns_burst_duration')
rogue_dns_burst_browse_interval=$(get_value 'simulation' 'rogue_dns_burst_browse_interval')
rogue_dns_burst_rate=$(get_value 'simulation' 'rogue_dns_burst_rate')
rogue_dns_burst_max_pending=$(get_value 'simulation' 'rogue_dns_burst_max_pending')
rogue_dns_burst_domains=$(get_value 'address' 'rogue_dns_burst_domains')
derive_username
for key in rogue_dns_burst_server rogue_dns_burst_dhcp_interval rogue_dns_burst_duration rogue_dns_burst_browse_interval \
           rogue_dns_burst_rate rogue_dns_burst_max_pending rogue_dns_burst_domains; do
  apply_override "$key"
done
# Validate before arithmetic; blank or malformed numbers use the fleet defaults.
for pair in rogue_dns_burst_dhcp_interval:1800 rogue_dns_burst_duration:3600 rogue_dns_burst_browse_interval:60 \
            rogue_dns_burst_rate:20 rogue_dns_burst_max_pending:40; do
  key=${pair%:*}
  [[ ${!key} =~ ^[1-9][0-9]{0,7}$ ]] || declare "$key=${pair#*:}"
done
write_state() {
  python3 - "$state_file" "$1" "$(date +%s)" "${wl:-}" "${address:-}" "${gateway:-}" "${dns:-}" "${connection:-}" <<'PY'
import json, os, sys
path, phase, start, adapter, address, gateway, dns, connection = sys.argv[1:]
with open(path + '.new', 'w') as f:
    json.dump(dict(phase=phase, start=int(start), adapter=adapter, address=address,
                   gateway=gateway, dns=dns, connection=connection), f)
    f.flush()
    os.fsync(f.fileno())
os.replace(path + '.new', path)
PY
}
restore_dhcp() {
  # Re-activate the untouched DHCP profile; never persist the manual settings.
  nmcli device reapply "$wl" >> "$log" 2>&1 || true
  if nmcli connection up uuid "$connection" ifname "$wl" >> "$log" 2>&1; then
    write_state dhcp
  else
    echo 'rogue_dns_burst: DHCP restore failed; retaining static state for retry' | tee -a "$log"
    return 1
  fi
}
if [[ -f "$state_file" ]]; then
  # JSON is data, never source/eval a persisted network state file.
  state=$(python3 - "$state_file" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
assert s['phase'] in ('dhcp', 'static') and isinstance(s['start'], int)
for key in ('phase', 'start', 'adapter', 'address', 'gateway', 'dns', 'connection'):
    print(s.get(key, ''))
PY
  ) || { echo 'rogue_dns_burst: unreadable state; refusing network changes' | tee -a "$log"; exit 1; }
  mapfile -t saved <<< "$state"
  phase=${saved[0]}; start=${saved[1]}; wl=${saved[2]}; address=${saved[3]}
  gateway=${saved[4]}; dns=${saved[5]}; connection=${saved[6]}
  if [[ $phase == static ]]; then
    restore_dhcp
    exit $?
  fi
else
  write_state dhcp || exit 1
  exit 0
fi
(( $(date +%s) - start >= rogue_dns_burst_dhcp_interval )) || exit 0
if ! python3 - "$rogue_dns_burst_server" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
    assert 0 < int(str(ip).split('.')[0]) < 224 and not ip.is_loopback
except (ValueError, AssertionError):
    sys.exit(1)
PY
then
  echo 'rogue_dns_burst: blank or invalid rogue_dns_burst_server; skipping' | tee -a "$log"
  exit 0
fi
# Select a connected NM wireless device, including PCI wlp*/wlo* names.
wl=$(nmcli -t -f DEVICE,TYPE,STATE device status | awk -F: '$2=="wifi" && $3=="connected" {print $1; exit}')
[[ -n $wl ]] || { echo 'rogue_dns_burst: no wireless lease' | tee -a "$log"; exit 0; }
connection=$(nmcli -g GENERAL.CON-UUID device show "$wl")
address=$(nmcli -g IP4.ADDRESS device show "$wl" | head -n1)
gateway=$(nmcli -g IP4.GATEWAY device show "$wl" | head -n1)
dns=$(nmcli -g IP4.DNS device show "$wl" | paste -sd,)
if [[ $(nmcli -g ipv4.method connection show uuid "$connection") != auto || -z $address || -z $gateway || $address == 169.254.* ]] || ! nmcli -g DHCP4.OPTION device show "$wl" | grep -q .; then
  echo 'rogue_dns_burst: no DHCP lease; skipping' | tee -a "$log"
  exit 0
fi
stop_browse() {
  if [[ -n ${browser_pid:-} ]]; then
    kill -TERM "$browser_pid" 2>/dev/null || true
    wait "$browser_pid" 2>/dev/null || true
    browser_pid=
  fi
  if [[ -n ${sleep_pid:-} ]]; then
    kill "$sleep_pid" 2>/dev/null || true
    wait "$sleep_pid" 2>/dev/null || true
    sleep_pid=
  fi
}
# Background DNS query load for the static window. Runs in a subshell so it
# only ever signals its own dig children (never a name-based kill). The rate
# goes through the shared dns_ceiling_rate; the shared gateway breaker backs
# it off (dns_ceiling_penalize) and holds it until the gateway is stable again.
burst_loop() {
  local -a doms pids=()
  local rate gw n i idx=0 next_gw=$SECONDS p alive
  read -ra doms <<< "${rogue_dns_burst_domains//,/ }"
  (( ${#doms[@]} )) || doms=(www.google.com www.youtube.com www.linkedin.com)
  gw=$(dns_default_gw)
  trap 'for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; exit 0' TERM INT HUP
  while :; do
    rate=$(dns_ceiling_rate "$rogue_dns_burst_rate")
    if [[ -n $gw ]] && (( SECONDS >= next_gw )); then
      next_gw=$((SECONDS + 10))
      if ! dns_gw_alive "$gw" && dns_gw_confirmed_down "$gw"; then
        rate=$(dns_ceiling_penalize "$rate")
        echo "rogue_dns_burst: gateway down; rate lowered to $rate/s, holding" >> "$log"
        until dns_gw_stable "$gw"; do sleep 2; done
      fi
    fi
    for (( n = 0; n < rate; n++ )); do
      # Prune finished queries; when the cap is hit wait on the oldest.
      alive=()
      for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive+=("$p"); done
      pids=("${alive[@]}")
      (( ${#pids[@]} >= rogue_dns_burst_max_pending )) && { wait "${pids[0]}" 2>/dev/null; pids=("${pids[@]:1}"); }
      dig -r "@$rogue_dns_burst_server" "${doms[idx % ${#doms[@]}]}" A +time=2 +tries=1 +noall +comments +answer >/dev/null 2>&1 &
      pids+=($!)
      idx=$((idx + 1))
    done
    sleep 1 &
    wait $!
  done
}
stop_burst() {
  # Stop only the loop we started; its TERM trap reaps its own dig children.
  if [[ -n ${burst_pid:-} ]]; then
    kill -TERM "$burst_pid" 2>/dev/null || true
    wait "$burst_pid" 2>/dev/null || true
    burst_pid=
  fi
}
write_state static || exit 1
# The burst stops before DHCP is restored, on every exit path.
trap 'rc=$?; stop_burst; stop_browse; restore_dhcp || rc=1; exit "$rc"' EXIT
trap 'exit 0' INT TERM HUP
if ! nmcli device modify "$wl" ipv4.method manual ipv4.addresses "$address" ipv4.gateway "$gateway" ipv4.dns "$rogue_dns_burst_server" ipv4.ignore-auto-dns yes >> "$log" 2>&1; then
  echo 'rogue_dns_burst: static apply failed' | tee -a "$log"
  exit 1
fi
if command -v dig >/dev/null 2>&1; then
  burst_loop &
  burst_pid=$!
else
  echo 'rogue_dns_burst: dig not installed; running without the query burst' | tee -a "$log"
fi
end=$(( $(date +%s) + rogue_dns_burst_duration ))
wwwfile=()
if [[ -r /usr/local/scripts/websites.txt ]]; then
  mapfile -t wwwfile < <(sed '/^[[:space:]]*$/d' /usr/local/scripts/websites.txt)
fi
while (( $(date +%s) < end )); do
  remaining=$((end - $(date +%s)))
  (( remaining > 0 )) || break
  delay=$rogue_dns_burst_browse_interval
  (( delay > remaining )) && delay=$remaining
  # One WWW-style random request, bounded to this interval. timeout owns the
  # browser process group; the trap terminates only this launch, never our sim.
  if (( ${#wwwfile[@]} )); then
    url=${wwwfile[RANDOM % ${#wwwfile[@]}]}
    if command -v cpulimit >/dev/null 2>&1; then
      timeout -k 1 "$delay" cpulimit -f -l 10 -- firefox-esr --headless "$url" >> "$log" 2>&1 &
    else
      timeout -k 1 "$delay" firefox-esr --headless "$url" >> "$log" 2>&1 &
    fi
    browser_pid=$!
  fi
  sleep "$delay" &
  sleep_pid=$!
  wait "$sleep_pid" || true
  stop_browse
done
