#!/bin/bash
version=.10
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

# Learning mode (Phase 2) — same as dns_fail: probe the rate UP on clean bursts,
# settle 20% below the DOS ceiling, report it up. OFF => production down-only
# self-throttle. Pushed via config (dns_learn=on); DNS_LEARN=1 forces per run.
# Shares the one DNS ceiling with dns_fail (same client dig capacity).
_dns_learn=$(get_value 'simulation' 'dns_learn')
if [[ "${DNS_LEARN:-}" == "1" || "$_dns_learn" == "on" ]]; then _learn=1; else _learn=0; fi

# Per-run rate override for manual ceiling / recovery testing (parallels
# DNS_MAX_INFLIGHT): DNS_LATENCY_RATE=<n> forces the rate AND bypasses the
# persisted self-throttle. Otherwise apply the persisted ceiling (dns_ceiling_rate).
if [[ "${DNS_LATENCY_RATE:-}" =~ ^[0-9]+$ ]]; then
  _configured_rate=$DNS_LATENCY_RATE; rate_per_minute=$DNS_LATENCY_RATE
else
  _configured_rate=$rate_per_minute
  rate_per_minute=$(dns_ceiling_rate "$rate_per_minute" "$_learn")
fi

# Seconds to wait between each launch to hit the target rate.
pause_between=$(awk "BEGIN { printf \"%.3f\", 60 / $rate_per_minute }")

# CPU guard: cap the number of background dig processes in flight at once. Knob
# for finding a client's ceiling by trial and error: precedence is the
# DNS_MAX_INFLIGHT env var (instant per-run: `DNS_MAX_INFLIGHT=40 bash
# dns_latency.sh`) > [simulation] dns_max_inflight (fleet-wide via config, no
# script redeploy) > default 100. Floored at 1. Shared knob with dns_fail — it's
# the client's dig capacity, and both are exclusive sims (one runs at a time).
_MAX_INFLIGHT="${DNS_MAX_INFLIGHT:-}"
[[ "$_MAX_INFLIGHT" =~ ^[0-9]+$ ]] || _MAX_INFLIGHT=$(get_value 'simulation' 'dns_max_inflight')
[[ "$_MAX_INFLIGHT" =~ ^[0-9]+$ ]] || _MAX_INFLIGHT=100
(( _MAX_INFLIGHT < 1 )) && _MAX_INFLIGHT=1

_throttle_note=""
(( rate_per_minute < _configured_rate )) && _throttle_note=" (self-throttled from ${_configured_rate}/min after a prior gateway DOS)"
(( _learn )) && { dns_ceiling_converged && _throttle_note="${_throttle_note} [LEARNING: ceiling found]" || _throttle_note="${_throttle_note} [LEARNING: hunting ceiling]"; }
echo "$(date) Firing DNS latency lookups at ${rate_per_minute}/min for ${burst_seconds}s (max ${_MAX_INFLIGHT} digs in flight)${_throttle_note}" | tee -a "$debug"
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
_burst_start=$SECONDS
# Gateway circuit-breaker (shared logic with dns_fail): if the gateway goes
# OFFLINE we've DOSed it → bail + drop the OPERATING rate 20% for next burst.
# START GATE: skip the burst if the gateway is already offline at start (adapter
# recovering). Offline = 5/5 pings fail (dns_gw_confirmed_down).
_dfgw=$(dns_default_gw)
if (( ! VERBOSE )) && [[ -n "$_dfgw" ]] && dns_gw_confirmed_down "$_dfgw"; then
  echo "$(date) default gateway $_dfgw offline at burst start (recovering?) — skipping this burst, no throttle change" | tee -a "$debug"
  exit 0
fi
_gw_next_check=$((SECONDS + 2))
_bailed=0
while (( SECONDS < stop_at )); do
  for record in $dnsfile; do
    for server in "${latencies[@]}"; do

      # Stop the moment the burst window closes (break out of both loops).
      (( SECONDS >= stop_at )) && break 2

      # Gateway check (~every 2s): single ping, then CONFIRM with 5 pings — OFFLINE
      # only if ALL 5 fail. Offline → bail + drop the OPERATING rate 20%.
      if (( ! VERBOSE )) && [[ -n "$_dfgw" ]] && (( SECONDS >= _gw_next_check )); then
        _gw_next_check=$((SECONDS + 2))
        if ! dns_gw_alive "$_dfgw" && dns_gw_confirmed_down "$_dfgw"; then
          _newrate=$(dns_ceiling_penalize "$rate_per_minute")
          (( _learn )) && dns_ceiling_mark_converged
          echo "$(date) default gateway $_dfgw OFFLINE (5/5 pings failed) after ${fired} digs at ${rate_per_minute}/min — BAILING; $( (( _learn )) && echo "ceiling FOUND, settling at" || echo "throttling to") ${_newrate}/min next burst" | tee -a "$debug"
          kill $(jobs -p) 2>/dev/null
          _bailed=1
          break 2
        fi
      fi

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
        # Print the launch so the fire-and-forget activity is visible in the
        # terminal / sim.log, then launch in the background. $SECONDS (seconds into
        # the burst) is a fork-free marker to gauge the rate.
        printf '[+%ss] dig @%s %s\n' "$SECONDS" "$server" "$record"
        dig +time=1 +tries=1 +short @"$server" "$record" >/dev/null 2>&1 &
      fi

      fired=$((fired + 1))
      sleep "$pause_between"
    done
  done
done

# Let any lookups still in flight finish, then record how many we fired.
(( VERBOSE )) || wait 2>/dev/null
if (( _bailed )); then
  echo "$(date) DNS latency lookups fired: ${fired} (BAILED on gateway loss — self-throttling next burst)" | tee -a "$debug"
elif (( _learn && ! VERBOSE )) && ! dns_ceiling_converged; then
  _probe=$(dns_ceiling_relax "$rate_per_minute")
  echo "$(date) DNS latency lookups fired: ${fired} (LEARNING: ${rate_per_minute}/min sustained — probing up to ${_probe}/min next burst)" | tee -a "$debug"
else
  echo "$(date) DNS latency lookups fired: ${fired}" | tee -a "$debug"
fi
