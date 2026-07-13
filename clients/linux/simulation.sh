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
server_url=$(get_value 'server' 'server_url')
server_url="${server_url:-http://169.253.1.1:8080}"
#------------------------------------------------------------
#Device Specific Simulation settings
#------------------------------------------------------------
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
  dns_bad_record_3 iperf_server)
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
  for sim in dns_fail iperf download www_traffic ping_test ssidpw_fail auth_fail dhcp_fail; do
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
    '{"hostname":"%s","simulation_id":"%s","platform":"%s","iteration":%s,"connected_ssid":"%s","gateway_reachable":%s,"active_simulations":[%s],"errors":%s,"config":{"kill_switch":"%s","dns_fail":"%s","iperf":"%s","www_traffic":"%s","download":"%s","ping_test":"%s","ssidpw_fail":"%s","auth_fail":"%s","dhcp_fail":"%s"}}' \
    "$(json_escape "$hostname")" "$(json_escape "${simulation_id:-}")" "$(json_escape "$platform")" \
    "$iteration" "$(json_escape "${connected_ssid:-}")" "$gateway_json" "$active_simulations" "$errors_json" \
    "$(json_escape "${kill_switch:-off}")" "$(json_escape "${dns_fail:-off}")" "$(json_escape "${iperf:-off}")" \
    "$(json_escape "${www_traffic:-off}")" "$(json_escape "${download:-off}")" "$(json_escape "${ping_test:-off}")" \
    "$(json_escape "${ssidpw_fail:-off}")" "$(json_escape "${auth_fail:-off}")" "$(json_escape "${dhcp_fail:-off}")"
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
connect_wifi() {
  nmcli radio wifi off
  nmcli radio wifi on
  sleep 15
  if [[ "$site_based_ssid" == "on" ]]; then
    nmcli -w $1 device wifi connect $wsite"-"$ssid password $ssidpw
  else
    nmcli -w $1 device wifi connect $ssid password $ssidpw
  fi
}
#------------------------------------------------------------
#Connection management
#------------------------------------------------------------
manage_connection() {
  local action=$1
  local wait_time=$2
  nmcli radio wifi off
  nmcli radio wifi on
  sleep 15
  if [[ "$site_based_ssid" == "on" ]]; then
    nmcli -w $wait_time connection $action $wsite"-"$ssid
  else
    nmcli -w $wait_time connection $action $ssid
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
#Attempting WiFi connection
#------------------------------------------------------------
connect_wifi 30
#------------------------------------------------------------
#Dumping Current Device List
#------------------------------------------------------------
echo Disabling unused interface | tee -a ${LOG_FILE}
if [[ "$sim_phy" == "ethernet" ]]; then sudo ip link set dev $wladapter down; fi
if [[ "$sim_phy" == "wireless" ]]; then ea_down; fi
mac_id=$(python3 -c "import zlib; h=zlib.crc32('${username}'.encode())&0xFFFFFF; print(f'bc:07:1d:{h>>16:02x}:{(h>>8)&0xff:02x}:{h&0xff:02x}')")
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
    sleep 15
    wladapter=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
    connect_wifi 180
    sleep 15
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
    manage_connection up 180
  fi
  sleep 5
fi
#------------------------------------------------------------
#End Setting up simulation load
#------------------------------------------------------------
echo Kill Switch is $kill_switch | tee -a ${LOG_FILE}
if [ "$kill_switch" != "on" ]; then
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
  #SSID Incorrect Password Simulation or Auth Failure Simulation
  #since these are very similar they are in the same section one
  #has a bad PSK and others have a blocked mac or invalud username/password combo
  #both need to be constantly connecting so we trigger insights
  #------------------------------------------------------------
  if [[ ($ssidpw_fail == "on" || $auth_fail == "on") && -n ${wladapter} ]]; then
    if [[ "$ssidpw_fail" == "on" ]]; then
     for i in {1..100}; do
      echo Running SSID Incorrect Password | tee -a ${LOG_FILE}
      ssidpw="$(get_value $simulation_id 'ssidpw')""_fail"
      echo Iteration $i of 100 | tee -a ${LOG_FILE}
      delete_matching_connections
      connect_wifi 5
     done
    fi
    if [[ "$auth_fail" == "on" ]]; then
     echo Running Auth Failure | tee -a ${LOG_FILE}
     for i in {1..100}; do
      echo Enable/Disable WLAN interface | tee -a ${LOG_FILE}
      echo Iteration $i of 100 | tee -a ${LOG_FILE}
      delete_matching_connections
      manage_connection up 5
      sleep 5
      manage_connection down 5
     done
    fi
   #------------------------------------------------------------
   #Resetting the WIFI Password so it can connect correctly for updates/maintenance
   #------------------------------------------------------------
   ssidpw=$(get_value $simulation_id 'ssidpw')
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
     sleep 15
     wladapter=$(ip -br a | grep "wlx\|wlan" | cut -d ' ' -f '1')
     delete_matching_connections
     connect_wifi 180
     echo WLAN Adapter name $wladapter | tee -a ${LOG_FILE}
     sleep 15
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