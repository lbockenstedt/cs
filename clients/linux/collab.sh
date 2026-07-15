#!/bin/bash
version=.01
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-collab.log"
echo Collab Script Version $version | tee "$debug"
#------------------------------------------------------------
# WHY: Launched via 'nohup bash collab.sh' from simulation.sh (run_simulation).
# Bash subprocesses do NOT inherit non-exported variables, so $collab_server,
# $collab_app, $collab_bw, $collab_time from simulation.sh are unset here.
# Fix: source ini-parser to read them from config + apply per-user overrides,
# then exec the python sender (mirror of iperf.sh's sourcing pattern).
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'
collab_server=$(get_value 'address' 'collab_server')
collab_app=$(get_value 'simulation' 'collab_app')
collab_bw=$(get_value 'simulation' 'collab_bw')
collab_time=$(get_value 'simulation' 'collab_time')
# Per-user overrides — mirror iperf.sh so a user-overrides.conf [username]
# override of collab_* reaches this run.
username=$(echo "$HOSTNAME" | cut -d "-" -f 1)
apply_override() { local v; v=$(get_value "$username" "$1"); [[ -n "$v" ]] && declare -g "$1=$v"; }
apply_override collab_server
apply_override collab_app
apply_override collab_bw
apply_override collab_time

if [[ -z "$collab_server" ]]; then
  echo "No collab_server configured — skipping collab" | tee -a "$debug"
  exit 0
fi

# collab_time=0 (or empty) -> run until the loop relaunches/kills it; otherwise
# run for that many seconds per launch (mirror iperf.sh's random window).
rn_time="${collab_time:-$((1 + RANDOM % 300))}"

echo "Collab target: $collab_server  app: ${collab_app:-teams}  bw: ${collab_bw:-default}  time: $rn_time" | tee -a "$debug"
exec python3 /usr/local/scripts/collab.py \
  --server "$collab_server" \
  --app "${collab_app:-teams}" \
  --bw "${collab_bw}" \
  --time "$rn_time"