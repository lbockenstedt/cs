#!/bin/bash
version=0.01
# Lifecycle simulation: the orchestrator waits for this window, not a traffic job.
source '/usr/local/scripts/ini-parser.sh'
source '/usr/local/scripts/common.sh'
source '/usr/local/scripts/network_common.sh'
version=0.01
LOG_FILE='/usr/local/scripts/sim.log'
process_ini_file '/usr/local/scripts/simulation.conf'
sim_version_banner 'client_offline.sh' "$version"
sim_phy="${1:-wireless}"

# Whole positive minutes; reject values whose seconds cannot fit on Windows.
minutes_or_default() {
  local value="$1" fallback="$2"
  if [[ "$value" =~ ^[0-9]{1,8}$ ]] && (( 10#$value >= 1 && 10#$value <= 35791394 )); then
    printf '%s' "$((10#$value))"
  else
    printf '%s' "$fallback"
  fi
}
min_minutes=$(minutes_or_default "$(get_value 'simulation' 'client_offline_min_minutes')" 60)
max_minutes=$(minutes_or_default "$(get_value 'simulation' 'client_offline_max_minutes')" 240)
if (( min_minutes > max_minutes )); then
  tmp=$min_minutes; min_minutes=$max_minutes; max_minutes=$tmp
fi
duration=$(shuf -i "$((min_minutes * 60))-$((max_minutes * 60))" -n 1) || exit 1

case "$sim_phy" in
  wireless) detect_wlan_adapter; adapter="$wladapter" ;;
  ethernet) detect_eth_adapter; adapter="$eadapter" ;;
  *) echo "client_offline: unknown sim_phy '$sim_phy'; skipping window" | tee -a "$LOG_FILE"; exit 0 ;;
esac
# Detection may return multiple names. Never guess or operate on a list.
if [[ -z "$adapter" || "$adapter" == *$'\n'* ]] || ! ip link show dev "$adapter" >/dev/null 2>&1; then
  echo "client_offline: $sim_phy adapter missing or ambiguous; skipping window" | tee -a "$LOG_FILE"
  exit 0
fi
addresses=$(ip -4 addr show dev "$adapter" 2>/dev/null) || {
  echo "client_offline: cannot check management addresses on $adapter; skipping window" | tee -a "$LOG_FILE"
  exit 0
}
# Same guard as ea_is_mgmt, applied to the selected adapter (including Wi-Fi).
if grep -q '169\.253\.' <<< "$addresses"; then
  echo "client_offline: management IP active on $adapter; skipping window" | tee -a "$LOG_FILE"
  exit 0
fi
if ! ip -o link show dev "$adapter" | grep -qE '<([^>]*,)?UP(,|>)'; then
  echo "client_offline: $adapter already down; skipping window" | tee -a "$LOG_FILE"
  exit 0
fi

brought_down=0
interrupted=0
sleep_pid=''
restore_adapter() {
  if [[ -n "$sleep_pid" ]]; then
    kill "$sleep_pid" 2>/dev/null || true
    wait "$sleep_pid" 2>/dev/null || true
  fi
  if (( brought_down )); then
    if sudo ip link set dev "$adapter" up; then
      echo "client_offline: restored $adapter" | tee -a "$LOG_FILE"
    else
      echo "client_offline: failed to restore $adapter" | tee -a "$LOG_FILE"
    fi
  fi
}
interrupt_window() {
  interrupted=1
  [[ -z "$sleep_pid" ]] || kill "$sleep_pid" 2>/dev/null || true
}
trap restore_adapter EXIT
trap interrupt_window TERM INT USR1 HUP
if (( interrupted )); then exit 0; fi
if sudo ip link set dev "$adapter" down; then
  brought_down=1
else
  echo "client_offline: failed to disable $adapter; skipping window" | tee -a "$LOG_FILE"
  exit 0
fi
if (( interrupted )); then exit 0; fi
echo "client_offline: $adapter offline for ${duration}s (management/API untouched)" | tee -a "$LOG_FILE"
sleep "$duration" &
sleep_pid=$!
# Catch a signal between starting sleep and recording its PID.
if (( interrupted )); then kill "$sleep_pid" 2>/dev/null || true; fi
wait "$sleep_pid" 2>/dev/null || true
