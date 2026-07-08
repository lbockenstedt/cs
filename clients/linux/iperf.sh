#!/bin/bash
version=.04
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-iperf.log"
echo iPerf Script Version $version | tee "$debug"
#------------------------------------------------------------
# WHY: This script is launched via 'nohup bash iperf.sh' from simulation.sh.
# Bash subprocesses do NOT inherit non-exported variables, so $iperf_server,
# $rn_iperf_port, and $rn_iperf_time from simulation.sh are always unset here.
# Fix: source ini-parser to read iperf_server/iperf_bw from config, generate
# own random values.
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'
iperf_server=$(get_value 'address' 'iperf_server')
iperf_bw=$(get_value 'simulation' 'iperf_bw')
rn_iperf_port=$((5201 + RANDOM % 10))
rn_iperf_time=$((1 + RANDOM % 300))
if [[ -z "$iperf_server" ]]; then
  echo "No iperf_server configured — skipping iperf" | tee -a "$debug"
  exit 0
fi
echo "iPerf target: $iperf_server  bw: ${iperf_bw:-1k}  time: $rn_iperf_time" | tee -a "$debug"
ports=($rn_iperf_port 443 3260 2049 1194 3389 445 80 1433)
for port in "${ports[@]}"; do
  iperf3 -c "$iperf_server" -p "$port" -b "${iperf_bw:-1k}" -t "$rn_iperf_time" | tee -a "$debug"
done
