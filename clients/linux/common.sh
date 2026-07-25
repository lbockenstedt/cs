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
version=.01

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
