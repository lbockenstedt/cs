#!/usr/bin/env bash
# ============================================================================ #
# GENERATED-COPY NOTICE — canonical source: clients/lib/common.sh              #
# clients/linux/common.sh is a byte-identical generated copy (the client       #
# deploy paths only ship flat per-platform files, so the lib cannot be served  #
# directly). Edit clients/lib/common.sh, then re-sync:                         #
#           cp clients/lib/common.sh clients/linux/common.sh                   #
# Verify:   cmp clients/lib/common.sh clients/linux/common.sh                  #
# ============================================================================ #
# common.sh — shared helpers for the linux sim client scripts.
#
# Deployed flat to /usr/local/scripts/common.sh: the API scripts list
# (/api/scripts/list), the GitHub linux/*.sh sync, the SMB share and
# install.sh's `cp *.sh` all ship every top-level .sh in clients/linux/, so it
# always travels with the scripts that source it. Source AFTER ini-parser.sh
# (apply_override reads the parsed config via get_value).
#
# Every helper here replaces copies that had drifted between simulation.sh,
# dashboard.sh and startup.sh — edit HERE (clients/lib/), not per-script.
version=0.02

# ── script version reporting ─────────────────────────────────────────────────
# So an operator can tell FOR SURE which script + which deployment is running.
# The deployed VERSION file (/usr/local/scripts/VERSION) is CI-maintained and
# can't drift, so it anchors each script's hand-bumped ``version=``.
#   sim_version_banner <name> <version>  → one startup line for a single script
#   sim_versions_report                  → a table of every deployed script's
#                                          version (startup.sh prints it at boot)
_sim_deploy_version() {
  local v; v=$(< /usr/local/scripts/VERSION 2>/dev/null)
  [[ -n "$v" ]] && printf '%s' "$v" || printf '?'
}
sim_version_banner() {
  echo "[${1:-script} v${2:-?} · deploy $(_sim_deploy_version)] running" \
    | tee -a /usr/local/scripts/sim.log 2>/dev/null
}
sim_versions_report() {
  echo "=== client script versions (deploy $(_sim_deploy_version)) ===" \
    | tee -a /usr/local/scripts/sim.log 2>/dev/null
  local f v
  for f in /usr/local/scripts/*.sh; do
    v=$(grep -m1 -oE '^version=[^[:space:]]*' "$f" 2>/dev/null | cut -d= -f2)
    printf '  %-22s v%s\n' "$(basename "$f")" "${v:-—}" \
      | tee -a /usr/local/scripts/sim.log 2>/dev/null
  done
}

# ── username ────────────────────────────────────────────────────────────────
# Sets $username from $HOSTNAME: the prefix before the first '-' (the whole
# hostname when there is no dash). Pure bash — replaces the
# `echo $HOSTNAME | cut -d "-" -f 1` pipeline every script forked.
derive_username() {
  username="${HOSTNAME%%-*}"
}

# ── s0-s9 bucket ────────────────────────────────────────────────────────────
# Sets $bucket (0-9) via zlib.crc32(hostname) % 10 — MUST stay identical to
# sim_config.bucket_for() on the spoke. The python3 fork happens ONCE per boot
# and is cached in /tmp keyed by hostname; simulation.sh, dashboard.sh and
# startup.sh each used to fork python3 for the same constant on every refresh.
_BUCKET_CACHE="/tmp/client-sim-bucket.cache"
derive_bucket() {
  bucket=""
  local cached_host="" cached_bucket=""
  if [[ -r "$_BUCKET_CACHE" ]]; then
    read -r cached_host cached_bucket < "$_BUCKET_CACHE" 2>/dev/null || true
    if [[ "$cached_host" == "$HOSTNAME" && "$cached_bucket" =~ ^[0-9]$ ]]; then
      bucket="$cached_bucket"
      return 0
    fi
  fi
  bucket=$(python3 -c "import zlib; print(zlib.crc32('${HOSTNAME}'.encode()) % 10)")
  if [[ "$bucket" =~ ^[0-9]$ ]]; then
    # startup.sh sources this as ROOT at boot and populates the cache (root:644),
    # so a later sim-user write on a miss would fail. GROUP the redirect (a bare
    # `> file 2>/dev/null` does NOT suppress a redirection-open failure) and
    # chmod 0666 so any tier can rewrite it.
    { printf '%s %s\n' "$HOSTNAME" "$bucket" > "$_BUCKET_CACHE"; } 2>/dev/null || true
    chmod 0666 "$_BUCKET_CACHE" 2>/dev/null || true
  fi
}

# ── adapter detection ───────────────────────────────────────────────────────
# Sets $wladapter / $eadapter. Unified SUPERSET patterns — startup.sh's wired
# pattern included `ens` while simulation.sh's didn't; the superset wins.
detect_wlan_adapter() {
  wladapter=$(ip -br a 2>/dev/null | grep "wlx\|wlan" | cut -d ' ' -f '1')
}
detect_eth_adapter() {
  eadapter=$(ip -br a 2>/dev/null | grep "enp\|eno\|eth0\|eth1\|eth2\|eth3\|eth4\|eth5\|eth6\|ens" | cut -d ' ' -f '1')
}

# Detect the ACTIVE PHY type dynamically — NOT from simulation.conf's sim_phy
# (which is the configured INTENT). Classifies ONLY the interface that owns the
# DEFAULT ROUTE (the box's real uplink, the one with a gateway):
#   wireless  → the negotiated 802.11 standard (ac/ax/n/g) via `iw dev link`
#               (VHT=ac, HE=ax, HT=n; legacy rates → g), else just "wireless"
#   ethernet  → "ethernet"
#   other     → the driver iface prefix (e.g. "br", "ppp")
# The backend/management API NIC gets an IP but NO default gateway (often a
# 169.254 link-local), so it is NEVER selected — that gateway-less NIC being
# grabbed by the old "first carrier-up iface" fallback (while wifi was still
# associating) was the "PHY shows ethernet for a few cycles then corrects"
# symptom. No default route yet → stay "unknown"; never guess "ethernet".
# Sets $phy_type. Best-effort + offline-tolerant (no network I/O — `iw`/`ip`
# read kernel/netlink state). Used by dashboard.sh so the PHY row reflects what
# is actually connected, not the config's choice.
detect_phy_type() {
  phy_type="unknown"
  local iface base
  iface=$(ip route show default 2>/dev/null \
    | awk '/default/{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
  [[ -z "$iface" ]] && return 0
  # Belt-and-suspenders: never classify off a link-local-only (169.254 APIPA)
  # interface — a NIC that never got a real DHCP lease. (A gateway-less iface
  # can't hold a default route, so this only guards a pathological route.)
  if ! ip -4 -o addr show dev "$iface" 2>/dev/null | grep 'inet ' | grep -qv '169\.254\.'; then
    return 0
  fi
  base="${iface%%@*}"
  case "$base" in
    wl*)  phy_type="wireless"; _detect_wifi_std "$iface" ;;
    en*|eth*) phy_type="ethernet" ;;
    *)    phy_type="$base" ;;
  esac
}
# Resolve the negotiated 802.11 standard from `iw dev <iface> link`'s TX line.
# `iw` is present on these boxes (simulation.sh uses `iw event`); if missing or
# not associated, leaves phy_type as "wireless". VHT checked before HT because
# "VHT-MCS" contains the "HT-" substring.
_detect_wifi_std() {
  local iface="$1" tx
  tx=$(iw dev "$iface" link 2>/dev/null | awk '/TX:/{print; exit}')
  case "$tx" in
    *VHT-*) phy_type="802.11ac" ;;
    *HE-*)  phy_type="802.11ax" ;;
    *HT-*)  phy_type="802.11n"  ;;
    *Bit/s*) phy_type="802.11g" ;;
    *)      phy_type="wireless" ;;
  esac
}

# ── per-user override ───────────────────────────────────────────────────────
# CS_OVERRIDE_KEYS is the SUPERSET of the per-script lists that had drifted:
# dashboard.sh was missing the collab_* / dns_* / dot1x_password / address
# keys, simulation.sh was missing web_server. apply_override reads the
# [$username] section (get_value) and, when set, wins over the bucket/global
# value. Requires ini-parser.sh sourced first and $username set
# (derive_username). simulation_id pinning stays per-script (it must be
# validated against ^s[0-9]$ before use).
CS_OVERRIDE_KEYS=(kill_switch sim_load github_repo repo_location site_based_ssid iperf_bw \
  wsite sim_phy ssid ssidpw dhcp_fail dns_fail dns_latency assoc_fail port_flap ping_test download iperf \
  www_traffic ssidpw_fail auth_fail mac_auth_fail mac_auth_fail_mac smb_address ping_address \
  dns_bad_ip_1 dns_bad_ip_2 dns_bad_ip_3 dns_bad_record_1 dns_bad_record_2 \
  dns_bad_record_3 iperf_server dot1x_password \
  collab collab_app collab_bw collab_time collab_server web_server)

apply_override() {
  local var=$1
  local val
  val=$(get_value "$username" "$var")
  [[ -n "${val}" ]] && declare -g "$var=$val"
}

apply_overrides() {
  local _k
  for _k in "${CS_OVERRIDE_KEYS[@]}"; do
    apply_override "$_k"
  done
}

# ── JSON string escaping ────────────────────────────────────────────────────
# Escape a value for embedding in a double-quoted JSON string (pure bash).
json_escape() {
  local value="${1-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

# ── query-name rotation (negative-cache aware) ──────────────────────────────
# Every burst used to run `shuf` over the whole name file and consume only the
# first few hundred entries. Successive bursts therefore re-drew at random and
# overlapped heavily -- and a resolver CACHES NXDOMAIN (negative TTL, commonly
# 300-3600s), so a repeated name answers instantly from cache with no recursion
# and produces NO latency. Roughly the same failure as a typo'd TLD: the query
# fires, something comes back fast, and the sim looks busy while generating no
# signal.
#
# A persisted cursor walks the shuffled list end-to-end before any name repeats.
# With 10k names and a few hundred consumed per burst, a given name is not seen
# again for many bursts -- far beyond any negative TTL. The order is reshuffled
# only when the list is exhausted, so the walk stays a walk rather than a
# re-draw. State lives on tmpfs: losing it at boot just means a fresh shuffle.
_DNS_NAMES_SRC="/usr/local/scripts/dns_fail.txt"
_DNS_NAMES_ORDER=""
_DNS_NAMES_CURSOR=""

# Write a shuffled copy ATOMICALLY. `shuf src > order` truncates the target
# BEFORE shuf runs, so on a client without coreutils' shuf the order file was
# left EMPTY -- and an empty order file makes dns_names_take fall back to
# `head -n`, returning the SAME names forever. That is silent and permanent, and
# it recreates the exact negative-cache problem the cursor exists to avoid.
_dns_names_shuffle() {
  local tmp="${_DNS_NAMES_ORDER}.tmp.$$"
  if shuf "$_DNS_NAMES_SRC" > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
    mv -f "$tmp" "$_DNS_NAMES_ORDER" 2>/dev/null && return 0
  fi
  # No shuf (or it failed): an UNSHUFFLED walk still visits every name exactly
  # once per pass, which is what the negative cache actually cares about.
  if cp "$_DNS_NAMES_SRC" "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
    mv -f "$tmp" "$_DNS_NAMES_ORDER" 2>/dev/null && return 0
  fi
  rm -f "$tmp" 2>/dev/null
  return 1
}

_dns_names_init() {
  [[ -n "$_DNS_NAMES_ORDER" ]] && return 0
  local d
  for d in /dev/shm /tmp; do
    [[ -d "$d" && -w "$d" ]] || continue
    _DNS_NAMES_ORDER="$d/dns_names_order"
    _DNS_NAMES_CURSOR="$d/dns_names_cursor"
    break
  done
  [[ -n "$_DNS_NAMES_ORDER" ]] || return 1
  if [[ ! -s "$_DNS_NAMES_ORDER" ]]; then
    _dns_names_shuffle || return 1
    printf '0' > "$_DNS_NAMES_CURSOR" 2>/dev/null
    chmod 0666 "$_DNS_NAMES_ORDER" "$_DNS_NAMES_CURSOR" 2>/dev/null || true
  fi
  [[ -s "$_DNS_NAMES_CURSOR" ]] || printf '0' > "$_DNS_NAMES_CURSOR" 2>/dev/null
  return 0
}

# Echo the next $1 names and advance the cursor. Reshuffles and wraps at the end
# of the list. Falls back to a plain shuf slice if state is unavailable, so a
# read-only /tmp degrades to the old behaviour instead of firing nothing.
dns_names_take() {
  local n="${1:-500}" cur total
  if ! _dns_names_init; then
    shuf -n "$n" "$_DNS_NAMES_SRC" 2>/dev/null || head -n "$n" "$_DNS_NAMES_SRC"
    return
  fi
  # NB: `$(< f)` is bash's fast-read form and CANNOT be piped -- `$(< f | tr)`
  # leaves the redirect with no command to feed and yields an EMPTY string, so
  # the cursor read silently returned 0 every time and the walk restarted from
  # the top on every call. Strip with parameter expansion instead of a pipe.
  # `$(< f)` is bash's fast-read and is defeated by ANY extra redirection:
  # both `$(< f | tr)` and `$(< f 2>/dev/null)` yield an EMPTY string, because
  # what is left is a redirect with no command to run. That made every cursor
  # read return 0, so the walk restarted from the top on each call and the
  # rotation silently did nothing. Guard with -r and read bare.
  cur=""
  [[ -r "$_DNS_NAMES_CURSOR" ]] && cur=$(< "$_DNS_NAMES_CURSOR")
  cur="${cur//[^0-9]/}"
  [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
  # BSD `wc -l < f` pads with leading spaces ("    1000") while GNU does not.
  # Unstripped, the numeric test below fails, total reads 0, and the function
  # silently falls back to `head -n` and NEVER advances the cursor -- the whole
  # rotation quietly stops working.
  total=$(wc -l < "$_DNS_NAMES_ORDER" 2>/dev/null | tr -d '[:space:]')
  [[ "$total" =~ ^[0-9]+$ ]] || total=0
  (( total == 0 )) && { head -n "$n" "$_DNS_NAMES_SRC"; return; }
  if (( cur >= total )); then          # full pass done -> reshuffle, start over
    _dns_names_shuffle || true
    cur=0
  fi
  sed -n "$(( cur + 1 )),$(( cur + n ))p" "$_DNS_NAMES_ORDER" 2>/dev/null
  printf '%s' "$(( cur + n ))" > "$_DNS_NAMES_CURSOR" 2>/dev/null
}

# How far through the list are we (for logging)?
dns_names_progress() {
  _dns_names_init || { printf 'n/a'; return; }
  local cur total
  # NB: `$(< f)` is bash's fast-read form and CANNOT be piped -- `$(< f | tr)`
  # leaves the redirect with no command to feed and yields an EMPTY string, so
  # the cursor read silently returned 0 every time and the walk restarted from
  # the top on every call. Strip with parameter expansion instead of a pipe.
  # `$(< f)` is bash's fast-read and is defeated by ANY extra redirection:
  # both `$(< f | tr)` and `$(< f 2>/dev/null)` yield an EMPTY string, because
  # what is left is a redirect with no command to run. That made every cursor
  # read return 0, so the walk restarted from the top on each call and the
  # rotation silently did nothing. Guard with -r and read bare.
  cur=""
  [[ -r "$_DNS_NAMES_CURSOR" ]] && cur=$(< "$_DNS_NAMES_CURSOR")
  cur="${cur//[^0-9]/}"
  [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
  total=$(wc -l < "$_DNS_NAMES_ORDER" 2>/dev/null | tr -d '[:space:]'); [[ "$total" =~ ^[0-9]+$ ]] || total=0
  printf '%s/%s' "$cur" "$total"
}

# ── DNS flood self-throttle (gateway circuit-breaker) ───────────────────────
# dns_fail / dns_latency flood background digs to trip Central's rate-based
# alarm. Pushed too hard on a small VM the box saturates until its OWN default
# gateway stops answering — the adapter drops association. That is the clean
# "I've DOSed myself" signal: a client whose gateway is gone emits nothing
# useful, so the sim BAILS (simulation.sh's cycle-top _wait_gateway recovery then
# re-associates the adapter) and ratchets its sustainable rate DOWN 20%, persisted.
# The next burst starts at that lower rate; if it DOSes again it drops another
# 20%, converging on a stable rate in a couple of cycles — "close enough, not
# wasteful". ONE shared ceiling for both DNS sims (same dig capacity). The stored
# value is a rate in failures/min — the currency the quota engine will consume
# once learning-mode reporting lands (Phase 2). Down-only in Phase 1: the upward
# re-probe is a learning-mode behavior (Phase 2); clear the state file to reset.
# BOOT-VOLATILE ON PURPOSE. This lives on tmpfs (/dev/shm), so every reboot
# restores the CONFIGURED rate. It used to sit in /usr/local/scripts and survive
# reboots, which meant the ratchet was effectively one-way: a bail multiplies the
# rate by 0.8 EVERY time, while the up-probe only fires on ~1 in 5 clean bursts,
# so a client that had a bad period never climbed back and the whole fleet's DNS
# output drifted toward the floor with nothing reporting it. With the floor at 0
# a client could even pin itself at "generate nothing" permanently.
#
# /dev/shm rather than /tmp: it is tmpfs on every target (so it is genuinely
# cleared at boot, which /tmp is not guaranteed to be on Debian) and it is 1777,
# so the sim can write it without root.
_DNS_CEILING_FILE="/dev/shm/dns_ceiling.state"                  # self-throttle rate (failures/min), cleared at boot
_DNS_CEILING_FILE_LEGACY="/usr/local/scripts/dns_ceiling.state"  # pre-tmpfs location; removed on first use
# [simulation] dns_rate_floor — the lowest rate the ratchet may fall to.
# It used to be a hard 0, which let a client that kept tripping ratchet itself
# all the way to ZERO and then generate no DNS traffic at all -- silently, since
# a sidelined client still looks healthy. Several clients reached near-0 that way
# and the fleet stopped producing alerts. A floor still self-throttles a
# struggling client without ever removing it from the fleet.
dns_rate_floor() {
  local t; t=$(get_value 'simulation' 'dns_rate_floor' 2>/dev/null)
  [[ "$t" =~ ^[0-9]+$ ]] || t=100
  printf '%s' "$t"
}
_DNS_UPPROBE_EVERY=5    # production AIMD: ~1 in N clean bursts, nudge the rate UP to re-test capacity

# Default-gateway IP (the sim's real uplink), empty if there is no default route.
dns_default_gw() { ip route 2>/dev/null | grep -oP 'default via \K\S+' | head -1; }

# [simulation] gw_ping_timeout_s — per-ping ICMP timeout (seconds) for the gateway
# liveness checks (default 4). Under heavy DNS-flood load the gateway RTT climbs
# toward ~1.5s and beyond; a timeout at/under that reads a slow-but-ALIVE gateway
# as dead and false-ratchets the DNS rate toward zero. Keep it comfortably above
# the loaded RTT. Editable in the WebUI sim-config (sim-views.js).
gw_ping_timeout_s() { local t; t=$(get_value 'simulation' 'gw_ping_timeout_s'); [[ "$t" =~ ^[0-9]+$ ]] || t=4; printf '%s' "$t"; }

# Gateway UP? Send 5 pings; UP if ANY of them replies (`ping -c5` exits 0 on any
# reply). Five tries + a tolerant per-ping timeout mean one slow/dropped echo under
# load never reads as down — so the flood KEEPS FIRING while the gateway is alive.
dns_gw_alive() { local gw="${1:-}"; [[ -n "$gw" ]] && ping -c5 -i0.3 -W"$(gw_ping_timeout_s)" "$gw" >/dev/null 2>&1; }

# "Is the gateway REALLY offline?" — a SECOND independent 5-ping round; returns 0
# (offline) ONLY when all 5 miss. Paired with dns_gw_alive above (in the bail
# condition), declaring the gateway down needs 10/10 loss across two rounds = a
# real outage, not a WiFi/busy-VM/high-RTT blip.
dns_gw_confirmed_down() {
  local gw="${1:-}"; [[ -n "$gw" ]] || return 1
  ! ping -c5 -i0.3 -W"$(gw_ping_timeout_s)" "$gw" >/dev/null 2>&1
}

# ── gateway probe: BACKGROUND, non-blocking ─────────────────────────────────
# The probe used to run INLINE in the dig loop: `ping -c5 -i0.3 -W4` blocks for
# ~1.2s even when the gateway answers instantly, and it fired every 2s. The
# flood therefore ran ~2s, froze ~1.2s, repeated -- roughly a 38% duty-cycle
# loss on a HEALTHY client, so a configured 750/min actually delivered ~465.
#
# Worse, an inline probe competes with the very load it is measuring: under a
# heavy burst the ping process can simply fail to be scheduled, and 5 missed
# replies then look identical to a real outage. Running it in a separate process
# that only writes a state file means the dig loop's cost to check the gateway
# is one file read, and the probe keeps its own timing regardless of how busy
# the flood is.
_DNS_GW_STATE_FILE=""        # set by dns_gw_probe_start (PID-suffixed)
_DNS_GW_PROBE_PID=""

# Start the background prober. Per-PID state file so dns_fail and dns_latency
# running concurrently never share (or double-write) one file.
dns_gw_probe_start() {
  local gw="$1" interval="${2:-2}"
  [[ -n "$gw" ]] || return 1
  # tmpfs preferred (never touches the SD card / disk on a client that writes
  # this every 2s), but fall back to /tmp rather than failing: a start failure
  # means the burst runs with NO gateway protection at all, which is far worse
  # than writing the state a little slower.
  local _d
  for _d in /dev/shm /tmp; do
    [[ -d "$_d" && -w "$_d" ]] || continue
    _DNS_GW_STATE_FILE="$_d/dns_gw_state.$$"
    printf 'unknown' > "$_DNS_GW_STATE_FILE" 2>/dev/null && break
    _DNS_GW_STATE_FILE=""
  done
  [[ -n "$_DNS_GW_STATE_FILE" ]] || return 1
  chmod 0666 "$_DNS_GW_STATE_FILE" 2>/dev/null || true
  (
    while :; do
      if dns_gw_alive "$gw"; then printf 'up' > "$_DNS_GW_STATE_FILE" 2>/dev/null
      else                        printf 'down' > "$_DNS_GW_STATE_FILE" 2>/dev/null; fi
      sleep "$interval"
    done
  ) &
  _DNS_GW_PROBE_PID=$!
  # Detach from job control so stopping it does not print "Terminated" into
  # sim.log — the prober is stopped on EVERY burst, so that message would appear
  # constantly and read like an error.
  disown "$_DNS_GW_PROBE_PID" 2>/dev/null || true
  return 0
}

# Always call from an EXIT trap: a leaked prober keeps pinging forever after the
# burst ends, and a fleet of them would be a real load of its own.
dns_gw_probe_stop() {
  [[ -n "$_DNS_GW_PROBE_PID" ]] && kill "$_DNS_GW_PROBE_PID" 2>/dev/null
  [[ -n "$_DNS_GW_STATE_FILE" ]] && rm -f "$_DNS_GW_STATE_FILE" 2>/dev/null
  _DNS_GW_PROBE_PID=""; _DNS_GW_STATE_FILE=""
}

# up | down | unknown. 'unknown' before the first round completes — callers must
# treat it as "do not act", never as down, or every burst would pause on startup.
dns_gw_probe_state() {
  local s=""
  [[ -n "$_DNS_GW_STATE_FILE" && -f "$_DNS_GW_STATE_FILE" ]] && s=$(< "$_DNS_GW_STATE_FILE")
  printf '%s' "${s:-unknown}"
}

# Seconds the gateway must read 'up' CONTINUOUSLY before the flood resumes.
# Resuming the instant the gateway answers one ping re-loads a link that has not
# finished clearing, which just trips the next pause immediately.
dns_gw_settle_s() {
  local t; t=$(get_value 'simulation' 'dns_gw_settle_s' 2>/dev/null)
  [[ "$t" =~ ^[0-9]+$ ]] || t=10
  printf '%s' "$t"
}

# Block until the gateway has been continuously up for the settle window, or
# until $1 (an absolute SECONDS deadline) passes. Echoes the seconds waited.
dns_gw_wait_settled() {
  local deadline="$1" settle start=$SECONDS ok=0
  settle=$(dns_gw_settle_s)
  while (( SECONDS < deadline )); do
    if [[ "$(dns_gw_probe_state)" == "up" ]]; then
      ok=$(( ok + 1 ))
      (( ok >= settle )) && break
    else
      ok=0            # any miss restarts the settle window
    fi
    sleep 1
  done
  printf '%s' "$(( SECONDS - start ))"
}

# ── pause accounting (sliding window) ───────────────────────────────────────
# "More than N pauses in W seconds" is the signal that the client genuinely
# cannot sustain the rate -- as opposed to one transient, which pausing alone
# recovers from. A TRUE sliding window, not a per-burst counter: a per-burst
# count resets at the boundary, so 5 pauses at the end of one burst plus 5 at
# the start of the next would never trip.
# [simulation] dns_pause_window_s / dns_pause_max — "more than MAX pauses inside
# WINDOW seconds means the rate itself is too high". Knobs because the right
# values depend on how twitchy a given fleet's gateway is, and getting them wrong
# in either direction is costly: too strict ratchets a healthy client, too loose
# lets a struggling one pause forever without ever slowing down.
dns_pause_window_s() {
  local t; t=$(get_value 'simulation' 'dns_pause_window_s' 2>/dev/null)
  [[ "$t" =~ ^[0-9]+$ ]] || t=300
  printf '%s' "$t"
}
dns_pause_max() {
  local t; t=$(get_value 'simulation' 'dns_pause_max' 2>/dev/null)
  [[ "$t" =~ ^[0-9]+$ ]] || t=5
  printf '%s' "$t"
}
# Resolved LAZILY on first use, never at source time: common.sh is sourced
# BEFORE process_ini_file runs, so reading the config here would silently pin
# both to their defaults and the knobs would appear to do nothing.
_DNS_PAUSE_WINDOW_S=""
_DNS_PAUSE_MAX=""
_dns_pause_init() {
  [[ -n "$_DNS_PAUSE_WINDOW_S" ]] || _DNS_PAUSE_WINDOW_S=$(dns_pause_window_s)
  [[ -n "$_DNS_PAUSE_MAX" ]]      || _DNS_PAUSE_MAX=$(dns_pause_max)
}
_dns_pause_times=()
_dns_pause_recent=0

# Record a pause and refresh _dns_pause_recent. Call DIRECTLY (not in $( )) —
# it prunes the array in place, which a subshell would discard.
dns_pause_record() {
  local now cutoff t keep=()
  _dns_pause_init
  now=$(date +%s); cutoff=$(( now - _DNS_PAUSE_WINDOW_S ))
  _dns_pause_times+=("$now")
  for t in ${_dns_pause_times[@]+"${_dns_pause_times[@]}"}; do
    (( t >= cutoff )) && keep+=("$t")
  done
  _dns_pause_times=(${keep[@]+"${keep[@]}"})
  _dns_pause_recent=${#_dns_pause_times[@]}
}

# Has the client paused too often to be believed at this rate?
dns_pause_over_budget() { _dns_pause_init; (( _dns_pause_recent > _DNS_PAUSE_MAX )); }

# RECOVERY HOLD: gateway is STABLY up = $2 (default 4) consecutive dns_gw_alive
# rounds all pass (each round = 5 pings, any reply), ~2s apart. The pre-flood gate.
# These dongles hang off a USB PCI card
# passed through to the guest; on bus contention the guest CANNOT reset the bus
# (it doesn't own the PCI device), so the only recovery is to remove the load and
# let the bus clear. Resuming the flood the instant the adapter blips back
# re-contends a bus that hasn't finished clearing → it never recovers. So the
# flood stays OFF until the adapter answers several pings in a row (bus cleared),
# THEN resumes at the throttled rate. $3 = seconds between pings (default 2).
dns_gw_stable() {
  local gw="${1:-}" n="${2:-4}" gap="${3:-2}" i
  [[ -n "$gw" ]] || return 1
  for (( i = 0; i < n; i++ )); do
    dns_gw_alive "$gw" || return 1
    (( i < n - 1 )) && sleep "$gap"
  done
  return 0
}

# Persisted rate, empty if none/invalid. NB: `$(< f 2>/dev/null)` is NOT the bash
# read-shortcut (the extra redirect makes it a null command that yields ""), and
# reading a missing file leaks a "No such file" error — so guard on -f.
_dns_ceiling_saved() {
  local saved=""
  # Drop any pre-tmpfs ceiling left on disk. It is NOT migrated: its whole
  # problem was surviving reboots, and a client carrying a ratcheted-down value
  # from a bad period would keep it forever. Deleting it is the reset. Left in
  # place it would also mislead anyone inspecting /usr/local/scripts, since the
  # file would still be there but no longer read.
  [[ -f "$_DNS_CEILING_FILE_LEGACY" ]] && rm -f "$_DNS_CEILING_FILE_LEGACY" 2>/dev/null
  [[ -f "$_DNS_CEILING_FILE" ]] && saved=$(< "$_DNS_CEILING_FILE")
  [[ "$saved" =~ ^[0-9]+$ ]] && echo "$saved"
}

# Effective per-burst rate = min(configured target, persisted self-throttle).
# NB: >= 0 (not > 0) so a persisted ceiling of 0 is HONORED — a fully-throttled
# (sidelined) client stays at 0, not silently reset to the configured rate.
dns_ceiling_rate() {
  local configured=$1 saved
  saved=$(_dns_ceiling_saved)
  [[ -n "$saved" ]] && (( saved >= 0 && saved < configured )) && { echo "$saved"; return; }
  echo "$configured"
}

# Persist a rate (cross-tier writable — same root/sim ownership trap as
# client-sim-update.stamp: grouped redirect + world-writable).
_dns_ceiling_write() {
  { printf '%s' "$1" > "$_DNS_CEILING_FILE"; } 2>/dev/null || true
  chmod 0666 "$_DNS_CEILING_FILE" 2>/dev/null || true
}

# Gateway offline (we overloaded it) → AIMD multiplicative DECREASE: persist a new
# ceiling of (rate * 0.8), floored, and echo it.
# Multiplicative decrease. Gentle (x0.95) BY DESIGN: the ratchet no longer fires
# on a single transient -- the flood now PAUSES and resumes for those, and this
# only runs when the client has paused more than _DNS_PAUSE_MAX times inside
# _DNS_PAUSE_WINDOW_S. A rare, well-evidenced signal deserves a small nudge, not
# the old x0.8 cliff that took ~15 clean bursts to climb back from.
_DNS_RATE_PENALTY=0.95
dns_ceiling_penalize() {
  local achieved=$1 next _floor
  next=$(awk -v a="$achieved" -v f="$_DNS_RATE_PENALTY" 'BEGIN { printf "%d", a * f }')
  # x0.95 of a small number can floor to itself and never move; force progress.
  (( next >= achieved )) && next=$(( achieved - 1 ))
  _floor=$(dns_rate_floor)
  (( next < _floor )) && next=$_floor
  _dns_ceiling_write "$next"
  echo "$next"
}

# Production AIMD up-probe (additive increase): on a clean burst while throttled
# BELOW the configured target, nudge the rate ~+20% to test whether conditions
# improved (dongle recovered, or the shared USB bus freed up as other clients
# dropped off). If the nudge reaches the target, clear the throttle entirely
# (fully recovered). $1=current rate, $2=configured target. Echoes the next rate.
# A DOS at the higher rate is caught by the 20% multiplicative decrease — so the
# client rides its VARYING capacity (e.g. 50→80 when things improve) instead of
# staying pinned at a transient low. Caller gates on frequency + throttled state.
dns_ceiling_upprobe() {
  local cur=$1 configured=$2 next
  next=$(awk -v c="$cur" 'BEGIN { printf "%d", c * 1.2 + 1 }')
  if (( next >= configured )); then
    dns_ceiling_reset      # recovered to the target — drop the throttle state
    echo "$configured"
  else
    _dns_ceiling_write "$next"
    echo "$next"
  fi
}

# Clear the persisted self-throttle (fully recovered → back to the configured target).
# Drop the self-throttle entirely — back to the CONFIGURED rate. Clears the
# legacy on-disk path as well, so calling this on a client that has not yet had
# its /usr/local/scripts copy removed still fully resets it.
dns_ceiling_reset() {
  rm -f "$_DNS_CEILING_FILE" "$_DNS_CEILING_FILE_LEGACY" 2>/dev/null || true
}

# ── DNS-latency server selection (self-healing) ──────────────────────────────
# The dns_latency sim needs a server whose lookups take >= the latency threshold
# so Central's DNS-latency alert fires. Real external DNS servers BLACKLIST a
# flooding client over time — they start REFUSING FAST, so lookups are no longer
# slow and the alert dies. So dns_latency.sh keeps a POOL of servers (any number)
# and uses ONE confirmed slow, rotating to the next when the current drops below
# the threshold (probed at burst start AND periodically mid-burst). A TIMEOUT
# (~1s) counts as slow (kept) — a blacklist that DROPS packets still produces the
# latency condition; only a FAST response (fast-refuse or genuinely quick) rotates.
_DNS_LAT_STATE_FILE="/usr/local/scripts/dns_latency_server.state"  # persisted current server
_DNS_LAT_MAX_PROBES=10   # cap the rotation walk so a mostly-blacklisted pool can't stall a burst

# [simulation] dns_latency_threshold_ms (default 500) / dns_latency_recheck_s (default 30).
dns_lat_threshold_ms() { local t; t=$(get_value 'simulation' 'dns_latency_threshold_ms'); [[ "$t" =~ ^[0-9]+$ ]] || t=500; printf '%s' "$t"; }
dns_lat_recheck_s()    { local s; s=$(get_value 'simulation' 'dns_latency_recheck_s');    [[ "$s" =~ ^[0-9]+$ ]] || s=30;  printf '%s' "$s"; }

# Wall-clock ms of ONE dig against $1 (record $2). A fast answer reads < threshold;
# a slow answer OR a +time timeout (~1s) both read high — so both keep the server.
dns_lat_probe_ms() {
  local server="$1" record="${2:-example.com}" a b
  a=$(date +%s%3N 2>/dev/null) || a=$(( $(date +%s) * 1000 ))
  dig +time=1 +tries=1 +short @"$server" "$record" >/dev/null 2>&1
  b=$(date +%s%3N 2>/dev/null) || b=$(( $(date +%s) * 1000 ))
  printf '%s' "$(( b - a ))"
}

# True when $1's lookups are still slow enough (>= threshold) to feed the alert.
dns_lat_ok() { local s="$1"; [[ -n "$s" ]] || return 1; (( $(dns_lat_probe_ms "$s" "${2:-}") >= $(dns_lat_threshold_ms) )); }

_dns_lat_saved() { [[ -f "$_DNS_LAT_STATE_FILE" ]] && { local v; v=$(< "$_DNS_LAT_STATE_FILE"); printf '%s' "${v//[$'\n\r ']/}"; }; }
_dns_lat_write() { { printf '%s' "$1" > "$_DNS_LAT_STATE_FILE"; } 2>/dev/null || true; chmod 0666 "$_DNS_LAT_STATE_FILE" 2>/dev/null || true; }

# Pick a server from the pool (args after $1=record) whose lookups clear the
# threshold. Starts from the persisted current (so a good one STICKS), probes it,
# and only if it's dropped below threshold walks the rest for the first that
# clears (capped at _DNS_LAT_MAX_PROBES). Persists + echoes the chosen server;
# if none of the probed set clears, keeps current (else the first) best-effort.
dns_lat_select() {
  local record="$1"; shift
  local pool=("$@"); (( ${#pool[@]} )) || { printf ''; return; }
  local cur; cur=$(_dns_lat_saved)
  local ordered=() s
  [[ -n "$cur" ]] && for s in "${pool[@]}"; do [[ "$s" == "$cur" ]] && ordered+=("$s"); done
  for s in "${pool[@]}"; do [[ "$s" == "$cur" ]] || ordered+=("$s"); done
  local n=0
  for s in "${ordered[@]}"; do
    (( n++ >= _DNS_LAT_MAX_PROBES )) && break
    if dns_lat_ok "$s" "$record"; then _dns_lat_write "$s"; printf '%s' "$s"; return; fi
  done
  local best="${cur:-${pool[0]}}"; _dns_lat_write "$best"; printf '%s' "$best"
}
