#!/bin/bash
version=.05
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-dns-fail.log"
echo DNS Failure Script Version $version | tee "$debug"
#------------------------------------------------------------
# Verbose / manual mode: pass --verbose (or export VERBOSE=1) to watch each
# lookup on stdout. Normal sim runs use fire-and-forget background digs with
# suppressed output (Central's DNS-fail alarm is rate-based — see below), which
# makes a manual `bash dns_fail.sh` silent. Verbose runs each dig in the
# FOREGROUND and prints server/record/exit/result so you can see what's
# happening. The burst window + rate still apply.
#------------------------------------------------------------
VERBOSE=0
[[ "${VERBOSE:-0}" == "1" ]] && VERBOSE=1
for _a in "$@"; do
  case "$_a" in --verbose|-v) VERBOSE=1 ;; esac
done
#------------------------------------------------------------
source '/usr/local/scripts/ini-parser.sh'
source '/usr/local/scripts/common.sh'
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
derive_username
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
#------------------------------------------------------------
# Fire-and-forget DNS failures
#
# Central's DNS-failure alarm is rate-based: it needs roughly 200 failed
# lookups per minute before it trips. The servers above are unreachable or slow
# on purpose (the bad_ip ones are RFC1918 blackholes), so a normal 'dig' would
# sit and WAIT on its timeout and we'd only manage a handful per minute -- the
# alarm would never fire.
#
# Instead we launch every 'dig' in the background with '&' and never wait for
# the answer. The failure is the point, not the reply. We give each 'dig' a
# 1-second timeout so the background lookups clear quickly instead of piling
# up, and we pause a fraction of a second between launches to set the rate.
#------------------------------------------------------------

# How fast to fire (lookups per minute) and how long to keep firing (seconds).
# Both are read from simulation.conf [simulation]; if unset we use safe
# defaults. The rate is never allowed below the ~200/min the alarm needs.
rate_per_minute=$(get_value 'simulation' 'dns_fail_rate')
burst_seconds=$(get_value 'simulation' 'dns_fail_duration')
[[ -z "$rate_per_minute" ]] && rate_per_minute=600
[[ -z "$burst_seconds"   ]] && burst_seconds=60
(( rate_per_minute < 200 )) && rate_per_minute=200

# Seconds to wait between each launch to hit the target rate.
# Example: 600 per minute -> 60/600 -> 0.1 second between lookups.
pause_between=$(awk "BEGIN { printf \"%.3f\", 60 / $rate_per_minute }")

echo "$(date) Firing DNS failures at ${rate_per_minute}/min for ${burst_seconds}s" | tee -a "$debug"
if (( VERBOSE )); then
  echo "[verbose] bad_records : ${bad_records[*]:-<none>}"
  echo "[verbose] bad_ips     : ${bad_ips[*]:-<none>}"
  echo "[verbose] latencies   : ${latencies[*]:-<none>}"
  echo "[verbose] records file: $(wc -l < /usr/local/scripts/dns_fail.txt 2>/dev/null) lines"
  echo "[verbose] pause between lookups: ${pause_between}s   (foreground — slower than sim mode)"
  echo "----------------------------------------"
fi

# Keep firing until the burst window is up, cycling through every record
# against every bad/slow server.
stop_at=$((SECONDS + burst_seconds))
fired=0
while (( SECONDS < stop_at )); do
  for record in $dnsfile; do
    for server in "${bad_records[@]}" "${bad_ips[@]}" "${latencies[@]}"; do

      # Stop the moment the burst window closes (break out of both loops).
      (( SECONDS >= stop_at )) && break 2

      if (( VERBOSE )); then
        # Foreground + visible: see each lookup's result (for manual debugging).
        _out=$(dig +time=1 +tries=1 +short @"$server" "$record" 2>&1); _rc=$?
        printf '%s [dig @%s %s] rc=%s -> %s\n' "$(date '+%H:%M:%S')" \
               "$server" "$record" "$_rc" "${_out:-<empty>}"
      else
        # Launch the lookup in the background and move straight on.
        dig +time=1 +tries=1 +short @"$server" "$record" >/dev/null 2>&1 &
      fi

      fired=$((fired + 1))
      sleep "$pause_between"
    done
  done
done

# Let any lookups still in flight finish, then record how many we fired.
# (Verbose mode runs foreground — no background jobs to wait on.)
(( VERBOSE )) || wait 2>/dev/null
echo "$(date) DNS failures fired: ${fired}" | tee -a "$debug"
