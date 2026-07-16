#!/bin/bash
version=.95
LOG_FILE=/usr/local/scripts/sim.log

echo $(date) | tee -a ${LOG_FILE}
echo ------------------------------| tee -a ${LOG_FILE}
echo Simulation Script Version $version | tee -a ${LOG_FILE}
#------------------------------------------------------------
#DO NOT EDIT BELOW THIS LINE UNLESS YOU KNOW WHAT YOU ARE DOING
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'

#------------------------------------------------------------
# Remote-control support — let agent.sh find and signal this loop.
# startup.sh runs `source .../simulation.sh`, so $$ here is the process
# actually running the sim loop (argv is startup.sh, so pgrep can't match).
# Writing our own PID lets agent.sh signal us via the PID file.
#------------------------------------------------------------
echo $$ > /usr/local/scripts/simulation.pid 2>/dev/null || true
_sim_reload=0
# USR1 (from agent.sh restart_sim / kill_switch) triggers a config re-read
# on the next outer-loop iteration instead of default-terminating the process.
trap '_sim_reload=1' USR1

require_config_value() {
  local name="$1" value="${2:-}"
  if [[ -z "$value" ]]; then
    echo "Missing required config value: $name" | tee -a ${LOG_FILE}
    exit 1
  fi
}

sed_escape() {
  printf '%s\n' "$1" | sed -e 's/[\/&]/\\&/g'
}

delete_matching_connections() {
  while IFS= read -r conn; do
    [[ -n "$conn" ]] && sudo nmcli con del "$conn"
  done < <(nmcli -t -f NAME con show 2>/dev/null | grep 'PSK' || true)
}

init_simulation_context() {
  # Load simulation.conf only — process_ini_file resets ALL section data on each
  # call, so a second call for user-overrides.conf would wipe [simulation]/[s0-s9].
  # Username-section overrides are defined in simulation.conf; apply_override()
  # reads them from there via get_value $username.
  process_ini_file '/usr/local/scripts/simulation.conf'
  username=$(echo "$HOSTNAME" | cut -d "-" -f 1)
  # Hash the hostname to assign a bucket — produces s0-s9 deterministically.
  bucket=$(python3 -c "import zlib; print(zlib.crc32('${HOSTNAME}'.encode()) % 10)")
  simulation_id="s${bucket}"
  # Allow user-overrides.conf to pin a specific bucket via simulation_id key.
  # Only accept valid slot IDs (s0-s9); ignore malformed values.
  # This must happen before the bucket config is read below.
  user_sim_id=$(get_value "$username" 'simulation_id')
  [[ "$user_sim_id" =~ ^s[0-9]$ ]] && simulation_id="$user_sim_id"
  require_config_value "username" "$username"
  require_config_value "simulation_id" "$simulation_id"
}

# Snapshot our OWN script's mtime so the loop can self-re-exec into a NEW
# simulation.sh the instant update.sh replaces it on disk. A sourced `while true`
# loop cannot reload its own CODE (only its config) — re-exec (same PID, no
# reboot, no sudo) is the reliable, immediate activator. This replaces the old
# "schedule a reboot from update.sh" approach, which failed silently whenever the
# sim user lacked passwordless sudo, leaving the box on stale code with the new
# code already on disk (update.sh then reports "no files updated" forever).
_self_path="/usr/local/scripts/simulation.sh"
_self_mtime=$(stat -c %Y "$_self_path" 2>/dev/null)

# One-time self-heal: disable NetworkManager MAC randomization. NM's defaults
# randomize the wifi MAC (scan-rand + stable-random cloned-mac on new profiles),
# which breaks device identity (AP/ClearPass/NetBox see a different MAC per
# scan). install.sh deploys the same conf at install time; this catches
# ALREADY-deployed clients without a reinstall.
# Best-effort `sudo -n` (no TTY hang): the sim user has full sudo on most boxes;
# on scoped-user boxes it silently no-ops and install.sh remains the backstop.
# Runs ONCE per process (before the loop) — reloading NM mid-loop would disrupt
# every associate. Idempotent: skips when the conf is already present + correct.
_no_rand_conf=/etc/NetworkManager/conf.d/99-client-sim-no-mac-random.conf
_no_rand_body='# Managed by client-sim-install.sh — do not edit manually
[device]
wifi.scan-rand-mac-address=no
[connection]
wifi.cloned-mac-address=permanent
ethernet.cloned-mac-address=permanent'
if ! grep -qs 'wifi.scan-rand-mac-address=no' "$_no_rand_conf" 2>/dev/null; then
  if printf '%s\n' "$_no_rand_body" | sudo -n tee "$_no_rand_conf" >/dev/null 2>&1; then
    sudo -n chmod 644 "$_no_rand_conf" 2>/dev/null || true
    sudo -n nmcli general reload 2>/dev/null || sudo -n systemctl reload NetworkManager 2>/dev/null || true
    echo "Disabled NM MAC randomization (self-heal)" | tee -a ${LOG_FILE}
  else
    echo "MAC-randomization self-heal skipped (no sudo — install.sh backstop)" | tee -a ${LOG_FILE}
  fi
fi

while true; do
  #------------------------------------------------------------
  # If a USR1 arrived (agent.sh restart_sim/kill_switch), note the
  # reload and clear the flag; init_simulation_context + the get_value
  # reads below re-read simulation.conf (kill_switch, simulation_id, etc.).
  #------------------------------------------------------------
  if [[ "${_sim_reload:-0}" == 1 ]]; then
    echo "USR1 received — reloading simulation config" | tee -a ${LOG_FILE}
    _sim_reload=0
  fi
  init_simulation_context
#------------------------------------------------------------
#Finding adapter names and setting usable variables for interfaces
#When using a physical piece of hardware we want to diable the
#interface not in use. So that we force the traffic out the interface
#set int he simulation.conf
#------------------------------------------------------------
#------------------------------------------------------------
wladapter=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
if [[ -n ${wladapter} ]]; then echo WLAN Adapter name $wladapter | tee -a ${LOG_FILE}; fi
eadapter=$(ip -br a | grep "enp\|eno\|eth0\|eth1\|eth2\|eth3\|eth4\|eth5\|eth6" | cut -d ' ' -f '1')
#------------------------------------------------------------
# Management IP guard — prevents shutting down the mgmt interface
#------------------------------------------------------------
ea_is_mgmt() {
  [[ -n "$eadapter" ]] && ip -4 addr show dev "$eadapter" 2>/dev/null | grep -q "169\.253\."
}
ea_down() {
  if ea_is_mgmt; then
    echo "Blocked ethernet shutdown — management IP active on $eadapter" | tee -a ${LOG_FILE}
  elif [[ -n "$eadapter" ]]; then
    sudo ip link set dev "$eadapter" down
  fi
}
if [[ -n ${eadapter} ]]; then echo Wired Adapter name $eadapter | tee -a ${LOG_FILE}; fi
#------------------------------------------------------------
echo Parsing Config File | tee -a ${LOG_FILE}
#------------------------------------------------------------
#Settings read from the local config file
#Global Simulation settings
#------------------------------------------------------------
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
# Hub mode (web_server=on): the engine drives placement + harvest and the s0-s9
# buckets are ignored — report "Auto" as the simulation_id so the dashboard shows
# the client is engine-driven rather than pinned to a bucket.
[[ "$web_server" == "on" ]] && simulation_id="Auto"
# 802.1X EAP method: 'peap' (default, PEAP-MSCHAPv2 username/password) or 'tls'
# (EAP-TLS, cert-based — for Cloud NAC; certs provisioned by cloud_nac_onboard.py).
dot1x_eap=$(get_value 'simulation' 'dot1x_eap')
dot1x_eap="${dot1x_eap:-peap}"
# Per-user PEAP password for Cloud NAC: the hub JIT-creates an Entra account for a
# client the moment the engine moves it onto a 1X SSID and delivers that account's
# random password here as a [username] override (apply_override, above). Falls back
# to $ssidpw when unset (a non-cloud-NAC 1X SSID that uses a shared password).
dot1x_password=$(get_value 'simulation' 'dot1x_password')
dot1x_client_cert=$(get_value 'simulation' 'dot1x_client_cert')
dot1x_private_key=$(get_value 'simulation' 'dot1x_private_key')
dot1x_ca_cert=$(get_value 'simulation' 'dot1x_ca_cert')
server_url=$(get_value 'server' 'server_url')
server_url="${server_url:-http://169.253.1.1:8080}"
#------------------------------------------------------------
#Device Specific Simulation settings
#
#Two modes, chosen by web_server:
#  HUB (on)  - the engine is the source of truth; the s0-s9 buckets are IGNORED.
#              Connectivity + failure sims arrive as [username] overrides (applied
#              below, they WIN). Ambient traffic (the ping/download/www/iperf noise
#              that keeps the fleet realistic) is not pinned to a bucket anymore —
#              it is a per-sim roll driven by a WEIGHT so a 5000-client fleet spreads
#              smoothly instead of quantising into 10 bucket combos. See the ambient
#              block below for how the weight becomes a probability.
#  STANDALONE (off) - the s0-s9 bucket drives everything, self-contained (GitHub).
#              Small deployments that pull config straight from a GitHub repo with no
#              hub/engine keep using the buckets exactly as before.
#randomizable_sims + the ambient controls are delivered by the spoke in [simulation]
#(and [ambient_weights] when the operator opts into manual weighting).
#------------------------------------------------------------
randomizable_sims=$(get_value 'simulation' 'randomizable_sims')
if [[ "$web_server" == "on" ]]; then
  # --- HUB / engine-driven ambient placement -----------------------------------
  # Connectivity (wsite/ssid/ssidpw) is intentionally left blank here: the engine
  # delivers it as a [username] override that apply_override() layers on top LAST,
  # so it always wins. sim_phy is a global default for now (wireless); the T1
  # wired+wireless (Raspberry Pi) case is handled separately, see docs.
  wsite=""; ssid=""; ssidpw=""
  sim_phy=$(get_value 'simulation' 'sim_phy'); sim_phy="${sim_phy:-wireless}"
  # Start every sim OFF. The engine turns failure sims on via the [username]
  # override applied further down; the roll below is what turns AMBIENT traffic on.
  for sim in dhcp_fail dns_fail assoc_fail port_flap ssidpw_fail auth_fail \
             ping_test download iperf www_traffic; do declare -g "$sim=off"; done
  #
  # Ambient distribution — two-step model (level, then weighted split)
  # ------------------------------------------------------------------
  # STEP 1 (level): this client is "ambient-active" with probability ambient_pct
  #   (the % of the fleet doing ambient traffic). ambient_pct is delivered already
  #   scaled for THIS client's site — the spoke folded the per-site load weight in
  #   (a site weighted 3 gets 3x the level of a site weighted 1), so the client
  #   just rolls against it. If the client rolls inactive, it runs NO ambient sim.
  # STEP 2 (split): an active client runs exactly ONE randomizable sim, chosen by a
  #   WEIGHTED random pick over [ambient_weights] (relative integers, default 1).
  #   A sim weighted 3 is picked 3x as often as one weighted 1 — so it runs on 3x
  #   the clients. This is the SAME "weight N = N x clients" rule as the SSID
  #   weighted placement rules.
  #
  #   ambient_control = off (DEFAULT): every sim weight is 1 → the active clients
  #       split EVENLY across the randomizable set. The operator does nothing.
  #   ambient_control = on: per-sim weights come from [ambient_weights] and the
  #       per-site level scaling is applied (both delivered by the spoke).
  #
  # Failure sims never enter this pool — the engine owns them via [username].
  ambient_control=$(get_value 'simulation' 'ambient_control'); ambient_control="${ambient_control:-off}"
  ambient_pct=$(get_value 'simulation' 'ambient_pct'); ambient_pct="${ambient_pct:-50}"
  echo "Auto (engine-driven): ambient control=${ambient_control} level=${ambient_pct}% set=[$randomizable_sims]" | tee -a ${LOG_FILE}
  if [[ -n "$randomizable_sims" ]] && (( RANDOM % 100 < ambient_pct )); then
    # Active — weighted single-pick. First total the weights (default 1 each).
    ambient_total=0
    for sim in $randomizable_sims; do
      weight=$(get_value 'ambient_weights' "$sim"); weight="${weight:-1}"
      [[ "$weight" =~ ^[0-9]+$ ]] || weight=1
      ambient_total=$(( ambient_total + weight ))
    done
    if (( ambient_total > 0 )); then
      # Roll a point in [0, total) and walk the cumulative weights to find its sim.
      ambient_roll=$(( RANDOM % ambient_total ))
      ambient_acc=0
      for sim in $randomizable_sims; do
        weight=$(get_value 'ambient_weights' "$sim"); weight="${weight:-1}"
        [[ "$weight" =~ ^[0-9]+$ ]] || weight=1
        ambient_acc=$(( ambient_acc + weight ))
        if (( ambient_roll < ambient_acc )); then
          declare -g "$sim=on"
          echo "  ambient: active → ${sim} ON (weight ${weight}/${ambient_total})" | tee -a ${LOG_FILE}
          break
        fi
      done
    fi
  else
    echo "  ambient: inactive this cycle" | tee -a ${LOG_FILE}
  fi
else
  wsite=$(get_value $simulation_id 'wsite')
  sim_phy=$(get_value $simulation_id 'sim_phy')
  ssid=$(get_value $simulation_id 'ssid')
  ssidpw=$(get_value $simulation_id 'ssidpw')
  dhcp_fail=$(get_value $simulation_id 'dhcp_fail')
  dns_fail=$(get_value $simulation_id 'dns_fail')
  assoc_fail=$(get_value $simulation_id 'assoc_fail')
  port_flap=$(get_value $simulation_id 'port_flap')
  ping_test=$(get_value $simulation_id 'ping_test')
  download=$(get_value $simulation_id 'download')
  iperf=$(get_value $simulation_id 'iperf')
  www_traffic=$(get_value $simulation_id 'www_traffic')
  # Ambient random pool: pick a random bucket, take only its randomizable flags;
  # every failure sim is forced off (harvest-only). A [username] harvest override
  # applied just below still WINS.
  random_pool=$(get_value 'simulation' 'random_pool')
  if [[ "$random_pool" == "on" && -n "$randomizable_sims" ]]; then
    random_bucket="s$(( RANDOM % 10 ))"
    echo "Ambient random pool: rolling behaviour from bucket ${random_bucket}" | tee -a ${LOG_FILE}
    for sim in dhcp_fail dns_fail assoc_fail port_flap ssidpw_fail auth_fail \
               ping_test download iperf www_traffic; do
      if [[ " $randomizable_sims " == *" $sim "* ]]; then
        declare -g "$sim=$(get_value $random_bucket "$sim")"
      else
        declare -g "$sim=off"
      fi
    done
  fi
fi
#------------------------------------------------------------
#Simlation IP
#------------------------------------------------------------
smb_address=$(get_value 'address' 'smb_address')
ping_address=$(get_value 'address' 'ping_address')
dns_latency_1=$(get_value 'address' 'dns_latency_1')
dns_latency_2=$(get_value 'address' 'dns_latency_2')
dns_latency_3=$(get_value 'address' 'dns_latency_3')
dns_bad_ip_1=$(get_value 'address' 'dns_bad_ip_1')
dns_bad_ip_2=$(get_value 'address' 'dns_bad_ip_2')
dns_bad_ip_3=$(get_value 'address' 'dns_bad_ip_3')
dns_bad_record_1=$(get_value 'address' 'dns_bad_record_1')
dns_bad_record_2=$(get_value 'address' 'dns_bad_record_2')
dns_bad_record_3=$(get_value 'address' 'dns_bad_record_3')
iperf_server=$(get_value 'address' 'iperf_server')
collab_server=$(get_value 'address' 'collab_server')
#------------------------------------------------------------
#User/Device Specific Overrides
#------------------------------------------------------------
apply_override() {
  local var=$1
  local val=$(get_value $username "$var")
  [[ -n ${val} ]] && declare -g "$var=$val"
}
override_keys=(kill_switch sim_load github_repo repo_location site_based_ssid iperf_bw \
  wsite sim_phy ssid ssidpw dhcp_fail dns_fail assoc_fail port_flap ping_test download iperf \
  www_traffic ssidpw_fail auth_fail smb_address ping_address dns_latency_1 dns_latency_2 \
  dns_latency_3 dns_bad_ip_1 dns_bad_ip_2 dns_bad_ip_3 dns_bad_record_1 dns_bad_record_2 \
  dns_bad_record_3 iperf_server dot1x_password \
  collab collab_app collab_bw collab_time collab_server)
for key in "${override_keys[@]}"; do
  apply_override "$key"
done
#------------------------------------------------------------
#End User/Device Specific Overrides
#------------------------------------------------------------
echo $(date) | tee -a ${LOG_FILE}
echo ------------------------------| tee -a ${LOG_FILE}
echo Simulation Details: | tee -a ${LOG_FILE}
echo Hostname: $HOSTNAME | tee -a ${LOG_FILE}
echo Site: $wsite | tee -a ${LOG_FILE}
echo Site Based SSID: $site_based_ssid | tee -a ${LOG_FILE}
echo Phy: $sim_phy | tee -a ${LOG_FILE}
if [[ "$sim_phy" == "wireless" ]] && [[ -n ${wladapter} ]]; then echo Adapter: $wladapter | tee -a ${LOG_FILE}; fi
echo Simulation Load: $sim_load | tee -a ${LOG_FILE}
echo Kill Switch: $kill_switch | tee -a ${LOG_FILE}
echo DHCP Fail: $dhcp_fail | tee -a ${LOG_FILE}
echo DNS Fail: $dns_fail | tee -a ${LOG_FILE}
echo WWW Traffic: $www_traffic | tee -a ${LOG_FILE}
echo iPerf: $iperf | tee -a ${LOG_FILE}
echo "Collab: $collab app=${collab_app:-teams} server=${collab_server:-unset}" | tee -a ${LOG_FILE}
echo Download: $download | tee -a ${LOG_FILE}
echo Port Flap: $port_flap | tee -a ${LOG_FILE}
echo Incorrect SSID PW: $ssidpw_fail | tee -a ${LOG_FILE}
echo ------------------------------| tee -a ${LOG_FILE}
sleep 5
#------------------------------------------------------------
#Checking global kill switch config
#------------------------------------------------------------
gkill_switch="off"
if [[ -n "${server_url:-}" ]]; then
  _gks=$(curl -sf --max-time 5 "${server_url%/}/api/kill-switch" 2>/dev/null | tr -d '[:space:]')
  [[ "$_gks" == "on" || "$_gks" == "off" ]] && gkill_switch="$_gks"
fi
if [[ "$gkill_switch" == "off" ]] && [[ -f '/usr/local/scripts/kill_switch.txt' ]]; then
  _gks=$(cat /usr/local/scripts/kill_switch.txt 2>/dev/null | tr -d '[:space:]')
  [[ "$_gks" == "on" || "$_gks" == "off" ]] && gkill_switch="$_gks"
fi
#------------------------------------------------------------
#Generating a random number to have some variance in the scripts
#------------------------------------------------------------
rn=$((1 + RANDOM % 60))
rn_iperf_port=$((5201 + RANDOM % 10))
rn_iperf_time=$((1 + RANDOM % 300))
rn_ping_size=$((1 + RANDOM % 65000))
rn_offline_time=$((1 + RANDOM % 14400))
rn_sim_load=$((1 + RANDOM % 99))
#------------------------------------------------------------
#Getting username from hostname extraction
#changing DHCP Client configuration to send the username as the hostname
#Pure aesthetics so the usernames in Central look good
#------------------------------------------------------------
sudo sed -i "s/gethostname()/\"$(sed_escape "$username")\"/g" /etc/dhcp/dhclient.conf
#------------------------------------------------------------
# WebUI dashboard reporting
#------------------------------------------------------------
hostname=$HOSTNAME
platform=linux
declare -a error_log=()

json_escape() {
  local value="${1-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

report_error() {
  local msg="$1" severity="${2:-error}"
  echo "$(date '+%Y-%m-%dT%H:%M:%S') [$severity] $msg" | tee -a ${LOG_FILE}
  error_log+=("$(json_escape "$msg")")
  _in_report_error=true report_status "${z:-0}" || true
  unset _in_report_error
}

report_status() {
  local iteration="${1:-${z:-0}}"
  [[ "${web_server:-off}" != "on" ]] && return 0
  [[ -z "${server_url:-}" ]] && return 0
  local connected_ssid gateway_json=false first=true active_simulations=""
  connected_ssid=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes' | cut -d: -f2 | head -n1)
  [[ "${gateway_reachable:-}" == "true" ]] && gateway_json=true
  for sim in dns_fail iperf download www_traffic ping_test ssidpw_fail auth_fail dhcp_fail collab; do
    if [[ "${!sim}" == "on" ]]; then
      [[ $first == true ]] && first=false || active_simulations+=","
      active_simulations+="\"$sim\""
    fi
  done
  local errors_json="[]"
  if [[ ${#error_log[@]} -gt 0 ]]; then
    local joined
    printf -v joined '"%s",' "${error_log[@]}"
    errors_json="[${joined%,}]"
  fi
  local payload
  printf -v payload \
    '{"hostname":"%s","simulation_id":"%s","platform":"%s","iteration":%s,"connected_ssid":"%s","gateway_reachable":%s,"active_simulations":[%s],"errors":%s,"config":{"kill_switch":"%s","dns_fail":"%s","iperf":"%s","www_traffic":"%s","download":"%s","ping_test":"%s","ssidpw_fail":"%s","auth_fail":"%s","dhcp_fail":"%s","collab":"%s"}}' \
    "$(json_escape "$hostname")" "$(json_escape "${simulation_id:-}")" "$(json_escape "$platform")" \
    "$iteration" "$(json_escape "${connected_ssid:-}")" "$gateway_json" "$active_simulations" "$errors_json" \
    "$(json_escape "${kill_switch:-off}")" "$(json_escape "${dns_fail:-off}")" "$(json_escape "${iperf:-off}")" \
    "$(json_escape "${www_traffic:-off}")" "$(json_escape "${download:-off}")" "$(json_escape "${ping_test:-off}")" \
    "$(json_escape "${ssidpw_fail:-off}")" "$(json_escape "${auth_fail:-off}")" "$(json_escape "${dhcp_fail:-off}")" \
    "$(json_escape "${collab:-off}")"
  local status_file="/usr/local/scripts/client-status.json"
  printf '%s\n' "$payload" > "$status_file" 2>/dev/null && \
    { [[ -z "${_in_report_error:-}" ]] && error_log=(); } || true
  return 0
}
#------------------------------------------------------------
#Functions
#------------------------------------------------------------
#WiFi connections
#------------------------------------------------------------
# Replaces the blind `sleep 15` settle that used to follow every radio cycle.
# Polls NetworkManager for a wifi device that's past the post-power-on
# "unavailable" limbo and ready to associate; returns the instant one is ready
# (usually <15s, often ~1-2s). $1 = backstop cap (sec); on timeout returns 1 and
# the caller proceeds anyway (nmcli -w handles a not-yet-ready device itself).
_wait_radio_ready() {
  local cap="${1:-15}" i st
  for ((i=0; i<cap; i++)); do
    st=$(nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null | grep ':wifi:' | head -1)
    if [[ -n "$st" && "$st" != *":unavailable" ]]; then return 0; fi
    sleep 1
  done
  return 1
}
# Race an in-flight nmcli activation (pid $2) against a background `iw event`
# deauth watcher — no blind `nmcli -w N` / `sleep N` wait. Returns the instant
# EITHER lands:
#   * nmcli exits → nmcli's own exit code. nmcli returns only once NetworkManager
#     reaches the activated state (full association + DHCP/IP), so this is the
#     reliable SUCCESS signal (iw events are L2-only and would race DHCP, so we
#     do NOT kill nmcli on a L2 "connected" event — we let it finish IP config).
#   * AP deauth/disassoc/disconn fires → kill nmcli, return 1 (FAILURE: wrong PSK /
#     blocked MAC / RADIUS reject), detected the instant the kernel sees it.
# $1 = backstop cap (sec). Used by every connect path so success AND failure are
# event-driven instead of blindly waiting out the nmcli -w cap. Falls back to
# just waiting on nmcli (the old behavior) when iw / $wladapter is unavailable.
_connect_outcome() {
  local cap="${1:-30}" nm_pid="$2"
  if [[ -z "${wladapter:-}" ]] || ! command -v iw >/dev/null 2>&1; then
    wait "$nm_pid" 2>/dev/null; return $?
  fi
  timeout "$cap" bash -c 'iw event -m -t 2>/dev/null | grep -m1 -E "$1: (deauth|disassoc|disconn)"' \
    _ "$wladapter" >/dev/null 2>&1 &
  local ev_pid=$! i
  for ((i=0; i<cap; i++)); do
    if ! kill -0 "$nm_pid" 2>/dev/null; then
      wait "$nm_pid" 2>/dev/null; local rc=$?
      kill "$ev_pid" 2>/dev/null; wait "$ev_pid" 2>/dev/null || true
      return "$rc"
    fi
    if ! kill -0 "$ev_pid" 2>/dev/null; then
      kill "$nm_pid" 2>/dev/null; wait "$nm_pid" 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
  kill "$nm_pid" "$ev_pid" 2>/dev/null; wait 2>/dev/null || true
  return 1
}
# Wait for a wlan adapter to appear — replaces the blind `sleep 15` that used to
# precede `wladapter=$(...)` re-detection in the recovery paths. Polls up to $1
# sec; sets wladapter and returns 0 the instant one is present, 1 on timeout.
_wait_wlan_adapter() {
  local cap="${1:-15}" i
  for ((i=0; i<cap; i++)); do
    wladapter=$(ip -br a 2>/dev/null | grep "wlx\|wlan" | cut -d ' ' -f '1')
    [[ -n "$wladapter" ]] && return 0
    sleep 1
  done
  return 1
}
connect_wifi() {
  # An 802.1X (enterprise) SSID is flagged by ssid=="1X" in the Pool/SSID matrix —
  # route it to connect_1x(); every other SSID is PSK below.
  if [[ "$ssid" == "1X" ]]; then
    connect_1x "$1"
    return
  fi
  nmcli radio wifi off
  nmcli radio wifi on
  _wait_radio_ready 15   # was: blind `sleep 15` — now returns when the radio is ready
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then target_ssid="$wsite-$ssid"
  else target_ssid="$ssid"; fi
  # Event-driven: returns the instant nmcli finishes activating (success) or the
  # AP drops the link (failure) — no blind `nmcli -w $1` wait for the whole window.
  nmcli -w "$1" device wifi connect "$target_ssid" password "$ssidpw" >/dev/null 2>&1 &
  _connect_outcome "$1" $! || true
}
#------------------------------------------------------------
#Fast WiFi connection for the wrong-PSK (ssidpw_fail) loop
#------------------------------------------------------------
# The normal connect_wifi() cycles the radio + sleeps 15s + waits up to 30s per
# attempt, capping the wrong-password loop at ~4 attempts/min. To trigger the
# "WPA Passphrase is Incorrect" insight at least 10x/min we need <=6s/attempt.
# The AP records the failed WPA 4-way handshake within ~1-2s of the association
# request, so a SHORT nmcli cap still registers the event — we drop the 15s
# settle and cap nmcli at $1 sec. delete_matching_connections first forces a
# fresh association each iteration (every attempt is a distinct AP/Central
# event, not a cached reuse). $1 defaults to 5 → worst case ~5.5s/attempt ≈ 10/min.
#------------------------------------------------------------
connect_wifi_fail() {
  local cap="${1:-5}"
  delete_matching_connections
  # Fire the association and return the instant the AP rejects the bad PSK
  # (deauth/disassoc — the real "WPA Passphrase Incorrect" event) instead of
  # blocking on `nmcli -w $cap` for the whole window. $cap is only a backstop for
  # a silent AP. PSK-only (no 1X dispatch) — connect_1x_fail is the separate path.
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then target_ssid="$wsite-$ssid"
  else target_ssid="$ssid"; fi
  nmcli -w "$cap" device wifi connect "$target_ssid" password "$ssidpw" >/dev/null 2>&1 &
  _connect_outcome "$cap" $! || true
}
#------------------------------------------------------------
#WiFi 802.1X (WPA-Enterprise / PEAP-MSCHAPv2) connection
#------------------------------------------------------------
# nmcli's "device wifi connect" is PSK-only, so 802.1X needs an explicit profile
# with 802-1x.* settings. EAP identity is the short username (e.g. kbell); the
# password is the SSID password ($ssidpw). PEAP + MSCHAPv2, no server-cert
# validation (lab). ssidpw_fail flows through here too: it sets $ssidpw to the
# wrong password before connecting, so PEAP auth fails and Central logs the insight.
connect_1x() {
  _connect_1x_core "$1" 0
}
#------------------------------------------------------------
#Fast 802.1X connection for the wrong-password (ssidpw_fail) loop
#------------------------------------------------------------
# Separate from connect_wifi_fail (PSK): 1X rebuilds an explicit profile and
# brings it up, so the fast path is its own function. Like the PSK fast path it
# drops the 15s radio settle and caps nmcli at $1 sec so the loop sustains
# >=10 auth-failure attempts/min — RADIUS logs the failed EAP within ~1-2s, so a
# short cap still registers the event. $1 defaults to 5.
#------------------------------------------------------------
connect_1x_fail() {
  _connect_1x_core "${1:-5}" 1
}

_connect_1x_core() {
  local wait_time=$1 fast=$2
  local eap="${dot1x_eap:-peap}"
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi

  if [[ "$fast" == "1" ]]; then
    # No radio cycle / settle — the profile delete + rebuild below forces a
    # fresh association so each attempt is a distinct AP/RADIUS event.
    nmcli -t -f NAME connection show | grep -Fxq "$target_ssid" && nmcli connection delete "$target_ssid"
  else
    nmcli radio wifi off
    nmcli radio wifi on
    _wait_radio_ready 15   # was: blind `sleep 15` — now returns when radio is ready
    # Rebuild the profile each run so identity / password / SSID always re-apply.
    nmcli -t -f NAME connection show | grep -Fxq "$target_ssid" && nmcli connection delete "$target_ssid"
  fi

  if [[ "$eap" == "tls" ]]; then
    # EAP-TLS (cert-based) — for Cloud NAC. Certs are provisioned headlessly by
    # cloud_nac_onboard.py and referenced by path here. No password/phase2.
    if [[ -z "$dot1x_client_cert" || -z "$dot1x_private_key" || -z "$dot1x_ca_cert" ]]; then
      echo "EAP-TLS selected but cert paths missing (dot1x_client_cert/private_key/ca_cert)" | tee -a ${LOG_FILE}
      return 1
    fi
    nmcli connection add type wifi con-name "$target_ssid" ifname "$wladapter" ssid "$target_ssid" \
      wifi-sec.key-mgmt wpa-eap \
      802-1x.eap tls \
      802-1x.identity "$username" \
      802-1x.client-cert "$dot1x_client_cert" \
      802-1x.private-key "$dot1x_private_key" \
      802-1x.ca-cert "$dot1x_ca_cert" \
      802-1x.system-ca-certs no
  else
    # PEAP-MSCHAPv2 (username/password) — the legacy default.
    nmcli connection add type wifi con-name "$target_ssid" ifname "$wladapter" ssid "$target_ssid" \
      wifi-sec.key-mgmt wpa-eap \
      802-1x.eap "$eap" \
      802-1x.phase2-auth mschapv2 \
      802-1x.identity "$username" \
      802-1x.password "${dot1x_password:-$ssidpw}" \
      802-1x.system-ca-certs no
  fi

  # Event-driven: returns the instant nmcli finishes activating (success) OR the
  # AP/RADIUS rejects the creds (deauth/disassoc — failure). $wait_time backstop.
  # Both fast (wrong-password) and normal paths use the same watcher — the bad
  # password is what makes the fast path fail fast on the deauth event.
  nmcli -w "$wait_time" connection up "$target_ssid" >/dev/null 2>&1 &
  _connect_outcome "$wait_time" $! || true
}
#------------------------------------------------------------
#Connection management
#------------------------------------------------------------
manage_connection() {
  local action=$1
  local wait_time=$2
  nmcli radio wifi off
  nmcli radio wifi on
  _wait_radio_ready 15   # was: blind `sleep 15` — now returns when radio is ready
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi
  if [[ "$action" == "up" ]]; then
    # Event-driven: returns the instant nmcli finishes activating (success) or
    # the AP drops the link (failure — e.g. a blocked-MAC deauth in the auth_fail
    # flap loop) instead of blocking on `nmcli -w $wait_time`. $wait_time backstop.
    nmcli -w "$wait_time" connection up "$target_ssid" >/dev/null 2>&1 &
    _connect_outcome "$wait_time" $! || true
  else
    # down: nmcli deactivates immediately (no long wait) — no event watch needed.
    nmcli -w "$wait_time" connection down "$target_ssid" >/dev/null 2>&1 || true
  fi
}
#------------------------------------------------------------
#Run simulation scripts
#------------------------------------------------------------
run_simulation() {
 local script=$1
 local sleep_time=$2
 if [ -f "/usr/local/scripts/$script" ]; then
  nohup bash "/usr/local/scripts/$script" >> /usr/local/scripts/sim.log 2>&1 &
  sleep $sleep_time
 fi
}
#------------------------------------------------------------
#DHCP-fail MAC spoof REMOVED.
# dhcp_fail no longer spoofs the adapter MAC. The failure is now driven by a
# standalone dhcp_fail.sh that fires crafted DHCP requests at the real
# (detected) DHCP server and at a dead server (10.10.10.10) with a forged
# client-id — see clients/linux/dhcp_fail.sh. The client's real lease and
# NM profile (including cloned-mac-address) are left untouched here.
#------------------------------------------------------------
#------------------------------------------------------------
#Attempting WiFi connection
#------------------------------------------------------------
connect_wifi 30
#------------------------------------------------------------
#Dumping Current Device List
#------------------------------------------------------------
echo Disabling unused interface | tee -a ${LOG_FILE}
if [[ "$sim_phy" == "ethernet" ]]; then sudo ip link set dev $wladapter down; fi
if [[ "$sim_phy" == "wireless" ]]; then ea_down; fi
#------------------------------------------------------------
#Checking to see if the default gateway is reachable
#------------------------------------------------------------
wladapter=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
sudo rfkill unblock wifi; sudo rfkill unblock all
dfgw=$(ip route | grep -oP 'default via \K\S+')
if ping -c2 "$dfgw"; then gw_ok=true; else gw_ok=false; fi
if [[ "$sim_phy" == "wireless" ]]; then
  # Wireless still requires the wlan adapter to be present; on failure
  # reconnect the WiFi as before.
  if [[ "$gw_ok" == true && -n "${wladapter}" ]]; then
    echo Successful network connection | tee -a ${LOG_FILE}
  else
    echo Network connection failed | tee -a ${LOG_FILE}
    _wait_wlan_adapter 15   # was: blind `sleep 15` — polls until the adapter appears
    connect_wifi 180
    dfgw=$(ip route | grep -oP 'default via \K\S+')
  fi
else
  # Ethernet just needs the gateway ping to succeed — no WiFi fallback.
  if [[ "$gw_ok" == true ]]; then
    echo Successful network connection | tee -a ${LOG_FILE}
  else
    echo Network connection failed | tee -a ${LOG_FILE}
  fi
fi
#------------------------------------------------------------
#Begin Setting up simulation load
#------------------------------------------------------------
if [[ "$sim_load" -lt "$rn_sim_load" ]]; then
  echo Simulation load under threshold | tee -a ${LOG_FILE}
  echo Skipping Simulations but staying associated | tee -a ${LOG_FILE}
  if [[ "$ssidpw_fail" != "on" ]] && [[ -n ${wladapter} ]]; then
    manage_connection up 180   # event-driven: returns on activation (no post-connect sleep needed)
  fi
fi
#------------------------------------------------------------
#End Setting up simulation load
#------------------------------------------------------------
echo Kill Switch is $kill_switch | tee -a ${LOG_FILE}
if [ "$kill_switch" != "on" ]; then
 # Snapshot the config's mtime so the loop can spot a pushed change mid-cycle.
 _cfg_mtime=$(stat -c %Y /usr/local/scripts/simulation.conf 2>/dev/null)
 for z in {1..100}; do
  #------------------------------------------------------------
  # Rapid update — MUST fire every iteration REGARDLESS of which
  # simulation is running. It sits at the very top of the loop, before
  # the ssidpw_fail/auth_fail branch and before the connectivity gate,
  # so update.sh polls config/scripts frequently for EVERY client. It
  # used to live at the bottom of the non-auth-fail branch, where an
  # auth_fail/ssidpw_fail client (which takes the other branch) or any
  # connectivity `continue 2` skipped it entirely — so rapid_update
  # silently never ran for those clients.
  #------------------------------------------------------------
  if [[ "$rapid_update" == "on" ]]; then source '/usr/local/scripts/update.sh'; fi
  #------------------------------------------------------------
  # Self-re-exec: if update.sh (or anything) just replaced simulation.sh on disk,
  # relaunch into the NEW code in place — same PID, no reboot, no sudo. A running
  # sourced loop otherwise keeps executing the OLD code no matter how many times
  # update.sh runs. Validate syntax first so a truncated/partial copy can't kill
  # the loop; on failure keep the current code and re-check next pass.
  #------------------------------------------------------------
  _now_mtime=$(stat -c %Y "$_self_path" 2>/dev/null)
  if [[ -n "$_self_mtime" && -n "$_now_mtime" && "$_now_mtime" != "$_self_mtime" ]]; then
    if bash -n "$_self_path" 2>/dev/null; then
      echo "simulation.sh changed on disk — re-exec'ing into new code" | tee -a ${LOG_FILE}
      exec bash "$_self_path"
    else
      echo "simulation.sh changed but failed syntax check — staying on current code" | tee -a ${LOG_FILE}
      _self_mtime=$_now_mtime
    fi
  fi
  #------------------------------------------------------------
  # Break-to-reload: if update.sh just pulled a CHANGED simulation.conf (its mtime
  # moved) or a USR1 reload was requested, break out so the outer loop re-runs
  # init_simulation_context and re-reads every flag. A pushed change then lands on
  # the next pass instead of waiting out the full 100-iteration cycle.
  #------------------------------------------------------------
  if [[ "${_sim_reload:-0}" == 1 || "$(stat -c %Y /usr/local/scripts/simulation.conf 2>/dev/null)" != "$_cfg_mtime" ]]; then
    echo "Config changed — reloading simulation" | tee -a ${LOG_FILE}
    break
  fi
  # Beacon the CURRENT config at the TOP of the loop so the dashboard reflects it
  # right away — the report_status at the bottom only fires after all the sims
  # (each a 30s run) finish, which is minutes of staleness.
  report_status $z
  #------------------------------------------------------------
  #SSID Incorrect Password Simulation or Auth Failure Simulation
  #since these are very similar they are in the same section one
  #has a bad PSK and others have a blocked mac or invalud username/password combo
  #both need to be constantly connecting so we trigger insights
  #------------------------------------------------------------
  if [[ ($ssidpw_fail == "on" || $auth_fail == "on") && -n ${wladapter} ]]; then
    if [[ "$ssidpw_fail" == "on" ]]; then
     # Base the wrong password on the client's EFFECTIVE password: the
     # [username]/cell override (get_value $username) wins over the [s0-s9]
     # bucket, so it's genuinely one char off the REAL password for the SSID this
     # client associates to. Re-resolve here — apply_override's $ssidpw may have
     # been clobbered by a prior iteration's restore.
     real_ssidpw=$(get_value $username 'ssidpw'); [[ -z "$real_ssidpw" ]] && real_ssidpw=$(get_value $simulation_id 'ssidpw')
     # Cloud-NAC (1X) clients authenticate with their per-user dot1x_password, not
     # $ssidpw — so corrupt THAT too when it's set, otherwise connect_1x would use
     # the correct password and the wrong-password sim wouldn't actually fail.
     real_dot1x=$(get_value $username 'dot1x_password')
     for i in {1..100}; do
      echo Running SSID Incorrect Password | tee -a ${LOG_FILE}
      ssidpw="${real_ssidpw}_fail"
      [[ -n "$real_dot1x" ]] && dot1x_password="${real_dot1x}_fail"
      echo Iteration $i of 100 | tee -a ${LOG_FILE}
      # Fast wrong-password attempt (>=10 failures/min): the PSK and 1X fail
      # functions are separate — pick by SSID type. Each drops the 15s settle and
      # caps nmcli at 5s; the AP/RADIUS records the failed handshake/auth within
      # ~1-2s, so a short cap still registers every attempt as a distinct event.
      if [[ "$ssid" == "1X" ]]; then
        connect_1x_fail 5
      else
        connect_wifi_fail 5
      fi
     done
    fi
    if [[ "$auth_fail" == "on" ]]; then
     echo Running Auth Failure | tee -a ${LOG_FILE}
     for i in {1..100}; do
      echo Enable/Disable WLAN interface | tee -a ${LOG_FILE}
      echo Iteration $i of 100 | tee -a ${LOG_FILE}
      delete_matching_connections
      # Event-driven flap: `manage_connection up` returns the instant the AP
      # rejects the blocked MAC / invalid creds (deauth) instead of blocking on
      # nmcli -w 5 + a blind sleep 5; `down` deactivates immediately.
      manage_connection up 5
      manage_connection down 5
     done
    fi
   #------------------------------------------------------------
   #Resetting the WIFI Password so it can connect correctly for updates/maintenance
   #------------------------------------------------------------
   ssidpw=$(get_value $username 'ssidpw'); [[ -z "$ssidpw" ]] && ssidpw=$(get_value $simulation_id 'ssidpw')
   connect_wifi 5
   #------------------------------------------------------------
   #End SSID Incorrect Password Simualtion or Auth Failure Simulation
   #------------------------------------------------------------
  else
   #------------------------------------------------------------
   #If SSID Incorrect Password Sim is not triggered then check
   #for the other simualtions
   #------------------------------------------------------------
   if ! ping -c2 "$dfgw"; then
     echo Attempting to reset adapter | tee -a ${LOG_FILE}
     _wait_wlan_adapter 15   # was: blind `sleep 15` — polls until the adapter appears
     delete_matching_connections
     connect_wifi 180
     echo WLAN Adapter name $wladapter | tee -a ${LOG_FILE}
    fi
    dfgw=$(ip route | grep -oP 'default via \K\S+')
    if ping -c2 "$dfgw"; then
     echo Successful network connection | tee -a ${LOG_FILE}
    else
    echo Connection failed muiltiple times | tee -a ${LOG_FILE}
    echo Resetting configuration | tee -a ${LOG_FILE}
    #------------------------------------------------------------
    #Cleaning up old network connection profiles
    #------------------------------------------------------------
    delete_matching_connections
    #------------------------------------------------------------
    #Looping Script - Network Connectivity Failed
    #------------------------------------------------------------
    continue 2
   fi
   #------------------------------------------------------------
   #End Connecting to Network
   #------------------------------------------------------------
   #Running WWW Traffic Simulation
   #------------------------------------------------------------
    if [[ "$www_traffic" == "on" ]]; then
     echo Running WWW Traffic simulation
     wwwfile=($(< /usr/local/scripts/websites.txt))
     rn_www=$((RANDOM % ${#wwwfile[@]}))
     url="${wwwfile[$rn_www]}"
     echo $(date) | tee -a ${LOG_FILE}
     echo ------------------------------| tee -a ${LOG_FILE}
     echo Phy: $sim_phy | tee -a ${LOG_FILE}
     echo Simulation Load: $sim_load | tee -a ${LOG_FILE}
     echo Website: $url | tee -a ${LOG_FILE}
     echo ------------------------------| tee -a ${LOG_FILE}
     cpulimit -l 25 -- firefox-esr --headless "$url" &
     www_traffic=off
    fi
   #------------------------------------------------------------
   #End WWW Traffic Simulation
   #------------------------------------------------------------
   #Running ping simulation
   #------------------------------------------------------------
   if [[ "$ping_test" == "on" ]]; then
    run_simulation "ping_test.sh" 30
   fi
   #------------------------------------------------------------
   #End Ping Simulation

    #Running iPerf simulation
    #------------------------------------------------------------
    if [[ "$iperf" == "on" ]]; then
     run_simulation "iperf.sh" 30
    fi
    #------------------------------------------------------------
    #Running Collaboration (Teams/Zoom/WebEx) UDP media simulation
    #------------------------------------------------------------
    # Raw UDP to collab_server (hub sink) over the wired/USB path — the media
    # never rides the WS control plane. See collab.sh / collab.py.
    if [[ "$collab" == "on" ]]; then
     run_simulation "collab.sh" 30
    fi
    #------------------------------------------------------------
    #Running download simulation
    #------------------------------------------------------------
    if [[ "$download" == "on" ]]; then
     run_simulation "download.sh" 30
    fi
    #------------------------------------------------------------
    #Running DNS Fail simulation
    #------------------------------------------------------------
    if [[ "$dns_fail" == "on" ]]; then
     run_simulation "dns_fail.sh" 30
    fi
   #------------------------------------------------------------
   #End DNS Fail Simulation
   #------------------------------------------------------------
    #Running DHCP Fail simulation
    #------------------------------------------------------------
    # dhcp_fail.sh fires crafted DHCPDISCOVERs (forged id -> real server, real
    # mac -> dead 10.10.10.10) for 100 attempts then exits; the sim loop
    # relaunches it. The client's real wifi/MAC are NOT touched.
    if [[ "$dhcp_fail" == "on" ]]; then
     run_simulation "dhcp_fail.sh" 30
    fi
   #------------------------------------------------------------
   #End DHCP Fail Simulation
   #------------------------------------------------------------
   echo End of simulation | tee -a ${LOG_FILE}
   #------------------------------------------------------------
   # (rapid_update now runs at the TOP of the loop so it fires for
   # every client regardless of simulation — see above.)
   #------------------------------------------------------------
   report_status $z
   echo Sleeping for 5 seconds | tee -a ${LOG_FILE}
   echo Loop iteration $z of 100 | tee -a ${LOG_FILE}
   sleep 5
   #------------------------------------------------------------
   #End of 100 Loop Count
   #------------------------------------------------------------
  fi
 done
else
 #------------------------------------------------------------
 #If kill switch is enabled - sleeping for 5 minutes then restarting the loop
 #------------------------------------------------------------
 echo Kill switch enabled - sleeping for 5 minutes
 # Still poll for updates while kill-switched so the client can pick up a
 # config change that turns the kill switch back OFF (update.sh pulls a fresh
 # simulation.conf; init_simulation_context re-reads it next outer loop).
 if [[ "$rapid_update" == "on" ]]; then source '/usr/local/scripts/update.sh'; fi
 sleep 300
fi
#------------------------------------------------------------
#Killing Firefox simulation
#------------------------------------------------------------
echo Closing Firefox | tee -a ${LOG_FILE}
pkill -f firefox &
#------------------------------------------------------------
#End Kill switch Check 
#------------------------------------------------------------
#------------------------------------------------------------
#Running apt update & apt upgrade
#------------------------------------------------------------
echo Running Updates | tee -a ${LOG_FILE}
bash /usr/local/scripts/apt_update.sh &
if [[ "$allow_offline" == "yes" ]]; then
  #------------------------------------------------------------
  #Bringing all interfaces down to make it look like the device is offline.
  #Otherwise they get triggered as IOT since they are always connected.
  #------------------------------------------------------------
  echo Bringing all interfaces down | tee -a ${LOG_FILE}
  if [[ -n ${wladapter} ]]; then sudo ip link set dev $wladapter down; fi
  if [[ -n ${eadapter} ]]; then sudo ip link set dev $eadapter down; fi
  echo Sleeping for $rn_offline_time seconds
  echo ------------------------------| tee -a ${LOG_FILE}
  #------------------------------------------------------------
  #Sleep for up to 4 hours to show the device left
  #------------------------------------------------------------
  sleep $rn_offline_time
  #------------------------------------------------------------
  #Bringing all interfaces back up to call home/update scripts
  #------------------------------------------------------------
  echo Bringing all interfaces online | tee -a ${LOG_FILE}
  if [[ -n ${eadapter} ]]; then sudo ip link set dev $eadapter up; fi
  if [[ -n ${wladapter} ]]; then sudo ip link set dev $wladapter up; fi
  echo ------------------------------| tee -a ${LOG_FILE}
fi
#------------------------------------------------------------
#Looping Script
#------------------------------------------------------------
done