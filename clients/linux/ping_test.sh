#!/bin/bash
version=.04
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-ping-test.log"
echo Ping_Test Script Version $version | tee "$debug"
#------------------------------------------------------------
# WHY: This script is launched via 'nohup bash ping_test.sh' from simulation.sh.
# Bash subprocesses do NOT inherit non-exported variables, so $rn, $ping_address,
# and $rn_ping_size from simulation.sh are always unset here.
# Fix: source ini-parser to read ping_address from config, generate own random values.
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'
ping_address=$(get_value 'address' 'ping_address')
# Per-user override — mirror simulation.sh's apply_override so a
# user-overrides.conf [username] override of ping_address reaches the ping.
# Without this ping_test.sh used ONLY the global [address] value.
username=$(echo "$HOSTNAME" | cut -d "-" -f 1)
apply_override() { local v; v=$(get_value "$username" "$1"); [[ -n "$v" ]] && declare -g "$1=$v"; }
apply_override ping_address
rn=$((1 + RANDOM % 60))
rn_ping_size=$((1 + RANDOM % 1400))
echo "Ping target: $ping_address  count: $rn  size: $rn_ping_size" | tee -a "$debug"
ping -c "$rn" "$ping_address" -s "$rn_ping_size" | tee -a "$debug"