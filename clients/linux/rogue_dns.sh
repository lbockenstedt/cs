#!/bin/bash
version=0.01
# --restore-only: recover a stale static phase and exit; never start a new one.
# The orchestrator runs it every cycle that finds phase=static and no instance.
restore_only=0
[[ ${1:-} == --restore-only ]] && restore_only=1
source '/usr/local/scripts/ini-parser.sh'
source '/usr/local/scripts/common.sh'
# common.sh has its own version variable; report this script's fleet baseline.
sim_version_banner 'rogue_dns.sh' '0.01'
process_ini_file '/usr/local/scripts/simulation.conf'
log=/usr/local/scripts/sim.log
state_file=/usr/local/scripts/rogue_dns.state
rogue_dns_server=$(get_value 'address' 'rogue_dns_server')
rogue_dns_dhcp_interval=$(get_value 'simulation' 'rogue_dns_dhcp_interval')
rogue_dns_duration=$(get_value 'simulation' 'rogue_dns_duration')
rogue_dns_browse_interval=$(get_value 'simulation' 'rogue_dns_browse_interval')
derive_username
for key in rogue_dns_server rogue_dns_dhcp_interval rogue_dns_duration rogue_dns_browse_interval; do
  apply_override "$key"
done
# Validate before arithmetic; blank or malformed timings use the fleet defaults.
for pair in rogue_dns_dhcp_interval:1800 rogue_dns_duration:3600 rogue_dns_browse_interval:60; do
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
    echo 'rogue_dns: DHCP restore failed; retaining state for retry' | tee -a "$log"
    return 1
  fi
}
if [[ -f "$state_file" ]]; then
  # JSON is data, never source/eval a persisted network state file.
  state=$(python3 - "$state_file" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
assert s['phase'] in ('dhcp', 'static') and type(s['start']) is int
# A static state must carry everything restore_dhcp needs, else it is corrupt.
assert s['phase'] == 'dhcp' or all(s.get(k) for k in ('adapter', 'address', 'gateway', 'connection'))
for key in ('phase', 'start', 'adapter', 'address', 'gateway', 'dns', 'connection'):
    print(s.get(key, ''))
PY
  ) || {
    (( restore_only )) || { echo 'rogue_dns: unreadable state; refusing network changes' | tee -a "$log"; exit 1; }
    # Restore-only on a corrupt state: we don't know what was applied. Act only
    # on rogue_dns's own fingerprint (the wireless device's DNS is exactly
    # rogue_dns_server); anything else may be operator config, so leave it and
    # just rewrite a valid dhcp state. restore_dhcp re-applies the saved
    # profile, so it only ever drops runtime (device modify) changes.
    wl=$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi" {print $1; exit}')
    [[ -n $wl ]] || { echo 'rogue_dns: unreadable state and no wireless device' | tee -a "$log"; exit 1; }
    # A failed query proves nothing, so keep the corrupt state and retry.
    wl_dns=$(nmcli -g IP4.DNS device show "$wl") || { echo "rogue_dns: unreadable state; DNS query on $wl failed, retrying" | tee -a "$log"; exit 1; }
    if [[ -n $rogue_dns_server && $wl_dns == "$rogue_dns_server" ]]; then
      echo "rogue_dns: unreadable state; $wl uses rogue_dns_server, restoring DHCP" | tee -a "$log"
      connection=$(nmcli -g GENERAL.CON-UUID device show "$wl")
      restore_dhcp
      exit $?
    fi
    echo "rogue_dns: unreadable state; no rogue_dns settings on $wl, leaving it" | tee -a "$log"
    write_state dhcp
    exit $?
  }
  mapfile -t saved <<< "$state"
  phase=${saved[0]}; start=${saved[1]}; wl=${saved[2]}; address=${saved[3]}
  gateway=${saved[4]}; dns=${saved[5]}; connection=${saved[6]}
  if [[ $phase == static ]]; then
    restore_dhcp
    exit $?
  fi
elif (( ! restore_only )); then
  write_state dhcp || exit 1
  exit 0
fi
(( restore_only )) && exit 0
(( $(date +%s) - start >= rogue_dns_dhcp_interval )) || exit 0
if ! python3 - "$rogue_dns_server" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
    assert 0 < int(str(ip).split('.')[0]) < 224 and not ip.is_loopback
except (ValueError, AssertionError):
    sys.exit(1)
PY
then
  echo 'rogue_dns: blank or invalid rogue_dns_server; skipping' | tee -a "$log"
  exit 0
fi
# Select a connected NM wireless device, including PCI wlp*/wlo* names.
wl=$(nmcli -t -f DEVICE,TYPE,STATE device status | awk -F: '$2=="wifi" && $3=="connected" {print $1; exit}')
[[ -n $wl ]] || { echo 'rogue_dns: no wireless lease' | tee -a "$log"; exit 0; }
connection=$(nmcli -g GENERAL.CON-UUID device show "$wl")
address=$(nmcli -g IP4.ADDRESS device show "$wl" | head -n1)
gateway=$(nmcli -g IP4.GATEWAY device show "$wl" | head -n1)
dns=$(nmcli -g IP4.DNS device show "$wl" | paste -sd,)
if [[ $(nmcli -g ipv4.method connection show uuid "$connection") != auto || -z $address || -z $gateway || $address == 169.254.* ]] || ! nmcli -g DHCP4.OPTION device show "$wl" | grep -q .; then
  echo 'rogue_dns: no DHCP lease; skipping' | tee -a "$log"
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
write_state static || exit 1
trap 'rc=$?; stop_browse; restore_dhcp || rc=1; exit "$rc"' EXIT
trap 'exit 0' INT TERM HUP
# Unprivileged nmcli like every other Linux sim: 50-client-sim-nm.rules grants
# the sim user NetworkManager network-control. If the apply is refused (no
# polkit rule, no root), stop here: the EXIT trap restores DHCP and keeps rc=1,
# so nothing stays half-applied and the browse loop never starts.
if ! nmcli device modify "$wl" ipv4.method manual ipv4.addresses "$address" ipv4.gateway "$gateway" ipv4.dns "$rogue_dns_server" ipv4.ignore-auto-dns yes >> "$log" 2>&1; then
  echo 'rogue_dns: static apply failed; restoring DHCP' | tee -a "$log"
  exit 1
fi
end=$(( $(date +%s) + rogue_dns_duration ))
wwwfile=()
if [[ -r /usr/local/scripts/websites.txt ]]; then
  mapfile -t wwwfile < <(sed '/^[[:space:]]*$/d' /usr/local/scripts/websites.txt)
fi
while (( $(date +%s) < end )); do
  remaining=$((end - $(date +%s)))
  (( remaining > 0 )) || break
  delay=$rogue_dns_browse_interval
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
