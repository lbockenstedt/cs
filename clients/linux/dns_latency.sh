#!/bin/bash
version=.02
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-dns-latency.log"
echo DNS Latency Script Version $version | tee "$debug"
#------------------------------------------------------------
# DNS LATENCY simulation — the sibling of dns_fail.sh. dns_fail queries
# UNREACHABLE/bogus servers (lookups fail → DNS-failure alert); this queries the
# SLOW responders (dns_latency_*) so lookups are delayed → DNS-latency alert.
# Split out of dns_fail.sh so each condition drives its own Central alert instead
# of being lumped together. Same fire-and-forget, rate-based shape as dns_fail.
#
# Verbose / manual mode: pass --verbose (or export VERBOSE=1) to watch each
# lookup on stdout (foreground). Normal sim runs fire-and-forget in the
# background with suppressed output.
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
#------------------------------------------------------------
# Per-user overrides — mirror simulation.sh's apply_override so a
# user-overrides.conf [username] override of a dns_latency server actually
# reaches the dig loop. Username = hostname prefix before the first '-' (same
# derivation as simulation.sh).
#------------------------------------------------------------
derive_username
apply_override() {
  local var=$1
  local val
  val=$(get_value "$username" "$var")
  [[ -n ${val} ]] && declare -g "$var=$val"
}
for key in dns_latency_1 dns_latency_2 dns_latency_3; do
  apply_override "$key"
done
latencies=($dns_latency_1 $dns_latency_2 $dns_latency_3)
#------------------------------------------------------------
# Fire-and-forget slow DNS lookups
#
# Central's DNS-latency alarm is rate-based like the failure alarm: it needs a
# sustained stream of slow lookups before it trips. The dns_latency_* servers
# respond slowly on purpose, so a normal 'dig' would sit and WAIT and we'd only
# manage a handful per minute. Instead we launch every 'dig' in the background
# with '&' and never wait for the answer — the slowness is the point. Each dig
# gets a 1-second timeout so background lookups clear instead of piling up, and
# we pause a fraction of a second between launches to set the rate.
#------------------------------------------------------------

# How fast to fire (lookups per minute) and how long to keep firing (seconds).
# Read from simulation.conf [simulation]; safe defaults if unset. Rate is never
# allowed below the ~200/min the alarm needs.
rate_per_minute=$(get_value 'simulation' 'dns_latency_rate')
burst_seconds=$(get_value 'simulation' 'dns_latency_duration')
[[ -z "$rate_per_minute" ]] && rate_per_minute=600
[[ -z "$burst_seconds"   ]] && burst_seconds=60
(( rate_per_minute < 200 )) && rate_per_minute=200

# Seconds to wait between each launch to hit the target rate.
pause_between=$(awk "BEGIN { printf \"%.3f\", 60 / $rate_per_minute }")

# CPU guard: cap the number of background dig processes in flight at once. The
# rate/pause alone doesn't bound this — a slow resolver keeps each dig alive up
# to its +time timeout, so a fast rate stacks hundreds of concurrent digs and
# pegs the CPU. ~100 keeps the burst intense without overrunning the box.
_MAX_INFLIGHT=100

echo "$(date) Firing DNS latency lookups at ${rate_per_minute}/min for ${burst_seconds}s" | tee -a "$debug"
if (( VERBOSE )); then
  echo "[verbose] latencies   : ${latencies[*]:-<none>}"
  echo "[verbose] records file: $(wc -l < /usr/local/scripts/dns_fail.txt 2>/dev/null) lines"
  echo "[verbose] pause between lookups: ${pause_between}s   (foreground — slower than sim mode)"
  echo "----------------------------------------"
fi

# Keep firing until the burst window is up, cycling through every record against
# every slow server.
stop_at=$((SECONDS + burst_seconds))
fired=0
while (( SECONDS < stop_at )); do
  for record in $dnsfile; do
    for server in "${latencies[@]}"; do

      # Stop the moment the burst window closes (break out of both loops).
      (( SECONDS >= stop_at )) && break 2

      if (( VERBOSE )); then
        _out=$(dig +time=1 +tries=1 +short @"$server" "$record" 2>&1); _rc=$?
        printf '%s [dig @%s %s] rc=%s -> %s\n' "$(date '+%H:%M:%S')" \
               "$server" "$record" "$_rc" "${_out:-<empty>}"
      else
        # Throttle to at most _MAX_INFLIGHT concurrent digs (CPU guard). `wait -n`
        # (bash 4.3+) blocks until one background dig finishes; the sleep fallback
        # covers older bash. jobs -rp counts only the running background digs.
        while (( $(jobs -rp 2>/dev/null | wc -l) >= _MAX_INFLIGHT )); do
          wait -n 2>/dev/null || sleep "$pause_between"
        done
        dig +time=1 +tries=1 +short @"$server" "$record" >/dev/null 2>&1 &
      fi

      fired=$((fired + 1))
      sleep "$pause_between"
    done
  done
done

# Let any lookups still in flight finish, then record how many we fired.
(( VERBOSE )) || wait 2>/dev/null
echo "$(date) DNS latency lookups fired: ${fired}" | tee -a "$debug"
