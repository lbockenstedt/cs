#!/bin/bash
version=0.01
LOG_FILE=/usr/local/scripts/sim.log

echo $(date) | tee -a ${LOG_FILE}
echo ------------------------------| tee -a ${LOG_FILE}
echo Simulation Script Version $version | tee -a ${LOG_FILE}
#------------------------------------------------------------
#DO NOT EDIT BELOW THIS LINE UNLESS YOU KNOW WHAT YOU ARE DOING
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
# Shared helpers (derive_username/derive_bucket/adapter detection/
# apply_overrides/json_escape) — canonical source clients/lib/common.sh.
source '/usr/local/scripts/common.sh'
# Network connect / adapter / reset helpers — split into sourced files so each
# concern (PSK connect, 1X connect, recovery, shared waits/state) can be edited
# in isolation WITHOUT touching the others or this orchestrator. Order matters:
# network_common.sh defines the shared helpers + the _reconnect_fails state that
# the other three call, so it MUST be sourced first.
source '/usr/local/scripts/network_common.sh'
source '/usr/local/scripts/connect_psk.sh'
source '/usr/local/scripts/connect_1x.sh'
source '/usr/local/scripts/recovery.sh'
# T3 IoT-fleet mode (vwlan multi-device) — detect_t3_pci + run_iot_simulation.
# Guarded: a T1/T2 client that hasn't been shipped iot_sim.sh yet simply never
# enters iot mode (the branch below type-checks detect_t3_pci first).
[[ -f '/usr/local/scripts/iot_sim.sh' ]] && source '/usr/local/scripts/iot_sim.sh'

#------------------------------------------------------------
# Remote-control support — let agent.sh find and signal this loop.
# startup.sh runs `source .../simulation.sh`, so $$ here is the process
# actually running the sim loop (argv is startup.sh, so pgrep can't match).
# Writing our own PID lets agent.sh signal us via the PID file.
#------------------------------------------------------------
echo $$ > /usr/local/scripts/simulation.pid 2>/dev/null || true
_sim_reload=0
_sleep_pid=""
# _reconnect_fails + _RADIO_CYCLE_AFTER (the connect-state variables) now live
# in network_common.sh (sourced above) — they are owned by the connect paths.
# The USR1 trap below stays HERE: it drives the orchestrator reload + the
# offline-sleep wake, not the connect paths.
# USR1 (from agent.sh restart_sim / kill_switch) triggers a config re-read
# on the next outer-loop iteration instead of default-terminating the process.
# If we're inside an interruptible sleep (_isleep, e.g. the offline window),
# kill that sleep child so the loop wakes immediately and re-reads config — an
# offline client is otherwise deaf to updates for up to 4h.
trap '_sim_reload=1; [[ -n "${_sleep_pid:-}" ]] && kill "$_sleep_pid" 2>/dev/null' USR1

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


init_simulation_context() {
  # Load simulation.conf only — process_ini_file resets ALL section data on each
  # call, so a second call for user-overrides.conf would wipe [simulation]/[s0-s9].
  # Username-section overrides are defined in simulation.conf; apply_override()
  # reads them from there via get_value $username.
  process_ini_file '/usr/local/scripts/simulation.conf'
  derive_username
  # Hash the hostname to assign a bucket — produces s0-s9 deterministically.
  # (cached once per boot — see common.sh derive_bucket)
  derive_bucket
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
# The four network files sourced at the top of this file. We watch their mtimes
# alongside simulation.sh's own so an edit to ANY of them re-execs the loop into
# fresh code (same mechanism as the old single-file check below, just covering
# every file we source). Listed once here, used by the in-loop change check.
_NET_SOURCE_FILES=(
  /usr/local/scripts/network_common.sh
  /usr/local/scripts/connect_psk.sh
  /usr/local/scripts/connect_1x.sh
  /usr/local/scripts/recovery.sh
)
# One mtime fingerprint of simulation.sh + every sourced network file, compared
# each loop iteration: if it differs, something we source changed and we re-exec
# (after a bash -n on every file). Built by _source_mtime_fingerprint just below.
_source_mtime_fingerprint() {
  # Print each file's mtime in a fixed order. stat skips a missing file (stderr
  # suppressed), which changes the line count and trips a re-exec.
  stat -c '%Y' "$_self_path" "${_NET_SOURCE_FILES[@]}" 2>/dev/null
}
_source_mtime_snapshot=$(_source_mtime_fingerprint)

#------------------------------------------------------------
# rapid_update time gate — with rapid_update=on the 100-iteration loop used to
# source the FULL update.sh on EVERY pass (config re-parse + TCP probe + curl
# /api/health each time, every ~5-10s). The update source stays exactly where
# it is in the loop (its placement fixed a real bug — see the comment there);
# this gate just skips it until 60s have elapsed since the last run. The
# last-run epoch lives in a variable AND a /tmp stamp file so a re-exec'd
# script (same PID, fresh variables) doesn't immediately re-run the update;
# update.sh refreshes the stamp itself on completion so standalone runs
# (startup.sh, agent update_now) count toward the gate too.
#------------------------------------------------------------
_UPD_STAMP="/tmp/client-sim-update.stamp"
_upd_last=""
_update_due() {
  local now=""
  # Fork-free epoch on bash >=4.2; date fallback + fail-OPEN (run the update)
  # if the time can't be read — never wedge rapid_update off.
  printf -v now '%(%s)T' -1 2>/dev/null || true
  [[ "${now:-}" =~ ^[0-9]+$ ]] || now=$(date +%s 2>/dev/null)
  [[ "${now:-}" =~ ^[0-9]+$ ]] || return 0
  if [[ ! "${_upd_last:-}" =~ ^[0-9]+$ ]]; then
    _upd_last=$(cat "$_UPD_STAMP" 2>/dev/null)
    [[ "${_upd_last:-}" =~ ^[0-9]+$ ]] || _upd_last=0
  fi
  if (( now - _upd_last < 60 )); then
    return 1
  fi
  _upd_last=$now
  # Persist the stamp. GROUP the redirect under 2>/dev/null — a bare
  # `> file 2>/dev/null` does NOT suppress a redirection-OPEN failure (the error
  # is printed before the stderr redirect applies), so a root-owned stamp (see
  # below) leaked "permission denied" every gate check. chmod 0666 so any tier
  # can rewrite it: update.sh may run as root (startup.sh at boot) and would
  # otherwise leave it root:644, locking the unprivileged sim user out. In-memory
  # _upd_last above still gates this process even if the write can't land.
  { printf '%s' "$now" > "$_UPD_STAMP"; } 2>/dev/null || true
  chmod 0666 "$_UPD_STAMP" 2>/dev/null || true
  return 0
}

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

# Randomized sleep — desynchronizes the fleet so 1000 clients don't all step
# in lockstep. Sleeps a uniformly random duration in [N, 2N]: _rsleep 30 sleeps
# 30-60s, _rsleep 5 sleeps 5-10s. Used for the pacing/stagger timers only (the
# poll loops below use a fixed 1s cadence on purpose; the offline window and
# kill-switch sleep are handled separately). Defined BEFORE the loop: it is
# first called mid-loop (the settle after the config dump) well before the
# in-loop function block, so a loop-body definition left the first iteration
# with "_rsleep: command not found".
_rsleep() {
  local base="${1:-1}"
  sleep $(( base + RANDOM % (base + 1) ))
}

# Announce which build is running (own version= + CI-maintained deploy VERSION).
# Re-prints on every self-re-exec, so the terminal always shows the live build.
sim_version_banner simulation.sh "$version"

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
detect_wlan_adapter
if [[ -n ${wladapter} ]]; then echo WLAN Adapter name $wladapter | tee -a ${LOG_FILE}; fi
detect_eth_adapter
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
# sim_load MUST be numeric — the `-lt` gate below throws "integer expression
# expected" on an empty/non-numeric value. Coerce: strip to digits, default 0.
[[ "$sim_load" =~ ^[0-9]+$ ]] || sim_load=0
github_repo=$(get_value 'simulation' 'github_repo')
repo_location=$(get_value 'simulation' 'repo_location')
site_based_ssid=$(get_value 'simulation' 'site_based_ssid')
iperf_bw=$(get_value 'simulation' 'iperf_bw')
auth_fail=$(get_value 'simulation' 'auth_fail')
ssidpw_fail=$(get_value 'simulation' 'ssidpw_fail')
# mac_auth_fail: same "connectivity-failure, inline in the connect loop" kind as
# ssidpw_fail/auth_fail (see the section below) — associates with a fixed,
# PREDICTABLE spoofed MAC (mac_auth_fail_mac, [address]) so the operator can
# pre-configure that exact MAC as a RADIUS/ClearPass MAC-Auth deny entry and
# watch it get rejected repeatedly.
mac_auth_fail=$(get_value 'simulation' 'mac_auth_fail')
allow_offline=$(get_value 'simulation' 'allow_offline')
web_server=$(get_value 'simulation' 'web_server')
# Default to ON (hub mode); flip to off ONLY when the conf literally says "off"
# so an unreadable/missing config (empty value) stays ON. See update.sh.
[[ "$web_server" != "off" ]] && web_server="on"
# A [username] override may flip web_server (CS_OVERRIDE_KEYS superset —
# dashboard.sh always honored it, this script didn't). Apply it BEFORE the
# hub/standalone branch below reads it, so the mode decision and the later
# apply_overrides pass agree.
apply_override web_server
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
  for sim in dhcp_fail dns_fail dns_latency assoc_fail port_flap ssidpw_fail auth_fail \
             mac_auth_fail ping_test download iperf www_traffic; do declare -g "$sim=off"; done
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
  dns_latency=$(get_value $simulation_id 'dns_latency')
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
               mac_auth_fail ping_test download iperf www_traffic; do
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
dns_bad_ip_1=$(get_value 'address' 'dns_bad_ip_1')
dns_bad_ip_2=$(get_value 'address' 'dns_bad_ip_2')
dns_bad_ip_3=$(get_value 'address' 'dns_bad_ip_3')
dns_bad_record_1=$(get_value 'address' 'dns_bad_record_1')
dns_bad_record_2=$(get_value 'address' 'dns_bad_record_2')
dns_bad_record_3=$(get_value 'address' 'dns_bad_record_3')
iperf_server=$(get_value 'address' 'iperf_server')
collab_server=$(get_value 'address' 'collab_server')
# mac_auth_fail_mac: the SHARED, predictable spoofed MAC every mac_auth_fail
# client associates with (same value fleet-wide — a single known RADIUS/
# ClearPass deny-list entry, matching how ssidpw_fail corrupts the SAME real
# password by the same rule rather than deriving a per-client value).
mac_auth_fail_mac=$(get_value 'address' 'mac_auth_fail_mac')
#------------------------------------------------------------
#User/Device Specific Overrides — apply_override + the shared SUPERSET
#CS_OVERRIDE_KEYS list live in common.sh (kept in sync with dashboard.sh).
#------------------------------------------------------------
apply_overrides
# A [username] override can re-set sim_load to non-numeric — re-coerce so the
# `-lt` gate stays safe.
[[ "$sim_load" =~ ^[0-9]+$ ]] || sim_load=0
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
echo DNS Latency: $dns_latency | tee -a ${LOG_FILE}
echo WWW Traffic: $www_traffic | tee -a ${LOG_FILE}
echo iPerf: $iperf | tee -a ${LOG_FILE}
echo "Collab: $collab app=${collab_app:-teams} server=${collab_server:-unset}" | tee -a ${LOG_FILE}
echo Download: $download | tee -a ${LOG_FILE}
echo Port Flap: $port_flap | tee -a ${LOG_FILE}
echo Incorrect SSID PW: $ssidpw_fail | tee -a ${LOG_FILE}
echo "MAC Auth Fail: $mac_auth_fail (target mac=${mac_auth_fail_mac:-unset})" | tee -a ${LOG_FILE}
echo ------------------------------| tee -a ${LOG_FILE}
_rsleep 5
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
# Run ONCE per boot: after the first sed, gethostname() is gone so the
# substitution is idempotent — but we still forked sudo+sed every cycle for
# nothing. Guard with a marker so we skip the work once it's done. username
# never changes, so a single substitution is correct.
if [[ ! -f /usr/local/scripts/.dhcp_hostname_done ]]; then
  sudo sed -i "s/gethostname()/\"$(sed_escape "$username")\"/g" /etc/dhcp/dhclient.conf
  touch /usr/local/scripts/.dhcp_hostname_done 2>/dev/null || true
fi
#------------------------------------------------------------
# WebUI dashboard reporting
#------------------------------------------------------------
hostname=$HOSTNAME
platform=linux
declare -a error_log=()

# json_escape lives in common.sh (shared).

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
  local connected_ssid sim_ip gateway_json=false first=true active_simulations=""
  connected_ssid=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes' | cut -d: -f2 | head -n1)
  # IPv4 source address of the default route (the sim interface's address);
  # works for both wifi and wired. Empty when there's no route / no IP yet —
  # relayed to the hub so a T2 client that never got an IP can be detected
  # (its heartbeat rides a separate backend network, so absence here ≠ offline).
  sim_ip=$(ip -4 -o route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -n1)
  [[ "${gateway_reachable:-}" == "true" ]] && gateway_json=true
  for sim in dns_fail dns_latency assoc_fail port_flap iperf download www_traffic ping_test ssidpw_fail auth_fail mac_auth_fail dhcp_fail collab; do
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
  # Report the client's current DNS self-throttle rate (failures/min the AIMD
  # ratchet settled on for this dongle) up to the hub — VISIBILITY only (the
  # dashboard badge). 0 = not yet throttled / sidelined.
  local dns_ceiling; dns_ceiling=$(_dns_ceiling_saved); [[ "$dns_ceiling" =~ ^[0-9]+$ ]] || dns_ceiling=0
  local payload
  printf -v payload \
    '{"hostname":"%s","simulation_id":"%s","platform":"%s","iteration":%s,"connected_ssid":"%s","ip":"%s","gateway_reachable":%s,"dns_ceiling":%s,"active_simulations":[%s],"errors":%s,"config":{"kill_switch":"%s","dns_fail":"%s","dns_latency":"%s","iperf":"%s","www_traffic":"%s","download":"%s","ping_test":"%s","ssidpw_fail":"%s","auth_fail":"%s","mac_auth_fail":"%s","dhcp_fail":"%s","collab":"%s"}}' \
    "$(json_escape "$hostname")" "$(json_escape "${simulation_id:-}")" "$(json_escape "$platform")" \
    "$iteration" "$(json_escape "${connected_ssid:-}")" "$(json_escape "${sim_ip:-}")" "$gateway_json" "$dns_ceiling" "$active_simulations" "$errors_json" \
    "$(json_escape "${kill_switch:-off}")" "$(json_escape "${dns_fail:-off}")" "$(json_escape "${dns_latency:-off}")" "$(json_escape "${iperf:-off}")" \
    "$(json_escape "${www_traffic:-off}")" "$(json_escape "${download:-off}")" "$(json_escape "${ping_test:-off}")" \
    "$(json_escape "${ssidpw_fail:-off}")" "$(json_escape "${auth_fail:-off}")" "$(json_escape "${mac_auth_fail:-off}")" "$(json_escape "${dhcp_fail:-off}")" \
    "$(json_escape "${collab:-off}")"
  local status_file="/usr/local/scripts/client-status.json"
  printf '%s\n' "$payload" > "$status_file" 2>/dev/null && \
    { [[ -z "${_in_report_error:-}" ]] && error_log=(); } || true
  return 0
}
#------------------------------------------------------------
#Functions
#------------------------------------------------------------
# WiFi connect / adapter / reset functions now live in SOURCED files (sourced at
# the top of this script), split by concern so each can be edited in isolation:
#   network_common.sh  — shared waits + connect state (_reconnect_fails etc.)
#   connect_psk.sh     — connect_wifi, connect_wifi_fail
#   connect_1x.sh      — connect_1x, connect_1x_fail
#   recovery.sh        — manage_connection
# What remains below are the orchestrator-only helpers: interruptible sleep
# (the offline window) and the apt-update-once-per-cycle gate.
#------------------------------------------------------------
# Interruptible sleep — broken early by USR1 (the trap kills this sleep child).
# Used for the offline window so a config update / repurpose lands immediately
# instead of waiting out up to 4h with the interfaces down (deaf to updates).
# Returns when the sleep finishes OR USR1 arrives; caller checks _sim_reload.
_isleep() {
  local secs="${1:-1}"
  sleep "$secs" & _sleep_pid=$!
  wait "$_sleep_pid" 2>/dev/null || true
  _sleep_pid=""
}
# Fire apt_update.sh once per outer cycle, the FIRST time we know the network is
# up — not blindly at the end of the loop (which may be after the network
# dropped or after the offline sleep). _apt_done is reset once per outer cycle;
# the first caller launches apt_update.sh in the background and sets the guard,
# every later caller no-ops. The end-of-cycle call is the "at least once per
# cycle" fallback for the case where the network never came up.
_run_apt_once() {
  if [[ "${_apt_done:-0}" == 0 ]]; then
    echo Running Updates | tee -a ${LOG_FILE}
    bash /usr/local/scripts/apt_update.sh &
    _apt_done=1
  fi
}
#------------------------------------------------------------
#Run simulation scripts
#------------------------------------------------------------
run_simulation() {
 local script=$1
 local sleep_time=$2
 if [ -f "/usr/local/scripts/$script" ]; then
  # Single-instance, NON-DESTRUCTIVE: if a copy of THIS sim is already running,
  # leave it running and just take the inter-sim pause. run_simulation fires every
  # inner-loop pass (~30s), but a sim's burst runs far longer (dns_fail_duration=
  # 600s). The old code pkill'd + relaunched on EVERY pass, which chopped the 600s
  # burst into ~30s fragments and dropped fleet DNS volume below the controller's
  # alert threshold (~200 failures/5min) — the DNS alert stopped firing. Skip-if-
  # running lets the burst run to completion while still preventing the 10-15x
  # stacking that pegged CPU. Match the exact script PATH so dns_fail.sh never
  # matches dns_latency.sh; `pgrep -f` matches the launched `bash <path>` line
  # (not run_simulation, whose cmdline is simulation.sh, nor tee's).
  if pgrep -f "/usr/local/scripts/$script" >/dev/null 2>&1; then
    _rsleep "$sleep_time"
    return
  fi
  # Auto-reclone if the sim client stopped reporting
  if [ "$(get_value '$username' 'client_last_seen_age_s')" -gt 300 ]; then
    echo Cloning VM due to client inactivity | tee -a ${LOG_FILE}
    bash /usr/local/scripts/auto_reclone.sh &
  fi
  case "$script" in
    dns_fail.sh|dns_latency.sh)
      # DNS sims: surface each fire-and-forget dig on the LIVE console (this
      # loop's stdout — a terminal when simulation.sh is run interactively) AND
      # still append to sim.log, via tee. nohup's stdout is the PIPE (not a tty),
      # so it won't hijack output to nohup.out; when the sim's burst finishes and
      # it exits, the pipe EOFs and tee exits — no orphan. Other sims stay quiet
      # in the log only.
      nohup bash "/usr/local/scripts/$script" 2>&1 | tee -a /usr/local/scripts/sim.log &
      ;;
    *)
      nohup bash "/usr/local/scripts/$script" >> /usr/local/scripts/sim.log 2>&1 &
      ;;
  esac
  _rsleep "$sleep_time"
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
# T3 IoT-fleet mode — skip the single-client path entirely.
# If this guest has the passed-through T3 WiFi adapter (its PCI vid:pid is in the
# cs-module t3_pci_vidpids list), it is an IoT host: run the vwlan IoT-fleet sim
# (iot_sim.sh), heartbeat, take an inter-cycle sleep, and re-enter the outer loop
# (which re-reads config each pass). type-guarded so a client without iot_sim.sh
# deployed falls straight through to the normal T1/T2 path.
#------------------------------------------------------------
if type -t detect_t3_pci >/dev/null 2>&1 && detect_t3_pci; then
  echo "T3 adapter detected — running IoT-fleet (vwlan) simulation" | tee -a ${LOG_FILE}
  run_iot_simulation "$ssid" "$ssidpw"
  z=$(( ${z:-0} + 1 ))
  report_status "$z"
  iot_cycle=$(get_value 'simulation' 'iot_cycle_sleep'); [[ "$iot_cycle" =~ ^[0-9]+$ ]] || iot_cycle=300
  _isleep "$iot_cycle"
  continue
fi
#------------------------------------------------------------
#Attempting WiFi connection
#------------------------------------------------------------
# Ethernet sims don't use wifi — skip the association+teardown so we don't key
# the radio and burn a full connect cycle just to immediately shut it down.
if [[ "$sim_phy" != "ethernet" ]]; then
  connect_wifi 30
fi
#------------------------------------------------------------
#Dumping Current Device List
#------------------------------------------------------------
echo Disabling unused interface | tee -a ${LOG_FILE}
if [[ "$sim_phy" == "ethernet" ]]; then sudo ip link set dev $wladapter down; fi
if [[ "$sim_phy" == "wireless" ]]; then ea_down; fi
#------------------------------------------------------------
#Checking to see if the default gateway is reachable
#------------------------------------------------------------
_apt_done=0
gateway_reachable=false
detect_wlan_adapter
sudo rfkill unblock wifi; sudo rfkill unblock all
dfgw=$(ip route | grep -oP 'default via \K\S+')
if _wait_gateway "$dfgw"; then gw_ok=true; else gw_ok=false; fi
if [[ "$sim_phy" == "wireless" ]]; then
  # Wireless still requires the wlan adapter to be present; on failure
  # reconnect the WiFi as before.
  if [[ "$gw_ok" == true && -n "${wladapter}" ]]; then
    echo Successful network connection | tee -a ${LOG_FILE}
    gateway_reachable=true
    _run_apt_once
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
    gateway_reachable=true
    _run_apt_once
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
  if [[ "$ssidpw_fail" != "on" ]] && [[ -n ${wladapter} ]] && ! _is_wifi_connected; then
    # Only reconnect if we're NOT already associated. manage_connection up is
    # event-driven (iw event + nmcli exit) — the 180 is just the silent-AP
    # backstop, not a blind wait: it returns the instant activation completes or
    # the AP deauths.
    manage_connection up 180
  fi
fi
#------------------------------------------------------------
#End Setting up simulation load
#------------------------------------------------------------
echo Kill Switch is $kill_switch | tee -a ${LOG_FILE}
if [ "$kill_switch" != "on" ]; then
 # Snapshot the config's mtime so the loop can spot a pushed change mid-cycle.
 _cfg_mtime=$(stat -c %Y /usr/local/scripts/simulation.conf 2>/dev/null)
 # Pick up to 5 random iteration indices (1..100) to open a web page on, so the
 # fleet spreads web traffic across the loop instead of opening a single page at
 # the first iteration. The page just generates a request for stats — it does
 # nothing with the content and may hang; all instances are killed at loop end.
 declare -a _www_iters=()
 if [[ "$www_traffic" == "on" ]]; then
  for (( _wi=0; _wi<5; _wi++ )); do
   _www_iters+=("$(( 1 + RANDOM % 100 ))")
  done
 fi
 for z in {1..100}; do
  #------------------------------------------------------------
  # Rapid update — MUST fire every iteration REGARDLESS of which
  # simulation is running. It sits at the very top of the loop, before
  # the ssidpw_fail/auth_fail branch and before the connectivity gate,
  # so update.sh polls config/scripts frequently for EVERY client. It
  # used to live at the bottom of the non-auth-fail branch, where an
  # auth_fail/ssidpw_fail client (which takes the other branch) or any
  # connectivity `continue 2` skipped it entirely — so rapid_update
  # silently never ran for those clients. _update_due (defined above the
  # loop) rate-limits the actual source to once per 60s — the check still
  # runs every iteration, only the expensive update.sh work is gated.
  #------------------------------------------------------------
  if [[ "$rapid_update" == "on" ]] && _update_due; then source '/usr/local/scripts/update.sh'; fi
  #------------------------------------------------------------
  # Self-re-exec: if update.sh (or anything) just replaced simulation.sh on disk,
  # relaunch into the NEW code in place — same PID, no reboot, no sudo. A running
  # sourced loop otherwise keeps executing the OLD code no matter how many times
  # update.sh runs. Validate syntax first so a truncated/partial copy can't kill
  # the loop; on failure keep the current code and re-check next pass.
  #------------------------------------------------------------
  # Re-build the mtime fingerprint of simulation.sh + every sourced network
  # file. If it differs from the snapshot taken at startup, something we source
  # changed on disk — re-exec into the fresh code (same PID, no reboot). Syntax-
  # check EVERY file first so a truncated/half-written push can't crash the loop.
  _now_source_mtime=$(_source_mtime_fingerprint)
  if [[ -n "$_source_mtime_snapshot" && "$_now_source_mtime" != "$_source_mtime_snapshot" ]]; then
    _source_ok=1
    for _f in "$_self_path" "${_NET_SOURCE_FILES[@]}"; do
      if ! bash -n "$_f" 2>/dev/null; then
        _source_ok=0
        break
      fi
    done
    if [[ "$_source_ok" == 1 ]]; then
      echo "Sourced simulation files changed on disk — re-exec'ing into new code" | tee -a ${LOG_FILE}
      exec bash "$_self_path"
    else
      echo "Sourced files changed but one failed syntax check — staying on current code" | tee -a ${LOG_FILE}
      _source_mtime_snapshot="$_now_source_mtime"
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
  #SSID Incorrect Password / Auth Failure / MAC Auth Failure Simulation
  #since these are very similar they are in the same section: one
  #has a bad PSK, one has a blocked mac or invalid username/password combo
  #(auth_fail — a generic/no-particular-MAC flap), and mac_auth_fail is the
  #SPECIFIC-known-MAC case: it associates with a fixed, predictable spoofed
  #MAC (mac_auth_fail_mac) so the operator can pre-configure that EXACT MAC as
  #a RADIUS/ClearPass MAC-Auth deny entry and watch it get rejected. All three
  #need to be constantly connecting so we trigger insights.
  #------------------------------------------------------------
  if [[ ($ssidpw_fail == "on" || $auth_fail == "on" || $mac_auth_fail == "on") && -n ${wladapter} ]]; then
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
      # nmcli -w 5 + a blind sleep 5; `down` deactivates immediately. reset=force
      # a cycle each iter (a distinct AP event), cap 5s scan backstop, track=0 so
      # the expected deauths don't pollute the genuine-reconnect ramp/counter.
      manage_connection up 5 reset 5 0
      manage_connection down 5
     done
    fi
    if [[ "$mac_auth_fail" == "on" ]]; then
     echo "Running MAC Auth Failure (spoofed MAC deny-list test, target=${mac_auth_fail_mac})" | tee -a ${LOG_FILE}
     # Resolve the connection PROFILE name the same way connect_wifi does.
     mac_fail_ssid="$ssid"
     [[ "$site_based_ssid" == "on" ]] && mac_fail_ssid="$wsite-$ssid"
     # Bootstrap the profile ONCE, unspoofed, if it doesn't exist yet — `device
     # wifi connect` negotiates WPA2/WPA3/security automatically (a manual
     # `connection add` can't easily replicate that). This is the ONLY
     # `device wifi connect` call in this block: every iteration after this
     # uses `connection up` (via manage_connection), which HONORS
     # cloned-mac-address. `device wifi connect` does NOT — it silently resets
     # the wifi MAC back to permanent on EVERY call, so it must never drive the
     # spoofed connection or the spoof is undone the instant it's applied.
     if ! nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$mac_fail_ssid"; then
       echo "  [mac_auth_fail] bootstrapping connection profile '${mac_fail_ssid}' (one-time, unspoofed)" | tee -a ${LOG_FILE}
       nmcli -w 30 device wifi connect "$mac_fail_ssid" password "$ssidpw" >/dev/null 2>&1
     fi
     for i in {1..100}; do
      echo "Enable/Disable WLAN interface (spoofed MAC deny-list test)" | tee -a ${LOG_FILE}
      echo Iteration $i of 100 | tee -a ${LOG_FILE}
      # Pin the deny-listed MAC onto the connection PROFILE every iteration —
      # cheap/idempotent, and belt-and-suspenders against anything else having
      # reset it. manage_connection's "up" drives `nmcli connection up`, which
      # honors this; it must NEVER be replaced with device-wifi-connect (see
      # the gotcha above).
      _mac_mod_err=$(nmcli connection modify "$mac_fail_ssid" 802-11-wireless.cloned-mac-address "$mac_auth_fail_mac" 2>&1)
      _mac_mod_rc=$?
      manage_connection up 5 reset 5 0
      _mac_up_rc=$?
      manage_connection down 5
      _mac_actual=$(cat "/sys/class/net/${wladapter}/address" 2>/dev/null)
      # Log every resolved value — modify rc/stderr, up rc, and the ACTUAL
      # interface MAC read from sysfs — so a spoof that silently doesn't land
      # is diagnosable from the log alone, not a manual guest-exec session.
      echo "  [mac_auth_fail] modify_rc=${_mac_mod_rc} modify_err='${_mac_mod_err}' up_rc=${_mac_up_rc} target_mac=${mac_auth_fail_mac} actual_iface_mac=${_mac_actual}" | tee -a ${LOG_FILE}
     done
    fi
   #------------------------------------------------------------
   #Resetting the WIFI Password so it can connect correctly for updates/maintenance
   #------------------------------------------------------------
   if [[ "$mac_auth_fail" == "on" && -n "${mac_fail_ssid:-}" ]]; then
     # Clear the cloned-mac override so the maintenance connect_wifi below (and
     # any subsequent normal reconnect) is NOT left running on the spoofed
     # identity — an empty value unsets the property.
     _mac_clr_err=$(nmcli connection modify "$mac_fail_ssid" 802-11-wireless.cloned-mac-address "" 2>&1)
     _mac_clr_rc=$?
     echo "  [mac_auth_fail] cleared cloned-mac-address override on '${mac_fail_ssid}' before maintenance reconnect (rc=${_mac_clr_rc} err='${_mac_clr_err}')" | tee -a ${LOG_FILE}
   fi
   ssidpw=$(get_value $username 'ssidpw'); [[ -z "$ssidpw" ]] && ssidpw=$(get_value $simulation_id 'ssidpw')
   # Restore dot1x_password too — the fail loop corrupts it for 1X clients
   # (${real_dot1x}_fail), and without a restore the maintenance connect_wifi 5
   # below would authenticate with the bad password. Falls back to [simulation]
   # (dot1x_password lives there, not in [s0-s9]).
   dot1x_password=$(get_value $username 'dot1x_password'); [[ -z "$dot1x_password" ]] && dot1x_password=$(get_value 'simulation' 'dot1x_password')
   connect_wifi 5
   #------------------------------------------------------------
   #End SSID Incorrect Password Simualtion or Auth Failure Simulation
   #------------------------------------------------------------
  else
   #------------------------------------------------------------
   #If SSID Incorrect Password Sim is not triggered then check
   #for the other simualtions
   #------------------------------------------------------------
   _conn_ok=0
   if ! _wait_gateway "$dfgw"; then
     echo Attempting to reset adapter | tee -a ${LOG_FILE}
     _wait_wlan_adapter 15   # was: blind `sleep 15` — polls until the adapter appears
     delete_matching_connections
     # Event-driven success signal: connect_wifi returns 0 only when nmcli reaches
     # activated (association + DHCP + IP) — the same iw-event outcome we trust
     # everywhere else. Replaces the blind `ping -c2 $dfgw` re-check below. reset=
     # force a cycle here — this IS the explicit "reset the adapter" recovery site.
     connect_wifi 180 reset
     _conn_ok=$?
     echo WLAN Adapter name $wladapter | tee -a ${LOG_FILE}
    fi
    if [[ "$_conn_ok" -ne 0 ]]; then
    echo Connection failed muiltiple times | tee -a ${LOG_FILE}
    echo Resetting configuration | tee -a ${LOG_FILE}
    #------------------------------------------------------------
    #Cleaning up old network connection profiles
    #------------------------------------------------------------
    delete_matching_connections
    #------------------------------------------------------------
    #Looping Script - Network Connectivity Failed
    #------------------------------------------------------------
    gateway_reachable=false
    continue 2
   fi
   echo Successful network connection | tee -a ${LOG_FILE}
   gateway_reachable=true
   _run_apt_once
   #------------------------------------------------------------
   #End Connecting to Network
   #------------------------------------------------------------
   #Running WWW Traffic Simulation
   #------------------------------------------------------------
    if [[ "$www_traffic" == "on" ]] && [[ " ${_www_iters[*]} " == *" $z "* ]]; then
     echo Running WWW Traffic simulation | tee -a ${LOG_FILE}
     wwwfile=($(< /usr/local/scripts/websites.txt))
     if [[ ${#wwwfile[@]} -eq 0 ]]; then
      echo "No websites listed in /usr/local/scripts/websites.txt — skipping" | tee -a ${LOG_FILE}
     else
      rn_www=$((RANDOM % ${#wwwfile[@]}))
      url="${wwwfile[$rn_www]}"
     echo $(date) | tee -a ${LOG_FILE}
     echo ------------------------------| tee -a ${LOG_FILE}
     echo Phy: $sim_phy | tee -a ${LOG_FILE}
     echo Simulation Load: $sim_load | tee -a ${LOG_FILE}
     echo Website: $url | tee -a ${LOG_FILE}
     echo ------------------------------| tee -a ${LOG_FILE}
     # cpulimit is installed by apt_update.sh, which runs at the END of the outer
     # loop — so on a fresh box it may not be present yet on the first cycle. Fall
     # back to a plain launch so the stats request always fires; the throttle is a
     # nice-to-have, not required for the page to generate its request.
     if command -v cpulimit >/dev/null 2>&1; then
      cpulimit -l 10 -- firefox-esr --headless "$url" &
     else
      firefox-esr --headless "$url" >/dev/null 2>&1 &
     fi
     fi
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
   #Running DNS Latency simulation (slow responders → DNS-latency alert; the
   # split-out sibling of dns_fail, which now only hits unreachable servers)
   #------------------------------------------------------------
    if [[ "$dns_latency" == "on" ]]; then
     run_simulation "dns_latency.sh" 30
    fi
   #------------------------------------------------------------
   #End DNS Latency Simulation
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
   echo Sleeping for 5-10 seconds | tee -a ${LOG_FILE}
   echo Loop iteration $z of 100 | tee -a ${LOG_FILE}
   _rsleep 5
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
# Scoped to our headless instances (firefox-esr --headless) so we never touch an
# interactive firefox, and to the cpulimit wrappers that spawn them. Foreground
# so the kill is confirmed before we proceed to apt_update / offline sleep.
pkill -f 'firefox-esr.*--headless' || true
#------------------------------------------------------------
#End Kill switch Check
#------------------------------------------------------------
#------------------------------------------------------------
#Running apt update & apt upgrade — fallback: if the network never came up
# this cycle (so _run_apt_once never fired above), attempt it once here so the
# box still tries to patch each cycle. No-ops if it already ran.
#------------------------------------------------------------
_run_apt_once
#------------------------------------------------------------
#DISABLED 2026-07-21 — allow_offline offline-window block commented out intact
#to isolate it as the cause of clients disconnecting from the network too often.
#This was the only place the loop deliberately takes BOTH adapters link-down
#(up to a 4h _isleep) after the sim cycle. Restore by removing the '# ' prefixes
#on the lines below (the helper, the if/fi, and every body line) when confirmed.
#
# QUEUED FEATURE (also commented out, activate with the block above): when the
# hub engine is actively controlling a SPECIFIC simulation for this client, the
# client is NOT allowed to go offline — a link-down blackout of up to 4h would
# silence the sim and stop the insight/alert it exists to fire. "Engine
# controlling a specific sim" = a [username] override has turned ON one of the
# engine-owned failure sims (dhcp_fail/dns_fail/assoc_fail/port_flap/
# ssidpw_fail/auth_fail/mac_auth_fail). Ambient traffic (ping/download/iperf/
# www) is a local roll, NOT per-client engine control, so it does NOT block
# offline.
# Connectivity pinning (wsite/ssid/ssidpw) is intentionally excluded too — every
# hub-mode client has it, so including it would disable the offline feature
# entirely. Activate by uncommenting _engine_driving_sim + the && ! gate below.
#------------------------------------------------------------
#_engine_driving_sim() {
#  [[ "${dhcp_fail:-off}" == "on" || "${dns_fail:-off}" == "on" || \
#     "${assoc_fail:-off}" == "on" || "${port_flap:-off}" == "on" || \
#     "${ssidpw_fail:-off}" == "on" || "${auth_fail:-off}" == "on" || \
#     "${mac_auth_fail:-off}" == "on" ]]
#}
#------------------------------------------------------------
#if [[ "$allow_offline" == "yes" ]] && ! _engine_driving_sim; then
#  #------------------------------------------------------------
#  #Bringing all interfaces down to make it look like the device is offline.
#  #Otherwise they get triggered as IOT since they are always connected.
#  #------------------------------------------------------------
#  echo Bringing all interfaces down | tee -a ${LOG_FILE}
#  if [[ -n ${wladapter} ]]; then sudo ip link set dev $wladapter down; fi
#  # mgmt guard (#3): never take the eth adapter down if it's carrying a
#  # 169.253.* mgmt IP — that would strand the box for the whole offline
#  # window. Matches the ea_down() guard used at the pre-sim disable step.
#  if [[ -n ${eadapter} ]] && ! ea_is_mgmt; then sudo ip link set dev $eadapter down; fi
#  echo Sleeping for $rn_offline_time seconds \(interruptible by update signal\)
#  echo ------------------------------| tee -a ${LOG_FILE}
#  #------------------------------------------------------------
#  #Sleep for up to 4 hours to show the device left. Interruptible: a USR1 from
#  #agent.sh (restart_sim / kill_switch) kills this sleep early so a pushed
#  #config change / repurpose lands immediately instead of after the offline
#  #window — the interfaces come back up below and the outer loop re-reads config.
#  #------------------------------------------------------------
#  _isleep "$rn_offline_time"
#  #------------------------------------------------------------
#  #Bringing all interfaces back up to call home/update scripts
#  #------------------------------------------------------------
#  echo Bringing all interfaces online | tee -a ${LOG_FILE}
#  if [[ -n ${eadapter} ]]; then sudo ip link set dev $eadapter up; fi
#  if [[ -n ${wladapter} ]]; then sudo ip link set dev $wladapter up; fi
#  echo ------------------------------| tee -a ${LOG_FILE}
#fi
#------------------------------------------------------------
#Looping Script
#------------------------------------------------------------
done