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
rn=$((1 + RANDOM % 60))
rn_ping_size=$((1 + RANDOM % 1400))
echo "Ping target: $ping_address  count: $rn  size: $rn_ping_size" | tee -a "$debug"
ping -c "$rn" "$ping_address" -s "$rn_ping_size" | tee -a "$debug"