#!/bin/bash
version=.10
# WHY: dashboard.sh is a read-only live monitor. It runs in its own terminal
# window (launched by startup.desktop) so the operator can always see
# what's happening without interrupting the simulation loop in the other pane.
source '/usr/local/scripts/ini-parser.sh'
#------------------------------------------------------------
# Simulation Dashboard (Read-only live monitor)
#------------------------------------------------------------
refresh_rate=5
CACHE_DIR="/tmp/client-sim-dash"

# Terminal colors — degrade gracefully if tput is unavailable (e.g. SSH without TERM)
GRN=$(tput setaf 2 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
YLW=$(tput setaf 3 2>/dev/null || true)
CYN=$(tput setaf 6 2>/dev/null || true)
BOLD=$(tput bold 2>/dev/null || true)
RST=$(tput sgr0 2>/dev/null || true)

# Per-device config override (same logic as simulation.sh). Defined before
# refresh_config so refresh_config can call it.
apply_override() {
  local var=$1
  local val
  val=$(get_value "$username" "$var")
  [[ -n "${val}" ]] && declare -g "$var=$val"
}
override_keys=(kill_switch sim_load github_repo repo_location site_based_ssid iperf_bw \
  wsite sim_phy ssid dhcp_fail dns_fail assoc_fail port_flap ping_test download iperf \
  www_traffic ssidpw_fail auth_fail web_server)

# refresh_config — re-parse simulation.conf and re-derive every flag so the
# dashboard reflects the CURRENT config, not a launch-time snapshot. The
# engine/spoke can push a new simulation.conf mid-run with an [username]
# override (e.g. ssidpw_fail=on); simulation.sh re-reads each outer loop and
# acts on it, but the dashboard used to read the flags ONCE at launch and keep
# showing stale bucket flags (DNS/Download/WWW) forever — ssidpw_fail/auth_fail
# are inline in simulation.sh's loop (no separate .sh to pgrep), so the flag
# table is the ONLY place they can surface. Called at launch and at the top of
# every display + cache-worker cycle. Load simulation.conf ONLY — process_ini_file
# resets ALL section data on each call, so a second call for user-overrides.conf
# would wipe [simulation]/[s0-s9]; per-user overrides live in the [username]
# section of simulation.conf and are applied via apply_override() below.
refresh_config() {
  process_ini_file '/usr/local/scripts/simulation.conf'
  # Derive username the same way startup.sh does — hostname prefix before "-".
  username=$(echo "$HOSTNAME" | cut -d "-" -f 1)
  server_url=$(get_value 'server' 'server_url')
  server_url="${server_url:-http://169.253.1.1:8080}"
  bucket=$(python3 -c "import zlib; print(zlib.crc32('${HOSTNAME}'.encode()) % 10)")
  simulation_id="s${bucket}"
  user_sim_id=$(get_value "$username" 'simulation_id')
  # Only accept valid slot IDs (s0-s9) from user overrides; ignore malformed values
  [[ "$user_sim_id" =~ ^s[0-9]$ ]] && simulation_id="$user_sim_id"
  kill_switch=$(get_value 'simulation' 'kill_switch')
  rapid_update=$(get_value 'simulation' 'rapid_update')
  sim_load=$(get_value 'simulation' 'sim_load')
  github_repo=$(get_value 'simulation' 'github_repo')
  repo_location=$(get_value 'simulation' 'repo_location')
  site_based_ssid=$(get_value 'simulation' 'site_based_ssid')
  iperf_bw=$(get_value 'simulation' 'iperf_bw')
  auth_fail=$(get_value 'simulation' 'auth_fail')
  ssidpw_fail=$(get_value 'simulation' 'ssidpw_fail')
  allow_offline=$(get_value 'simulation' 'allow_offline')
  web_server=$(get_value 'simulation' 'web_server')
  # Device-specific settings
  wsite=$(get_value "$simulation_id" 'wsite')
  sim_phy=$(get_value "$simulation_id" 'sim_phy')
  ssid=$(get_value "$simulation_id" 'ssid')
  dhcp_fail=$(get_value "$simulation_id" 'dhcp_fail')
  dns_fail=$(get_value "$simulation_id" 'dns_fail')
  assoc_fail=$(get_value "$simulation_id" 'assoc_fail')
  port_flap=$(get_value "$simulation_id" 'port_flap')
  ping_test=$(get_value "$simulation_id" 'ping_test')
  download=$(get_value "$simulation_id" 'download')
  iperf=$(get_value "$simulation_id" 'iperf')
  www_traffic=$(get_value "$simulation_id" 'www_traffic')
  for key in "${override_keys[@]}"; do
    apply_override "$key"
  done
}
refresh_config
#------------------------------------------------------------
# Helper: webUI API reachability — reads from cache (no blocking curl).
#------------------------------------------------------------
get_api_status() {
  if [[ "$web_server" != "on" ]]; then
    echo "${YLW}DISABLED${RST} (web_server=off in config)"
    return
  fi
  if [[ -z "$server_url" ]]; then
    echo "${YLW}NOT CONFIGURED${RST}"
    return
  fi
  local http_code
  http_code=$(cat "$CACHE_DIR/api_code" 2>/dev/null)
  if [[ "$http_code" == "200" ]]; then
    echo "${GRN}CONNECTED${RST} (${server_url})"
  elif [[ -z "$http_code" ]]; then
    echo "${YLW}Checking...${RST}"
  else
    echo "${RED}UNREACHABLE${RST} (${server_url})"
  fi
}
#------------------------------------------------------------
# Background cache worker — runs all slow network I/O (nmcli, ping, curl)
# and writes results to $CACHE_DIR. The display loop reads these files
# instantly, eliminating all visible blocking pauses on refresh.
# Also sends the heartbeat POST from here so it never blocks rendering.
#------------------------------------------------------------
run_cache_worker() {
  mkdir -p "$CACHE_DIR"
  while true; do
    # Re-read config so the heartbeat's active_sims + the API-health gate track
    # a freshly-pushed simulation.conf instead of the launch-time snapshot.
    refresh_config
    # WiFi SSID
    nmcli -t -f active,ssid dev wifi 2>/dev/null \
      | awk -F: '$1=="yes"{print $2}' \
      > "$CACHE_DIR/wifi_ssid" 2>/dev/null || true

    # Default gateway + reachability
    local gw
    gw=$(ip route | grep -oP 'default via \K\S+' | head -n1)
    echo "$gw" > "$CACHE_DIR/gateway"
    if [[ -n "$gw" ]] && ping -c1 -W1 "$gw" >/dev/null 2>&1; then
      echo "up" > "$CACHE_DIR/gateway_ok"
    else
      echo "down" > "$CACHE_DIR/gateway_ok"
    fi

    # API health check
    if [[ "$web_server" == "on" && -n "$server_url" ]]; then
      local http_code
      http_code=$(curl -o /dev/null -s -w "%{http_code}" \
        --connect-timeout 2 --max-time 3 \
        "${server_url%/}/api/health" 2>/dev/null)
      echo "$http_code" > "$CACHE_DIR/api_code"
    fi

    # Heartbeat POST — reads from cache files so no extra nmcli/ping calls
    if [[ "$web_server" == "on" && -n "$server_url" ]]; then
      local connected_ssid gateway_reachable=false
      connected_ssid=$(cat "$CACHE_DIR/wifi_ssid" 2>/dev/null)
      [[ "$(cat "$CACHE_DIR/gateway_ok" 2>/dev/null)" == "up" ]] && gateway_reachable=true

      # simulation.sh owns the authoritative status: it does the weighted-ambient
      # pick + applies [username] overrides and writes the full payload (incl.
      # active_simulations) to client-status.json every loop. In HUB mode
      # (web_server=on) the s0-s9 slot sections are STRIPPED from the pushed
      # simulation.conf, so recomputing active_simulations here from those flags
      # yields [] for EVERY client — the "no active simulations" bug. Forward
      # simulation.sh's payload, overlaying only our freshest SSID/gateway (the
      # dashboard is the non-blocking network courier). Fall back to a locally-
      # built payload only when the file is absent (sim loop not up yet).
      local payload="" status_file="/usr/local/scripts/client-status.json"
      if [[ -s "$status_file" ]]; then
        payload=$(_SF="$status_file" _SSID="$connected_ssid" _GWOK="$gateway_reachable" \
          python3 -c 'import json,os,sys
try:
    d=json.load(open(os.environ["_SF"]))
except Exception:
    sys.exit(1)
d["connected_ssid"]=os.environ.get("_SSID") or None
d["gateway_reachable"]=os.environ.get("_GWOK")=="true"
sys.stdout.write(json.dumps(d))' 2>/dev/null)
      fi
      if [[ -z "$payload" ]]; then
        # Fallback (no client-status.json yet): build from the dashboard's own
        # flags. In hub mode these are empty (stripped slots) — that's the pre-
        # sim-loop window only; once simulation.sh writes the file we forward it.
        local active_sims=() active_sims_json="[]"
        [[ "$dhcp_fail"   == "on" ]] && active_sims+=("dhcp_fail")
        [[ "$dns_fail"    == "on" ]] && active_sims+=("dns_fail")
        [[ "$assoc_fail"  == "on" ]] && active_sims+=("assoc_fail")
        [[ "$port_flap"   == "on" ]] && active_sims+=("port_flap")
        [[ "$ping_test"   == "on" ]] && active_sims+=("ping_test")
        [[ "$download"    == "on" ]] && active_sims+=("download")
        [[ "$iperf"       == "on" ]] && active_sims+=("iperf")
        [[ "$www_traffic" == "on" ]] && active_sims+=("www_traffic")
        [[ "$ssidpw_fail" == "on" ]] && active_sims+=("ssidpw_fail")
        [[ "$auth_fail"   == "on" ]] && active_sims+=("auth_fail")
        if [[ ${#active_sims[@]} -gt 0 ]]; then
          active_sims_json=$(printf '"%s",' "${active_sims[@]}" | sed 's/,$//')
          active_sims_json="[$active_sims_json]"
        fi
        local ssid_json="null"
        [[ -n "$connected_ssid" ]] && ssid_json="\"$connected_ssid\""
        payload="{
          \"hostname\": \"$HOSTNAME\",
          \"simulation_id\": \"$simulation_id\",
          \"platform\": \"linux\",
          \"iteration\": 0,
          \"connected_ssid\": $ssid_json,
          \"gateway_reachable\": $gateway_reachable,
          \"active_simulations\": $active_sims_json,
          \"config\": {
            \"kill_switch\": \"$kill_switch\",
            \"sim_load\": \"$sim_load\",
            \"ssid\": \"$ssid\",
            \"wsite\": \"$wsite\"
          },
          \"errors\": []
        }"
      fi

      local resp throttle_secs
      resp=$(curl -s -X POST "${server_url%/}/api/status" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null) || true
      # Honor server throttle_interval so HTTP clients back off under load.
      if [[ -n "$resp" ]]; then
        throttle_secs=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('throttle_interval',''))" 2>/dev/null || true)
        if [[ "$throttle_secs" =~ ^[0-9]+$ ]] && (( throttle_secs > 0 )); then
          refresh_rate=$throttle_secs
        fi
      fi
    fi

    sleep "$refresh_rate"
  done
}

get_wifi_status() {
  local ssid
  ssid=$(cat "$CACHE_DIR/wifi_ssid" 2>/dev/null)
  if [[ -n "$ssid" ]]; then
    echo "${GRN}CONNECTED${RST} ($ssid)"
  else
    echo "${RED}DISCONNECTED${RST}"
  fi
}
#------------------------------------------------------------
# Helper: gateway reachability — reads from cache (no blocking ping).
#------------------------------------------------------------
get_gateway_status() {
  local gw ok
  gw=$(cat "$CACHE_DIR/gateway" 2>/dev/null)
  ok=$(cat "$CACHE_DIR/gateway_ok" 2>/dev/null)
  if [[ -z "$gw" ]]; then
    echo "${RED}NO ROUTE${RST}"
  elif [[ "$ok" == "up" ]]; then
    echo "${GRN}ONLINE${RST} ($gw)"
  else
    echo "${RED}OFFLINE${RST} ($gw)"
  fi
}
#------------------------------------------------------------
# Helper: map a script filename to its simulation config flag.
# "always" = Infrastructure section (always shown).
# "off"    = hide unless currently running.
#------------------------------------------------------------
get_script_flag() {
  case "$1" in
    agent.sh)       echo "always"       ;;
    update.sh)      echo "always"       ;;
    dns_fail.sh)    echo "$dns_fail"    ;;
    download.sh)    echo "$download"    ;;
    iperf.sh)       echo "$iperf"       ;;
    ping_test.sh)   echo "$ping_test"   ;;
    www_traffic.sh) echo "$www_traffic" ;;
    *)              echo "off"          ;;
  esac
}
#------------------------------------------------------------
# Helper: convert epoch timestamp to human-readable "X ago" string
#------------------------------------------------------------
time_ago() {
  local epoch="$1"
  [[ -z "$epoch" ]] && echo "-" && return
  local delta=$(( $(date +%s) - epoch ))
  if   (( delta < 60 ));    then echo "${delta}s ago"
  elif (( delta < 3600 ));  then echo "$(( delta / 60 ))m ago"
  elif (( delta < 86400 )); then echo "$(( delta / 3600 ))h ago"
  else                           echo "$(( delta / 86400 ))d ago"
  fi
}
#------------------------------------------------------------
# Helper: print one script row — tracks last-run timestamp in cache.
# RUNTIME column shows elapsed time when running, "Xm ago" when stopped.
#------------------------------------------------------------
print_script_row() {
  local script_name="$1" pid runtime display_time
  local last_run_file="$CACHE_DIR/last-run-${script_name}"
  pid=$(pgrep -f "$script_name" | head -n 1)
  if [[ -n "$pid" ]]; then
    runtime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
    date +%s > "$last_run_file"
    display_time="$runtime"
    printf "  %s%-12s%s %-22s %-10s\n" "$GRN" "[RUNNING]" "$RST" "$script_name" "$display_time"
  else
    display_time=$(time_ago "$(cat "$last_run_file" 2>/dev/null)")
    printf "  %s%-12s%s %-22s %-10s\n" "$RED" "[STOPPED]" "$RST" "$script_name" "$display_time"
  fi
}
#------------------------------------------------------------
# Helper: print one INLINE sim row. dhcp_fail/assoc_fail/port_flap/
# ssidpw_fail/auth_fail run INSIDE simulation.sh (no separate .sh script to
# pgrep), so they never appeared in the Simulations table. Surface them as
# [RUNNING] when their config flag is on — the runtime shown is the driving
# simulation.sh loop's elapsed time (the process actually executing them).
# Returns 0 (and prints a row) when the sim is active, 1 otherwise so the
# caller can count it toward "shown".
#------------------------------------------------------------
print_inline_sim_row() {
  local sim_name="$1" flag_val="$2"
  [[ "$flag_val" != "on" ]] && return 1
  local pid runtime="-"
  pid=$(pgrep -f "simulation\.sh" | head -n 1)
  if [[ -n "$pid" ]]; then
    runtime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
    [[ -z "$runtime" ]] && runtime="-"
  fi
  printf "  %s%-12s%s %-22s %-10s\n" "$GRN" "[RUNNING]" "$RST" "$sim_name" "$runtime"
  return 0
}
#------------------------------------------------------------
# Helper: simulation process table — two sections:
#   Infrastructure: always-shown scripts (agent.sh etc.)
#   Simulations:    flag-controlled scripts (only shown when flag=on)
# Any untracked script that is running also appears in Simulations.
#------------------------------------------------------------
get_sim_status() {
  local exclude=("dashboard.sh" "install.sh" "simulation.sh" "ini-parser.sh" "sys_mon.sh" "startup.sh")
  local hdr="  %s%-12s %-22s %-10s%s\n"
  local div="  %-12s %-22s %-10s\n"

  # ── Infrastructure ──────────────────────────────────────
  printf "  %sInfrastructure:%s\n" "$BOLD" "$RST"
  printf "$hdr" "$BOLD" "STATUS" "SCRIPT" "RUNTIME" "$RST"
  printf "$div" "──────────" "──────────────────────" "───────"
  for s in /usr/local/scripts/*.sh; do
    local script_name script_flag
    script_name=$(basename "$s")
    for e in "${exclude[@]}"; do [[ "$script_name" == "$e" ]] && continue 2; done
    script_flag=$(get_script_flag "$script_name")
    [[ "$script_flag" == "always" ]] && print_script_row "$script_name"
  done

  echo ""

  # ── Simulations ─────────────────────────────────────────
  printf "  %sSimulations:%s\n" "$BOLD" "$RST"
  printf "$hdr" "$BOLD" "STATUS" "SCRIPT" "RUNTIME" "$RST"
  printf "$div" "──────────" "──────────────────────" "───────"
  local shown=0
  for s in /usr/local/scripts/*.sh; do
    local script_name script_flag pid
    script_name=$(basename "$s")
    for e in "${exclude[@]}"; do [[ "$script_name" == "$e" ]] && continue 2; done
    script_flag=$(get_script_flag "$script_name")
    [[ "$script_flag" == "always" ]] && continue
    pid=$(pgrep -f "$script_name" | head -n 1)
    if [[ "$script_flag" == "on" || -n "$pid" ]]; then
      print_script_row "$script_name"
      (( shown++ ))
    fi
  done
  # Inline sims (run inside simulation.sh — no separate script to pgrep).
  # Shown as [RUNNING] when their flag is on; runtime = simulation.sh loop etime.
  print_inline_sim_row "dhcp_fail"   "$dhcp_fail"   && (( shown++ ))
  print_inline_sim_row "assoc_fail"  "$assoc_fail"  && (( shown++ ))
  print_inline_sim_row "port_flap"   "$port_flap"   && (( shown++ ))
  print_inline_sim_row "ssidpw_fail" "$ssidpw_fail" && (( shown++ ))
  print_inline_sim_row "auth_fail"   "$auth_fail"   && (( shown++ ))
  (( shown == 0 )) && printf "  ${GRN}None active${RST}\n"
}
#------------------------------------------------------------
# Main dashboard loop
# WHY: clear+redraw every refresh_rate seconds gives a live view without
# needing curses or a separate UI framework. All slow network I/O is handled
# by the background cache worker — the display loop only reads local files.
#------------------------------------------------------------
trap 'kill "$_cache_pid" 2>/dev/null; rm -rf "$CACHE_DIR"' EXIT
run_cache_worker &
_cache_pid=$!

while true; do
  clear
  # Re-read config each refresh so the flag table reflects the current
  # simulation.conf (engine-pushed [username] overrides land immediately,
  # e.g. ssidpw_fail=on) instead of the launch-time snapshot.
  refresh_config
  # Re-detect the WiFi adapter each refresh.
  wladapter=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
  # Global kill switch comes from kill_switch.txt, synced from the GitHub repo by update.sh.
  # To kill all simulations globally, set linux/kill_switch.txt = "on" in the repo.
  gkill=$(cat /usr/local/scripts/kill_switch.txt 2>/dev/null || echo "off")

  printf "%s%s%s\n" "$BOLD" "$(printf '═%.0s' $(seq 1 $(tput cols 2>/dev/null || echo 58)))" "$RST"
  printf "%s  SIMULATION DASHBOARD %s%-14s%s %s%s\n" "$BOLD" "$CYN" "$HOSTNAME" "$RST" "$(date '+%H:%M:%S')" "$RST"
  printf "%s%s%s\n" "$BOLD" "$(printf '═%.0s' $(seq 1 $(tput cols 2>/dev/null || echo 58)))" "$RST"
  echo ""
  printf "  %sSite:%s    %-22s  %sSim-ID:%s %s\n" "$BOLD" "$RST" "$wsite" "$BOLD" "$RST" "$simulation_id"
  printf "  %sPHY:%s     %-22s  %sLoad:%s   %s%%\n" "$BOLD" "$RST" "$sim_phy" "$BOLD" "$RST" "$sim_load"
  [[ -n "$wladapter" ]] && printf "  %sAdapter:%s %s\n" "$BOLD" "$RST" "$wladapter"
  echo ""
  printf "  %sWiFi:%s    %s\n" "$BOLD" "$RST" "$(get_wifi_status)"
  printf "  %sGateway:%s %s\n" "$BOLD" "$RST" "$(get_gateway_status)"
  printf "  %sAPI:%s     %s\n" "$BOLD" "$RST" "$(get_api_status)"
  # Surface global kill-switch prominently — operator needs to know immediately.
  # Controlled via linux/kill_switch.txt in the GitHub repo (update.sh syncs it).
  if [[ "$gkill" == "on" ]]; then
    printf "  %sKill Sw:%s %s\n" "$BOLD" "$RST" "${RED}${BOLD}ENABLED — all simulations suspended${RST}"
  fi
  echo ""
  # Simulation flags table — 2-column layout so nothing wraps.
  # WHY: Bash associative arrays have no guaranteed iteration order so the
  # flags would appear in a different sequence every refresh. Parallel arrays
  # give consistent ordering so the operator can scan quickly.
  flag_labels=("Kill Switch" "DHCP Fail" "DNS Fail" "WWW Traffic" "iPerf" "Download" "Port Flap" "Bad SSID PW" "Auth Fail")
  flag_values=("$kill_switch" "$dhcp_fail" "$dns_fail" "$www_traffic" "$iperf" "$download" "$port_flap" "$ssidpw_fail" "$auth_fail")
  printf "  %sSimulations:%s\n" "$BOLD" "$RST"
  col=0
  for i in "${!flag_labels[@]}"; do
    val="${flag_values[$i]}"
    [[ "$val" != "on" ]] && continue
    label="${flag_labels[$i]}"
    printf "  %-14s %b   " "$label:" "${YLW}[ON] ${RST}"
    (( col++ ))
    if (( col % 2 == 0 )); then printf "\n"; fi
  done
  (( col % 2 != 0 )) && printf "\n"
  (( col == 0 )) && printf "  ${GRN}None active${RST}\n"
  echo ""
  get_sim_status
  printf "%s%s%s\n" "$BOLD" "$(printf '═%.0s' $(seq 1 $(tput cols 2>/dev/null || echo 58)))" "$RST"
  sleep "$refresh_rate"
done