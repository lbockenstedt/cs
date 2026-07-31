#!/bin/bash
version=0.01
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
# RANDOMIZE the record order each burst (see dns_fail.sh) so the query stream
# varies per client/burst instead of the same file-order first-N.
dnsfile=$(shuf /usr/local/scripts/dns_fail.txt 2>/dev/null)
[[ -n "$dnsfile" ]] || dnsfile=$(< /usr/local/scripts/dns_fail.txt)   # fallback if shuf absent
# The dns_latency server POOL (any number). Prefer the [address] `dns_latency`
# list (space-separated, UNLIMITED — real DNS servers blacklist a flooding client
# over time, so we keep a big pool and rotate to a still-slow one); fall back to
# nothing when the list key is absent — the legacy dns_latency_1/2/3 slots are
# retired, this list is the only source.
dns_latency=$(get_value 'address' 'dns_latency')
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
apply_override 'dns_latency'
dns_latency="${dns_latency//,/ }"          # tolerate comma- OR space-separated lists
latencies=($dns_latency)
# Self-healing pick: use ONE server whose lookups are actually SLOW (>= the
# latency threshold), rotating away from any blacklisted one (now refuses fast).
# Persisted so a good server sticks; re-probed periodically in the burst below.
_lat_probe_record=$(head -1 /usr/local/scripts/dns_fail.txt 2>/dev/null); [[ -n "$_lat_probe_record" ]] || _lat_probe_record="example.com"
_lat_recheck_s=$(dns_lat_recheck_s)
_current=$(dns_lat_select "$_lat_probe_record" "${latencies[@]}")
[[ -n "$_current" ]] || _current="${latencies[0]}"
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

# Per-run rate override for manual ceiling / recovery testing (parallels
# DNS_MAX_INFLIGHT): DNS_LATENCY_RATE=<n> forces the rate AND bypasses the
# persisted self-throttle. Otherwise apply it — the AIMD ratchet's current rate.
# Shares the one DNS ceiling with dns_fail (same client dig capacity).
if [[ "${DNS_LATENCY_RATE:-}" =~ ^[0-9]+$ ]]; then
  _configured_rate=$DNS_LATENCY_RATE; rate_per_minute=$DNS_LATENCY_RATE
else
  _configured_rate=$rate_per_minute
  rate_per_minute=$(dns_ceiling_rate "$rate_per_minute")
fi

# Ceiling 0 = can't sustain any flood (bad USB/hub) — sideline: don't flood, avoid
# div-by-zero. Off until state clears / recloned / learning re-probes.
if (( rate_per_minute <= 0 )); then
  echo "$(date) DNS self-throttle floored this client to 0/min — can't sustain the flood (bad USB/hub?); sidelining, not flooding" | tee -a "$debug"
  exit 0
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
echo "$(date) Firing DNS latency lookups at ${rate_per_minute}/min for ${burst_seconds}s (max ${_MAX_INFLIGHT} digs in flight)${_throttle_note}" | tee -a "$debug"
if (( VERBOSE )); then
  echo "[verbose] server pool : ${latencies[*]:-<none>}"
  echo "[verbose] using server: ${_current:-<none>}  (threshold $(dns_lat_threshold_ms)ms, recheck every ${_lat_recheck_s}s)"
  echo "[verbose] records file: $(wc -l < /usr/local/scripts/dns_fail.txt 2>/dev/null) lines"
  echo "[verbose] pause between lookups: ${pause_between}s   (foreground — slower than sim mode)"
  echo "----------------------------------------"
fi

# Keep firing until the burst window is up, cycling through every record against
# every slow server.
stop_at=$((SECONDS + burst_seconds))
fired=0
_burst_start=$SECONDS
# Gateway circuit-breaker (shared logic with dns_fail): OFFLINE → bail + drop the
# OPERATING rate 20%. RECOVERY HOLD (start gate): the dongles are on a passed-
# through USB PCI card the guest can't bus-reset, so recovery = remove load + let
# the bus clear. Don't (re)start the flood until the gateway is STABLY up (several
# pings in a row); until then hold it OFF so a recovering bus isn't re-contended.
_dfgw=$(dns_default_gw)
if (( ! VERBOSE )) && [[ -n "$_dfgw" ]] && ! dns_gw_stable "$_dfgw"; then
  echo "$(date) default gateway $_dfgw not stably up (USB bus still clearing?) — holding the flood OFF this burst so the adapter can recover" | tee -a "$debug"
  exit 0
fi
_gw_next_check=$((SECONDS + 2))
_lat_next_check=$((SECONDS + _lat_recheck_s))
_bailed=0
while (( SECONDS < stop_at )); do
  for record in $dnsfile; do

    # Stop the moment the burst window closes.
    (( SECONDS >= stop_at )) && break 2

    # Gateway check (~every 2s): single ping, then CONFIRM with 5 pings — OFFLINE
    # only if ALL 5 fail. Offline → bail + drop the OPERATING rate 20%.
    if (( ! VERBOSE )) && [[ -n "$_dfgw" ]] && (( SECONDS >= _gw_next_check )); then
      _gw_next_check=$((SECONDS + 2))
      if ! dns_gw_alive "$_dfgw" && dns_gw_confirmed_down "$_dfgw"; then
        _newrate=$(dns_ceiling_penalize "$rate_per_minute")
        echo "$(date) default gateway $_dfgw OFFLINE (5/5 pings failed) after ${fired} digs at ${rate_per_minute}/min — BAILING; throttling to ${_newrate}/min next burst" | tee -a "$debug"
        kill $(jobs -p) 2>/dev/null
        _bailed=1
        break 2
      fi
    fi

    # Periodic latency re-check (~every _lat_recheck_s): if the current server has
    # dropped BELOW the threshold (blacklisted → now refusing fast), rotate to the
    # next confirmed-slow server so the latency alert stays fed.
    if (( ! VERBOSE )) && (( SECONDS >= _lat_next_check )); then
      _lat_next_check=$((SECONDS + _lat_recheck_s))
      if ! dns_lat_ok "$_current" "$_lat_probe_record"; then
        _new=$(dns_lat_select "$_lat_probe_record" "${latencies[@]}")
        if [[ -n "$_new" && "$_new" != "$_current" ]]; then
          echo "$(date) dns_latency: server $_current no longer slow (blacklisted?) — rotating to $_new" | tee -a "$debug"
          _current="$_new"
        fi
      fi
    fi

    # Fire against the single selected server.
    if (( VERBOSE )); then
      _out=$(dig +time=1 +tries=1 +short @"$_current" "$record" 2>&1); _rc=$?
      printf '%s [dig @%s %s] rc=%s -> %s\n' "$(date '+%H:%M:%S')" \
             "$_current" "$record" "$_rc" "${_out:-<empty>}"
    else
      # Throttle to at most _MAX_INFLIGHT concurrent digs (CPU guard). `wait -n`
      # (bash 4.3+) blocks until one background dig finishes; the sleep fallback
      # covers older bash. jobs -rp counts only the running background digs.
      while (( $(jobs -rp 2>/dev/null | wc -l) >= _MAX_INFLIGHT )); do
        wait -n 2>/dev/null || sleep "$pause_between"
      done
      printf '[+%ss] dig @%s %s\n' "$SECONDS" "$_current" "$record"
      dig +time=1 +tries=1 +short @"$_current" "$record" >/dev/null 2>&1 &
    fi

    fired=$((fired + 1))
    sleep "$pause_between"
  done
done

# Let any lookups still in flight finish, then record how many we fired.
(( VERBOSE )) || wait 2>/dev/null
if (( _bailed )); then
  echo "$(date) DNS latency lookups fired: ${fired} (BAILED on gateway loss — self-throttling next burst)" | tee -a "$debug"
elif (( ! VERBOSE )) && (( rate_per_minute > 0 && rate_per_minute < _configured_rate )) && (( RANDOM % _DNS_UPPROBE_EVERY == 0 )); then
  # Production AIMD up-probe (see dns_fail): throttled + clean → occasionally nudge
  # UP to re-test capacity; a DOS falls back 20%. Rides varying capacity (50→80).
  _up=$(dns_ceiling_upprobe "$rate_per_minute" "$_configured_rate")
  echo "$(date) DNS latency lookups fired: ${fired} — clean; probing rate UP ${rate_per_minute}→${_up}/min next burst (re-testing capacity)" | tee -a "$debug"
else
  echo "$(date) DNS latency lookups fired: ${fired}" | tee -a "$debug"
fi
