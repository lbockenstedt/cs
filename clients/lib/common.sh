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
version=.03

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
  www_traffic ssidpw_fail auth_fail smb_address ping_address dns_latency_1 dns_latency_2 \
  dns_latency_3 dns_bad_ip_1 dns_bad_ip_2 dns_bad_ip_3 dns_bad_record_1 dns_bad_record_2 \
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
_DNS_CEILING_FILE="/usr/local/scripts/dns_ceiling.state"        # persisted rate (failures/min)
_DNS_CONVERGED_FILE="/usr/local/scripts/dns_ceiling.converged"  # marker: learning found the ceiling
_DNS_RATE_FLOOR=30      # never throttle below this (below it a client is just broken)
_DNS_PROBE_MAX=20000    # cap the learning-mode upward probe (a client that never DOSes can't run away)

# Default-gateway IP (the sim's real uplink), empty if there is no default route.
dns_default_gw() { ip route 2>/dev/null | grep -oP 'default via \K\S+' | head -1; }

# Return 0 iff the gateway answers a single quick (1s) ping.
dns_gw_alive() { local gw="${1:-}"; [[ -n "$gw" ]] && ping -c1 -W1 "$gw" >/dev/null 2>&1; }

# Persisted rate, empty if none/invalid. NB: `$(< f 2>/dev/null)` is NOT the bash
# read-shortcut (the extra redirect makes it a null command that yields ""), and
# reading a missing file leaks a "No such file" error — so guard on -f.
_dns_ceiling_saved() {
  local saved=""
  [[ -f "$_DNS_CEILING_FILE" ]] && saved=$(< "$_DNS_CEILING_FILE")
  [[ "$saved" =~ ^[0-9]+$ ]] && echo "$saved"
}

# Effective per-burst rate. $1=configured, $2=learn (1 in learning mode).
#  production (learn=0): min(configured, persisted) — down-only self-throttle.
#  learning  (learn=1):  the persisted PROBE value (MAY exceed configured — it's
#                        hunting the ceiling from below), or configured to start.
dns_ceiling_rate() {
  local configured=$1 learn=${2:-0} saved
  saved=$(_dns_ceiling_saved)
  if (( learn )); then
    [[ -n "$saved" ]] && (( saved > 0 )) && { echo "$saved"; return; }
    echo "$configured"
  else
    [[ -n "$saved" ]] && (( saved > 0 && saved < configured )) && { echo "$saved"; return; }
    echo "$configured"
  fi
}

# Persist a rate (cross-tier writable — same root/sim ownership trap as
# client-sim-update.stamp: grouped redirect + world-writable).
_dns_ceiling_write() {
  { printf '%s' "$1" > "$_DNS_CEILING_FILE"; } 2>/dev/null || true
  chmod 0666 "$_DNS_CEILING_FILE" 2>/dev/null || true
}

# Gateway died while achieving $1 failures/min → persist ceiling = achieved*0.8
# (floored) and echo it. The DOS edge: in production the safety ratchet, in
# learning the ceiling we then hold 20% below.
dns_ceiling_penalize() {
  local achieved=$1 next
  next=$(awk -v a="$achieved" 'BEGIN { printf "%d", a * 0.8 }')
  (( next < _DNS_RATE_FLOOR )) && next=$_DNS_RATE_FLOOR
  _dns_ceiling_write "$next"
  echo "$next"
}

# Learning mode, CLEAN burst (no DOS) → probe the rate UP 20% to hunt the ceiling.
# $1 = the rate we just ran cleanly. Bounded by _DNS_PROBE_MAX. Echoes next rate.
dns_ceiling_relax() {
  local cur=$1 next
  next=$(awk -v c="$cur" 'BEGIN { printf "%d", c * 1.2 }')
  (( next > _DNS_PROBE_MAX )) && next=$_DNS_PROBE_MAX
  (( next <= cur )) && next=$(( cur + 1 ))   # always make forward progress
  _dns_ceiling_write "$next"
  echo "$next"
}

# Convergence marker — learning stops probing UP once it has found the ceiling
# (first DOS). Cleared (dns_ceiling_reset) to start a fresh learning run.
dns_ceiling_converged()      { [[ -f "$_DNS_CONVERGED_FILE" ]]; }
dns_ceiling_mark_converged() { { : > "$_DNS_CONVERGED_FILE"; } 2>/dev/null || true; chmod 0666 "$_DNS_CONVERGED_FILE" 2>/dev/null || true; }
dns_ceiling_reset()          { rm -f "$_DNS_CEILING_FILE" "$_DNS_CONVERGED_FILE" 2>/dev/null || true; }
