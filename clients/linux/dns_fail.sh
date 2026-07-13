#!/bin/bash
version=.03
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-update.log"
echo DNS Failure Script Version $version | tee "$debug"
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'
dnsfile=$(< /usr/local/scripts/dns_fail.txt)
dns_latency_1=$(get_value 'address' 'dns_latency_1')
dns_latency_2=$(get_value 'address' 'dns_latency_2')
dns_latency_3=$(get_value 'address' 'dns_latency_3')
dns_bad_ip_1=$(get_value 'address' 'dns_bad_ip_1')
dns_bad_ip_2=$(get_value 'address' 'dns_bad_ip_2')
dns_bad_ip_3=$(get_value 'address' 'dns_bad_ip_3')
dns_bad_record_1=$(get_value 'address' 'dns_bad_record_1')
dns_bad_record_2=$(get_value 'address' 'dns_bad_record_2')
dns_bad_record_3=$(get_value 'address' 'dns_bad_record_3')
#------------------------------------------------------------
# Per-user overrides — mirror simulation.sh's apply_override so a
# user-overrides.conf [username] override of a DNS server (dns_bad_ip /
# dns_bad_record / dns_latency) actually reaches the dig loop. Without this,
# dns_fail.sh used ONLY the global [address] values and silently ignored any
# per-user override that simulation.sh honors. Username = hostname prefix
# before the first '-' (same derivation as simulation.sh).
#------------------------------------------------------------
username=$(echo "$HOSTNAME" | cut -d "-" -f 1)
apply_override() {
  local var=$1
  local val
  val=$(get_value "$username" "$var")
  [[ -n ${val} ]] && declare -g "$var=$val"
}
for key in dns_latency_1 dns_latency_2 dns_latency_3 \
           dns_bad_ip_1 dns_bad_ip_2 dns_bad_ip_3 \
           dns_bad_record_1 dns_bad_record_2 dns_bad_record_3; do
  apply_override "$key"
done
bad_records=($dns_bad_record_1 $dns_bad_record_2 $dns_bad_record_3)
bad_ips=($dns_bad_ip_1 $dns_bad_ip_2 $dns_bad_ip_3)
latencies=($dns_latency_1 $dns_latency_2 $dns_latency_3)
for i in {1..10}; do
  for r in $dnsfile; do
   echo $(date) | tee -a "$debug"
   echo Running DNS Failure: | tee -a "$debug"
   echo $r | tee -a "$debug"
   for server in "${bad_records[@]}" "${bad_ips[@]}" "${latencies[@]}"; do
     dig @$server $r
   done
   sleep 5
  done
done
