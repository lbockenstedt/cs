#!/bin/bash
version=.10
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-dns-fail.log"
echo DNS Failure Script Version $version | tee "$debug"
#------------------------------------------------------------
# Normal (default) mode: fire-and-forget background digs, but PRINT each launch
# to stdout so you can watch the sim run in a terminal (`bash dns_fail.sh`) or in
# sim.log. We never wait for the answer — the failure IS the sim. The timestamp
# is a fork-free `$SECONDS` marker, so the visibility costs no extra process per
# dig.
# Verbose / manual mode: pass --verbose (or export VERBOSE=1) to run each dig in
# the FOREGROUND and print server/record/exit/RESULT (slower — one at a time —
# for detailed debugging). The burst window + rate still apply in both modes.
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
# dns_fail queries UNREACHABLE / bogus servers only (dns_bad_record_* +
# dns_bad_ip_*) so the lookups FAIL. The slow-responder set (dns_latency_*) is a
# DIFFERENT condition (high DNS latency, not failure) and now lives in its own
# dns_latency.sh so each drives its own Central alert.
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
for key in dns_bad_ip_1 dns_bad_ip_2 dns_bad_ip_3 \
           dns_bad_record_1 dns_bad_record_2 dns_bad_record_3; do
  apply_override "$key"
done
bad_records=($dns_bad_record_1 $dns_bad_record_2 $dns_bad_record_3)
bad_ips=($dns_bad_ip_1 $dns_bad_ip_2 $dns_bad_ip_3)
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

# Per-run rate override for manual ceiling / recovery testing (parallels
# DNS_MAX_INFLIGHT): DNS_FAIL_RATE=<n> forces the rate AND bypasses the persisted
# self-throttle, so you can deliberately drive a client to its DOS ceiling and
# watch it bail + recover (e.g. DNS_FAIL_RATE=5000 DNS_MAX_INFLIGHT=1500 bash
# dns_fail.sh). Otherwise apply the persisted self-throttle ceiling: a prior
# burst that DOSed its OWN gateway ratcheted this rate down (dns_ceiling_penalize
# in common.sh); start there so we converge instead of re-DOSing every cycle.
if [[ "${DNS_FAIL_RATE:-}" =~ ^[0-9]+$ ]]; then
  _configured_rate=$DNS_FAIL_RATE; rate_per_minute=$DNS_FAIL_RATE
else
  _configured_rate=$rate_per_minute
  rate_per_minute=$(dns_ceiling_rate "$rate_per_minute")
fi

# Seconds to wait between each launch to hit the target rate.
# Example: 600 per minute -> 60/600 -> 0.1 second between lookups.
pause_between=$(awk "BEGIN { printf \"%.3f\", 60 / $rate_per_minute }")

# CPU guard: cap the number of background dig processes in flight at once. The
# rate/pause alone doesn't bound this — a slow or unreachable resolver keeps each
# dig alive up to its +time timeout, so a fast rate stacks concurrent digs and
# pegs the CPU. This is the knob for finding a client's ceiling by trial and
# error: precedence is the DNS_MAX_INFLIGHT env var (instant per-run sweep:
# `DNS_MAX_INFLIGHT=40 bash dns_fail.sh`) > [simulation] dns_max_inflight (fleet-
# wide, pushed via config, no script redeploy) > default 100. Floored at 1.
_MAX_INFLIGHT="${DNS_MAX_INFLIGHT:-}"
[[ "$_MAX_INFLIGHT" =~ ^[0-9]+$ ]] || _MAX_INFLIGHT=$(get_value 'simulation' 'dns_max_inflight')
[[ "$_MAX_INFLIGHT" =~ ^[0-9]+$ ]] || _MAX_INFLIGHT=100
(( _MAX_INFLIGHT < 1 )) && _MAX_INFLIGHT=1

_throttle_note=""
(( rate_per_minute < _configured_rate )) && _throttle_note=" (self-throttled from ${_configured_rate}/min after a prior gateway DOS)"
echo "$(date) Firing DNS failures at ${rate_per_minute}/min for ${burst_seconds}s (max ${_MAX_INFLIGHT} digs in flight)${_throttle_note}" | tee -a "$debug"
if (( VERBOSE )); then
  echo "[verbose] bad_records : ${bad_records[*]:-<none>}"
  echo "[verbose] bad_ips     : ${bad_ips[*]:-<none>}"
  echo "[verbose] records file: $(wc -l < /usr/local/scripts/dns_fail.txt 2>/dev/null) lines"
  echo "[verbose] pause between lookups: ${pause_between}s   (foreground — slower than sim mode)"
  echo "----------------------------------------"
fi

# Keep firing until the burst window is up, cycling through every record
# against every bad/slow server.
stop_at=$((SECONDS + burst_seconds))
fired=0
_burst_start=$SECONDS
# Gateway circuit-breaker: watch our OWN default gateway during the flood. If we
# flood hard enough to knock it offline we've DOSed the box — bail (below) and
# ratchet the rate down 20% for next cycle. Checked ~every 2s; 2 consecutive
# misses required so a single dropped ping doesn't false-trip.
_dfgw=$(dns_default_gw)
_gw_next_check=$((SECONDS + 2))
_gw_misses=0
_bailed=0
while (( SECONDS < stop_at )); do
  for record in $dnsfile; do
    for server in "${bad_records[@]}" "${bad_ips[@]}"; do

      # Stop the moment the burst window closes (break out of both loops).
      (( SECONDS >= stop_at )) && break 2

      # Self-DOS check (skip verbose/manual mode — foreground, can't DOS).
      if (( ! VERBOSE )) && [[ -n "$_dfgw" ]] && (( SECONDS >= _gw_next_check )); then
        _gw_next_check=$((SECONDS + 2))
        if dns_gw_alive "$_dfgw"; then
          _gw_misses=0
        else
          _gw_misses=$((_gw_misses + 1))
          if (( _gw_misses >= 2 )); then
            _elapsed=$(( SECONDS - _burst_start )); (( _elapsed < 1 )) && _elapsed=1
            _achieved=$(awk -v f="$fired" -v e="$_elapsed" 'BEGIN { printf "%d", f / e * 60 }')
            _newrate=$(dns_ceiling_penalize "$_achieved")
            echo "$(date) DNS flood knocked out default gateway $_dfgw after ${fired} digs (~${_achieved}/min) — BAILING; next burst throttles to ${_newrate}/min" | tee -a "$debug"
            kill $(jobs -p) 2>/dev/null
            _bailed=1
            break 2
          fi
        fi
      fi

      if (( VERBOSE )); then
        # Foreground + visible: see each lookup's result (for manual debugging).
        _out=$(dig +time=1 +tries=1 +short @"$server" "$record" 2>&1); _rc=$?
        printf '%s [dig @%s %s] rc=%s -> %s\n' "$(date '+%H:%M:%S')" \
               "$server" "$record" "$_rc" "${_out:-<empty>}"
      else
        # Throttle to at most _MAX_INFLIGHT concurrent digs. `wait -n` (bash
        # 4.3+) blocks until ONE background dig finishes, then we launch the next;
        # the sleep fallback covers older bash. jobs -rp counts only running
        # background jobs (the digs — nothing else is backgrounded here).
        while (( $(jobs -rp 2>/dev/null | wc -l) >= _MAX_INFLIGHT )); do
          wait -n 2>/dev/null || sleep "$pause_between"
        done
        # Print the launch so the fire-and-forget activity is visible in the
        # terminal / sim.log, then launch in the background and move straight on —
        # we never wait for the result. $SECONDS (seconds into the burst) is a
        # fork-free marker to gauge the rate, so the visibility costs no extra
        # process per dig.
        printf '[+%ss] dig @%s %s\n' "$SECONDS" "$server" "$record"
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
if (( _bailed )); then
  echo "$(date) DNS failures fired: ${fired} (BAILED on gateway loss — self-throttling next burst)" | tee -a "$debug"
else
  echo "$(date) DNS failures fired: ${fired}" | tee -a "$debug"
fi
