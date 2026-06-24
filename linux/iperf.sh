#!/bin/bash
version=.03
#------------------------------------------------------------
ports=($rn_iperf_port 443 3260 2049 1194 3389 445 80 1433)
for port in "${ports[@]}"; do
  iperf3 -c $iperf_server -p $port -b 1k -t $rn_iperf_time
done
