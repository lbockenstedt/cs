#!/bin/bash
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

# ============================================================
# Lab Manager — Client Simulator (CS) Spoke Installer
#
# Deploys the LM client simulator spoke.
# Safe to re-run (updates code, preserves credentials).
#
# DHCP (a cs-OWNED Kea instance on the second NIC):
#   The spoke provides DHCP for the isolated simulation-client network on a
#   second NIC via its OWN Kea DHCP4 instance (kea-dhcp4-sim) — SEPARATE from the
#   lm/dhcp module's Kea, so both coexist on an all-in-one box. The 2nd NIC is
#   auto-detected; if only one NIC is present, DHCP is skipped. The NIC must
#   already be attached to the container/host in Proxmox. Serves 169.253.1.0/24
#   with no router option (parity with the prior dnsmasq scope).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh \
#     | sudo bash -s -- --hub ws://HUB_IP:8765
#
# Re-running preserves the spoke secret + INSTALL_UUID (same identity). To
# reset identity in one shot (regenerate secret + UUID → spoke re-registers,
# needs hub approval) add --purge-env (alias --reset-identity):
#   … | sudo bash -s -- --hub wss://172.16.1.31:443 --purge-env
#
# Environment variable overrides (set before running):
#   DHCP_IFACE, DHCP_SUBNET, DHCP_PREFIX, DHCP_GATEWAY, DHCP_RANGE_START,
#   DHCP_RANGE_END, DHCP_LEASE_TIME, DHCP_SKIP (1 to skip DHCP entirely)
# ============================================================

HUB_URL="${HUB_URL:-}"
# Track whether the hub URL was explicitly given (arg or env). When NOT pinned
# the installer auto-discovers the hub via DNS (lm-hub.<dns-suffix>) then mDNS
# (_lm-hub._tcp.local.) after the venv is ready; if nothing is found HUB_URL is
# left empty and the spoke re-discovers at startup (BaseControlPlane.run).
HUB_URL_PINNED=0
[ -n "$HUB_URL" ] && HUB_URL_PINNED=1
# SPOKE_ID is OPTIONAL. When neither the SPOKE_ID env var nor --id is supplied
# the spoke derives its id from the current OS hostname at startup (see
# control_plane __main__), so a cloned+renamed container reconnects under a new
# id instead of being frozen to the hostname captured at install. A pinned --id
# (install_all.sh / explicit --id) is honored as-is. We only bake SPOKE_ID into
# .env + the unit when it was explicitly pinned; otherwise Python owns the id.
SPOKE_ID="${SPOKE_ID:-}"
SPOKE_ID_PINNED=0
[ -n "$SPOKE_ID" ] && SPOKE_ID_PINNED=1
SPOKE_SECRET=""
HUB_SECRET=""
ADMIN_TOKEN=""
SVC_USER="svc_lm"
LM_DIR="/opt/lm"
# Client API listener. The spoke owns the isolated sim-client DHCP scope
# (169.253.1.1/24 on the 2nd NIC) and is also the client API gateway on
# 169.253.1.1:8080. Binding 0.0.0.0 puts the listener on the DHCP NIC too;
# the cs-owned Kea instance serves 169.253.1.0/24 with no router option, so
# clients reach the listener directly. Override either before running.
# 8080 (not 8000): the LM hub serves its admin WebUI/API on 0.0.0.0:8000, and
# in hub mode the cs spoke runs on the SAME box — binding 8000 here collided
# with the hub and took the WebUI down. The cs client API takes 8080.
CS_API_PORT="${CS_API_PORT:-8080}"
CS_API_HOST="${CS_API_HOST:-0.0.0.0}"
# Migrate the pre-collision default: 8000 collides with the hub WebUI/API on
# the same box, so a stale CS_API_PORT=8000 baked into an existing .env by a
# prior install is bumped to 8080. Any other explicitly-chosen port is kept.
[ "${CS_API_PORT:-}" = "8000" ] && CS_API_PORT="8080"

# ── DHCP (sim-client isolated network) — cs-owned Kea instance ─────────────────
# A second NIC runs a cs-owned Kea DHCP4 instance for the isolated simulation-
# client network. Auto-detected (2nd NIC) unless DHCP_IFACE is set. DHCP_SKIP=1
# skips entirely. Same subnet/pool/lease behaviour the prior dnsmasq scope had.
DHCP_IFACE="${DHCP_IFACE:-}"
DHCP_SKIP="${DHCP_SKIP:-0}"
DHCP_SUBNET="${DHCP_SUBNET:-169.253.1.0}"
DHCP_PREFIX="${DHCP_PREFIX:-24}"
DHCP_GATEWAY="${DHCP_GATEWAY:-169.253.1.1}"
DHCP_RANGE_START="${DHCP_RANGE_START:-169.253.1.11}"
DHCP_RANGE_END="${DHCP_RANGE_END:-169.253.1.254}"
DHCP_LEASE_TIME="${DHCP_LEASE_TIME:-1h}"

# TLS cert verification is OFF by default (self-signed hub cert → encrypt
# without auth). Pass --tls-verify --tls-ca-cert <path> to make this spoke
# verify the hub cert. A standalone cs spoke is remote, so the hub CA cert
# MUST be supplied (--tls-ca-cert) — there is no local hub cert to default to.
TLS_VERIFY=false
TLS_CA_CERT=""
# Agent listener (split topology): ON by default — a pxmx host agent dials
# THIS cs spoke's /ws/agent directly (wss://<cs>:443/ws/agent) instead of the
# pxmx spoke, since most deployments route the agent relationship through cs
# (see AgentHostingControlPlane / CSControlPlane). Generates its own
# self-signed cert + grants CAP_NET_BIND_SERVICE, same pattern as
# install_pxmx.sh's standalone mode. --no-agent-listener opts OUT for the rare
# all-in-one/relay-only deployment, where this cs spoke never binds :443 and
# agents go through the pxmx spoke (or the hub's /ws/agent byte-proxy) instead.
CS_AGENT_LISTENER=1
# --infra-only: set up ONLY the OS/host-level simulation infrastructure (cs-owned
# Kea DHCP on the 2nd NIC, sysctl rp_filter, the agent-listener self-signed TLS cert,
# and /etc/lm-cs-agent/config.json) then exit — WITHOUT cloning the spoke code,
# building the venv, writing the spoke .env, extracting lm core, creating the
# lm-cs systemd unit, or installing the rollback watchdog/sudoers. This is the
# mode the generic agent (lm-agent, User=root) invokes when it hosts the CS
# "simulation" role IN-PROCESS: the agent owns the process + self-updates and
# gets its runtime config from the hub push, so it only needs the host-level
# infra this installer would otherwise lay down. Idempotent + non-interactive.
INFRA_ONLY=0
# --purge-env: delete the existing spoke .env BEFORE install so the secret +
# INSTALL_UUID are regenerated from scratch (a fresh identity). Normally the
# installer preserves both across re-runs; this flag opts into a clean-slate
# reset without a full uninstall. The spoke then reconnects with a NEW UUID +
# no pre-shared secret → it lands in "pending admin approval" on the hub (pass
# --secret alongside to keep it authenticated).
PURGE_ENV=0
# --clone: prep this box as a golden image. Full install (packages, venv, deps,
# DHCP/agent-listener infra, unit) but DO NOT mint identity or start the spoke —
# strip SPOKE_SECRET/INSTALL_UUID and leave SPOKE_ID hostname-derived, then
# enable (not start) the unit. On the NEXT boot of each clone the spoke mints a
# fresh INSTALL_UUID, derives its id from the (renamed) hostname, and connects.
CLONE_MODE=0
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub)                HUB_URL="$2"; HUB_URL_PINNED=1; shift ;;
        --id|--name)          SPOKE_ID="$2"; SPOKE_ID_PINNED=1; shift ;;
        --secret)             SPOKE_SECRET="$2"; shift ;;
        --hub-secret)         HUB_SECRET="$2";   shift ;;
        --dhcp-iface)         DHCP_IFACE="$2";   shift ;;
        --no-dhcp)            DHCP_SKIP=1 ;;
        --tls-verify)         TLS_VERIFY=true ;;
        --tls-ca-cert)        shift; TLS_CA_CERT="$1" ;;
        --agent-listener)     CS_AGENT_LISTENER=1 ;;  # default already; kept as a harmless no-op
        --no-agent-listener)  CS_AGENT_LISTENER=0 ;;
        --infra-only)         INFRA_ONLY=1 ;;
        --purge-env|--reset-identity) PURGE_ENV=1 ;;
        --clone|--prep-clone) CLONE_MODE=1 ;;
        --admin-token) ;; # deprecated
        --all-prereqs) ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

# --clone forces a hostname-derived id (a pinned --id would freeze every clone
# to the template's name); identity is minted on first boot, not now.
if [ "$CLONE_MODE" = "1" ]; then
    if [ "$SPOKE_ID_PINNED" = "1" ]; then
        echo "⚠️  --clone ignores --id ($SPOKE_ID): each clone derives its id from its own hostname."
    fi
    SPOKE_ID=""; SPOKE_ID_PINNED=0
fi

# Accept a bare hub IP/host for --hub (e.g. `--hub 172.16.1.31` == `--hub
# wss://172.16.1.31:443`). A ws://|wss:// scheme or the "auto" sentinel is left
# as-is; host:port gets a scheme; a bare host defaults to the unified :443.
if [ -n "${HUB_URL:-}" ] && [ "$HUB_URL" != "auto" ]; then
    case "$HUB_URL" in
        ws://*|wss://*) : ;;
        *:[0-9]*)       HUB_URL="wss://${HUB_URL}" ;;
        *)              HUB_URL="wss://${HUB_URL}:443" ;;
    esac
fi

if $TLS_VERIFY && [ -z "$TLS_CA_CERT" ]; then
    echo "❌ --tls-verify requires --tls-ca-cert <path> on a standalone spoke (the hub CA cert is not on this box)."
    exit 1
fi
if $TLS_VERIFY; then
    HUB_TLS_VERIFY_ENV=1
    HUB_TLS_CA_ENV="$TLS_CA_CERT"
else
    HUB_TLS_VERIFY_ENV=0
    HUB_TLS_CA_ENV=""
fi

[ "$(id -u)" -eq 0 ] || { echo "❌ Must be run as root."; exit 1; }

# A successful install chowns $LM_DIR (the whole lm checkout, incl. .git + cs/)
# to $SVC_USER (see chown -R near the end of this script), but every run —
# including re-runs/updates — executes entirely as root. Root then running
# `git pull`/`git clone` against a directory owned by a different user trips
# git's dubious-ownership safety check (CVE-2022-24765 mitigation):
# "fatal: detected dubious ownership in repository at ...". Whitelist the
# git-managed paths for root's git config up front so clone/pull always work
# regardless of who currently owns them. $LM_DIR covers the new all-in-one
# lm checkout (.git at /opt/lm); $LM_DIR/cs is the separate cs clone.
git config --global --add safe.directory "$LM_DIR" 2>/dev/null || true
git config --global --add safe.directory "$LM_DIR/cs" 2>/dev/null || true
git config --global --add safe.directory "$LM_DIR/core" 2>/dev/null || true

# ── Local logging ─────────────────────────────────────────────────────────────
# The lm-cs service unit below logs to /var/log/lm/lm-cs.log, but systemd
# ``append:`` does NOT create parent dirs — so on a fresh box the spoke ran with
# its logs silently dropped and a failed/pending install left nothing to read.
# Create the dir up front and mirror the entire install to an install log so a
# silent or half-finished run is debuggable without re-running. (Mirrors
# lm/install_all.sh + agent/install_agent.sh, which both do this.)
mkdir -p /var/log/lm
chmod 755 /var/log/lm

# Circular logging: cap /var/log/lm/*.log (+ legacy client-sim logs) so they
# can't fill the disk. copytruncate keeps the same inode so the running spoke's
# FileHandler + systemd StandardError=append: writers keep appending (both
# O_APPEND → no sparse files). Belt-and-suspenders alongside the app's
# RotatingFileHandler (LM_LOG_MAX_BYTES) in logging_setup.py.
cat > /etc/logrotate.d/lm <<'LOGROTATE'
/var/log/lm/*.log /var/log/client-sim-*.log {
    su root root
    size 50M
    rotate 5
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
LOGROTATE

INSTALL_LOG="/var/log/lm/lm-cs-install.log"
exec > >(tee -a "$INSTALL_LOG") 2>&1
echo "══ $(date -u '+%Y-%m-%dT%H:%M:%SZ') install_cs.sh start ══"
echo "  args: $*"
echo "  log:  $INSTALL_LOG"

GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✅  $*${NC}"; }
warn() { echo -e "${YLW}⚠️   $*${NC}"; }
step() { echo -e "\n${GRN}━━  $*  ━━${NC}"; }

# ══════════════════════════════════════════════════════════════════════════════
# OS/host-level simulation infrastructure — shared by a full install AND the
# --infra-only path the generic agent uses when it hosts the CS role in-process.
# Defined up front (before the --infra-only early exit) but the full-install
# flow still CALLS them at their original positions further down, so a normal
# `install_cs.sh` run is byte-for-byte unchanged. Both functions run in the
# current shell (no `local`), so setup_sim_agent_listener's CS_AGENT_LISTENER_*
# / cert-path variables still escape to the full-install .env writer below.
# ══════════════════════════════════════════════════════════════════════════════

# ── DHCP on the second NIC (sim-client network) — cs-owned Kea instance ────────
# Auto-detect a second NIC and run a cs-OWNED Kea DHCP4 instance (kea-dhcp4-sim,
# SEPARATE from the lm/dhcp module's Kea) serving 169.253.1.0/24 on it for the
# isolated simulation-client network. Same subnet/pool/lease behaviour the prior
# dnsmasq scope had (no router option). Skipped on single-NIC hosts or when
# --no-dhcp / DHCP_SKIP=1.
#
# Distinct-from-the-dhcp-module names so both Kea instances coexist on one box:
#   config  /etc/kea/kea-dhcp4-sim.conf + /etc/kea/kea-ctrl-agent-sim.conf
#   units   kea-dhcp4-sim.service + kea-ctrl-agent-sim.service
#   socket  /run/kea/kea4-ctrl-socket-sim   ctrl-agent http-port 8002 (dhcp=8001)
#   leases  /var/lib/kea/kea-leases4-sim.csv
setup_sim_dhcp() {
if [[ "$DHCP_SKIP" != "1" ]]; then
    step "Detecting DHCP interface (sim-client network)"

    # Build the list of ethernet interfaces excluding loopback. Strip the "@…"
    # veth-pair suffix so "eth0@if5" still detects as eth0. The trailing
    # `|| true` keeps `set -euo pipefail` happy when grep has no matches.
    mapfile -t IFACES < <(
        ip -o link show \
            | awk -F': ' '{print $2}' \
            | sed 's/@.*//' \
            | grep -v '^lo$' \
            || true
    )
    NIC_COUNT=${#IFACES[@]}
    echo "   Found ${NIC_COUNT} interface(s): ${IFACES[*]}"

    # Honour an explicit DHCP_IFACE; otherwise use the 2nd NIC, or skip.
    if [[ -z "$DHCP_IFACE" ]]; then
        # Prefer the NIC with NO IPv4 address: the uplink always has one, the
        # sim NIC only gets 169.253.1.1 from us, so before that it is bare.
        # Index-based detection ("the second one") depends on enumeration order
        # surviving a rebuild -- a renamed/reordered NIC silently points Kea at
        # an interface that no longer exists and it serves nobody. Fall back to
        # index 1 (the historical rule) when every NIC already has an address,
        # which is the case on a re-run where we assigned the sim IP earlier.
        _bare=""
        for _n in "${IFACES[@]}"; do
            if ! ip -o -4 addr show dev "$_n" 2>/dev/null | grep -q .; then
                _bare="$_n"; break
            fi
        done
        if [[ -n "$_bare" ]]; then
            DHCP_IFACE="$_bare"
            ok "Sim NIC detected (no IPv4 address): ${DHCP_IFACE} — DHCP will be configured"
        elif (( NIC_COUNT >= 2 )); then
            DHCP_IFACE="${IFACES[1]}"
            ok "Second NIC detected: ${DHCP_IFACE} — DHCP will be configured"
        else
            warn "Only one NIC found — DHCP setup skipped"
        fi
    fi

    if [[ -n "$DHCP_IFACE" ]]; then
        echo "   Configuring DHCP on ${DHCP_IFACE} (${DHCP_GATEWAY}/${DHCP_PREFIX})"
        # Sim DHCP is OPTIONAL (single-NIC hosts skip it). A kea apt failure must
        # NOT abort the whole cs spoke install under `set -e` — bail the DHCP
        # branch and continue so the spoke unit still gets written/started.
        # 'acl' ships setfacl, used below to give the spoke a durable read on the
        # sim lease DB (which Kea owns as root and recreates periodically).
        if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -q kea-dhcp4-server kea-ctrl-agent acl \
                -o Dpkg::Options::="--force-confdef" \
                -o Dpkg::Options::="--force-confold"; then
            warn "kea install failed — skipping sim DHCP (cs spoke will still install)"
            return 0
        fi
        ok "Kea (kea-dhcp4-server + kea-ctrl-agent) installed"

        # ── AppArmor: allow the SIM instance's runtime files ──────────────────
        # Kea derives its PID file name from the CONFIG file name, so our
        # deliberately-renamed sim instance writes
        #   /run/kea/kea-dhcp4-sim.kea-dhcp4.pid
        #   /run/kea/kea-ctrl-agent-sim.kea-ctrl-agent.pid
        # while Debian's stock profiles only permit the packaged
        # kea-dhcp4.kea-dhcp4.pid names. The result is a DENIED on create — as
        # ROOT, so it looks nothing like a permissions problem (ls shows
        # /run/kea root-owned and writable) and the unit just crash-loops with
        # "Unable to open PID file ... for write" plus a logger_lockfile denial.
        # Only the sim instance is affected; the dhcp module's Kea keeps working,
        # which makes it look like the sim config is at fault when it is valid.
        # 'k' = lock permission, required for the interprocess logger_lockfile
        # (denied separately from the PID file).
        # Make complain mode SURVIVE A REBOOT, by writing the flag into the
        # POLICY TEXT rather than relying on the loader.
        #
        # Two weaker approaches already failed on this fleet:
        #   1. `apparmor_parser -C -r` alone — RUNTIME only. At boot AppArmor
        #      reloads from /etc/apparmor.d/ in ENFORCE, the packaged profile's
        #      explicit deny applies again (an explicit deny beats any later
        #      allow and cannot be overridden from local/), and every spoke lost
        #      DHCP simultaneously at the first reboot.
        #   2. an /etc/apparmor.d/force-complain/<profile> symlink — whether that
        #      directory is honoured depends on the AppArmor version and on
        #      whether the systemd unit or the SysV functions load policy. It was
        #      NOT honoured here: the fleet died again on the next reboot.
        #
        # `flags=(complain)` in the profile header is part of the policy itself,
        # so any loader, at any boot, brings the profile up in complain. This is
        # exactly what `aa-complain` does; apparmor-utils is not installed on
        # these spokes, so we edit the header directly. A .lm-bak copy is kept so
        # the change is reversible, and the symlink is still created as
        # belt-and-braces where the loader does honour it.
        #
        # NOTE: these are dpkg conffiles, so a kea package upgrade may prompt
        # about the local modification. Keeping the local version preserves DHCP.
        _kea_force_complain() {
            mkdir -p /etc/apparmor.d/force-complain
            for _cp in usr.sbin.kea-dhcp4 usr.sbin.kea-ctrl-agent usr.sbin.kea-lfc; do
                _cf="/etc/apparmor.d/$_cp"
                [ -f "$_cf" ] || continue
                ln -sf "$_cf" "/etc/apparmor.d/force-complain/$_cp" 2>/dev/null || true
                [ -f "${_cf}.lm-bak" ] || cp -a "$_cf" "${_cf}.lm-bak" 2>/dev/null || true
                python3 - "$_cf" <<'PYFLAG' || warn "AppArmor: could not set complain flag in $_cf"
import re, sys
p = sys.argv[1]
s = open(p).read()
# The profile header is the first non-comment line ending in '{'. Forms seen:
#   /usr/sbin/kea-dhcp4 {
#   /usr/sbin/kea-dhcp4 flags=(attach_disconnected) {
#   profile kea-dhcp4 /usr/sbin/kea-dhcp4 flags=(...) {
out, done = [], False
for line in s.splitlines(True):
    if not done:
        t = line.strip()
        if t and not t.startswith(('#', 'include', 'abi')) and t.endswith('{'):
            if 'complain' in line:
                done = True                      # already complain — leave it alone
            elif 'flags=(' in line:
                line = re.sub(r'flags=\(([^)]*)\)',
                              lambda m: 'flags=(%s,complain)' % m.group(1).strip(),
                              line, count=1)
                done = True
            else:
                line = line.rstrip()[:-1].rstrip() + ' flags=(complain) {\n'
                done = True
    out.append(line)
if done:
    open(p, 'w').write(''.join(out))
sys.exit(0 if done else 1)
PYFLAG
            done
            # Reload PLAINLY now: the flag lives in the file, so no -C needed and
            # the running mode matches what the next boot will produce.
            for _cp in usr.sbin.kea-dhcp4 usr.sbin.kea-ctrl-agent usr.sbin.kea-lfc; do
                [ -f "/etc/apparmor.d/$_cp" ] || continue
                apparmor_parser -r "/etc/apparmor.d/$_cp" 2>/dev/null || true
            done
            systemctl restart kea-dhcp4-sim kea-ctrl-agent-sim >/dev/null 2>&1 || true
        }

        _aa_reloaded=0
        # kea-lfc too: the Lease File Cleanup helper runs hourly against the
        # SAME renamed lease DB, so it hits the identical denial --
        #   profile="kea-lfc" name="/var/lib/kea/kea-leases4-sim.csv"     (w)
        #   profile="kea-lfc" name="/var/lib/kea/kea-leases4-sim.csv.pid" (c)
        # It does not stop DHCP, which is why it went unnoticed: leases keep
        # being handed out while cleanup silently fails and the CSV grows
        # without bound. Same rule set covers it (the .pid lives under
        # /var/lib/kea/**).
        for _p in usr.sbin.kea-dhcp4 usr.sbin.kea-ctrl-agent usr.sbin.kea-lfc; do
            [ -f "/etc/apparmor.d/$_p" ] || continue
            mkdir -p /etc/apparmor.d/local
            # BOTH runtime dirs. /run/kea covers the PID + logger lockfile;
            # /var/lib/kea covers the memfile LEASE DB. Kea derives both names
            # from the config file name, so the sim instance wants
            # kea-leases4-sim.csv where the stock profile only allows the
            # packaged kea-leases4.csv. Granting only /run/kea gets the daemon
            # past start-up and then it dies one step later on
            # DHCPSRV_MEMFILE_FAILED_TO_OPEN -- looks like a different bug, same
            # cause.
            # Append only the rules that are missing, so a box already carrying
            # an earlier partial grant is topped up instead of accumulating a
            # duplicate copy of the rules it already had.
            touch "/etc/apparmor.d/local/$_p"
            while IFS= read -r _rule; do
                [ -n "$_rule" ] || continue
                grep -qxF "$_rule" "/etc/apparmor.d/local/$_p" 2>/dev/null \
                    || printf '%s\n' "$_rule" >> "/etc/apparmor.d/local/$_p"
            done <<'AARULES'
  /run/kea/ rw,
  /run/kea/** rwk,
  /var/lib/kea/ rw,
  /var/lib/kea/** rwk,
AARULES
            # kea-lfc ALSO needs CAP_DAC_OVERRIDE. Our sim unit runs Kea as ROOT
            # (no User=), and kea-lfc creates its pid file and RENAMES the lease
            # DB (csv -> csv.1) across files whose ownership it does not match;
            # the stock profile's capability set omits dac_override, so AppArmor
            # denies it even though the process is root:
            #   profile="kea-lfc" capability=1 capname="dac_override"
            # The visible failures are LFC_FAIL_PID_CREATE and
            # DHCPSRV_MEMFILE_LFC_LEASE_FILE_RENAME_FAIL "Permission denied",
            # neither of which stops DHCP -- leases keep being served while
            # cleanup silently fails and the memfile grows without bound. That
            # is what made the health panel report more leases than the pool can
            # hold (it counts file ROWS, and an uncompacted memfile keeps every
            # renewal). Granted to kea-lfc ONLY, not the daemons.
            if [ "$_p" = "usr.sbin.kea-lfc" ]; then
                grep -qxF '  capability dac_override,' "/etc/apparmor.d/local/$_p" 2>/dev/null \
                    || printf '%s\n' '  capability dac_override,' >> "/etc/apparmor.d/local/$_p"
            fi
            # kea-ctrl-agent resolves names at start-up (getaddrinfo for its
            # listen address), which the stock profile does not permit: it is
            # denied /etc/resolv.conf, /etc/hosts, /etc/nsswitch.conf and an
            # inet dgram socket, and then cannot start. The nameservice
            # abstraction is the canonical grant for exactly that set — cleaner
            # and narrower than listing the files by hand. dhcp4 does not need
            # it (it binds an interface, not a name).
            if [ "$_p" = "usr.sbin.kea-ctrl-agent" ] \
               && ! grep -qF 'abstractions/nameservice' "/etc/apparmor.d/local/$_p" 2>/dev/null; then
                printf '  #include <abstractions/nameservice>\n' >> "/etc/apparmor.d/local/$_p"
            fi
            # Report the reload PER PROFILE and surface the parser's own error.
            # A single shared flag hid the case that actually bit: ctrl-agent
            # reloading fine while kea-dhcp4 did not, so the installer claimed
            # success while the DHCP server -- the one that matters for leases --
            # was still denied its lease file and crash-looping.
            # PRESERVE COMPLAIN MODE. A plain `apparmor_parser -r` forces the
            # profile back to ENFORCE, which knocks over a spoke that was
            # deliberately left in complain because the packaged profile's deny
            # cannot be overridden from local/. Observed: re-running the
            # installer on a healthy spoke took its DHCP down. Read the current
            # mode from securityfs (aa-status is not installed on these boxes)
            # and reload in the SAME mode.
            _prof="${_p#usr.sbin.}"
            _mode="$(sed -n "s/^${_prof} (\([a-z]*\)).*/\1/p" \
                     /sys/kernel/security/apparmor/profiles 2>/dev/null | head -1)"
            _aa_flag=""
            [ "$_mode" = "complain" ] && _aa_flag="-C"
            if command -v apparmor_parser >/dev/null 2>&1; then
                _aa_err="$(apparmor_parser $_aa_flag -r "/etc/apparmor.d/$_p" 2>&1 >/dev/null)" && {
                    ok "AppArmor: ${_p} reloaded${_mode:+ (kept in $_mode mode)}"
                    _aa_reloaded=1
                } || warn "AppArmor: ${_p} FAILED to reload${_aa_err:+ — $_aa_err}. Kea will keep being denied; fix and run: apparmor_parser -r /etc/apparmor.d/${_p}"
            fi
        done
        if [ "$_aa_reloaded" = "1" ]; then
            ok "AppArmor: granted the sim Kea instance its /run/kea + /var/lib/kea runtime files (see per-profile reload lines above)"
        elif [ -d /etc/apparmor.d ] && [ -f /etc/apparmor.d/usr.sbin.kea-dhcp4 ]; then
            warn "AppArmor profiles present but apparmor_parser did not reload — if kea-dhcp4-sim crash-loops with \"Unable to open PID file\", run: apparmor_parser -r /etc/apparmor.d/usr.sbin.kea-dhcp4"
        fi

        # ── Static IP on the internal interface ───────────────────────────────
        IFACE_CFG="/etc/network/interfaces.d/${DHCP_IFACE}.conf"
        mkdir -p /etc/network/interfaces.d
        NETMASK=$(python3 -c "import ipaddress; print(ipaddress.IPv4Network('${DHCP_SUBNET}/${DHCP_PREFIX}',False).netmask)")
        cat >"$IFACE_CFG" <<EOF
auto ${DHCP_IFACE}
iface ${DHCP_IFACE} inet static
    address ${DHCP_GATEWAY}
    netmask ${NETMASK}
EOF
        ok "Interface config written to ${IFACE_CFG}"

        # Bring the interface up (ignore errors if already up)
        ip link set "$DHCP_IFACE" up 2>/dev/null || true
        ip addr flush dev "$DHCP_IFACE" 2>/dev/null || true
        ip addr add "${DHCP_GATEWAY}/${DHCP_PREFIX}" dev "$DHCP_IFACE" 2>/dev/null || true
        ok "${DHCP_IFACE} configured with ${DHCP_GATEWAY}/${DHCP_PREFIX}"

        # ── rp_filter: loose mode so DestNat / port-forwarded traffic from
        #    other subnets isn't dropped by the kernel's strict reverse-path
        #    check. Without this a firewall DestNat pointing at this multi-homed
        #    host works from the same subnet but fails from others.
        sysctl -w net.ipv4.conf.all.rp_filter=2     >/dev/null 2>&1 || true
        sysctl -w net.ipv4.conf.default.rp_filter=2 >/dev/null 2>&1 || true
        cat >/etc/sysctl.d/10-client-sim.conf <<'SYSCTL'
# Loose reverse-path filter — allows DestNat / port-forward traffic on
# multi-homed hosts (management NIC + sim-client DHCP NIC).
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
SYSCTL
        ok "rp_filter set to loose mode (DestNat-compatible)"

        # ── Convert the dnsmasq-style lease time (e.g. 1h/30m/3600) to the
        #    integer seconds Kea's valid-lifetime wants. Default 1h → 3600.
        LEASE_SECS=3600
        if [[ "$DHCP_LEASE_TIME" =~ ^([0-9]+)([smhd]?)$ ]]; then
            _n="${BASH_REMATCH[1]}"; _u="${BASH_REMATCH[2]}"
            case "$_u" in
                m) LEASE_SECS=$(( _n * 60 )) ;;
                h) LEASE_SECS=$(( _n * 3600 )) ;;
                d) LEASE_SECS=$(( _n * 86400 )) ;;
                *) LEASE_SECS="$_n" ;;   # 's' or bare number = seconds
            esac
        fi

        # Locate the kea binaries (systemd units need absolute ExecStart paths).
        KEA_DHCP4_BIN="$(command -v kea-dhcp4 2>/dev/null || echo /usr/sbin/kea-dhcp4)"
        KEA_CA_BIN="$(command -v kea-ctrl-agent 2>/dev/null || echo /usr/sbin/kea-ctrl-agent)"
        mkdir -p /etc/kea /run/kea /var/lib/kea

        # ── kea-dhcp4 config for the cs sim subnet — bound ONLY to the internal
        #    interface; one static subnet4 = 169.253.1.0/24. No routers option
        #    (parity with the prior dnsmasq "no default gateway": sim clients
        #    route through their own WiFi/USB adapter, so an injected gateway
        #    would break traffic generation). Memfile leases at the -sim CSV;
        #    control socket at the -sim path so it never clashes with the
        #    dhcp-module Kea. SEPARATE instance — do NOT touch the distro default
        #    kea-dhcp4-server.service (that belongs to the lm/dhcp module).
        KEA_DHCP4_CONF="/etc/kea/kea-dhcp4-sim.conf"
        cat >"$KEA_DHCP4_CONF" <<EOF
{
  "Dhcp4": {
    "interfaces-config": {
      "interfaces": [ "${DHCP_IFACE}" ]
    },
    "control-socket": {
      "socket-type": "unix",
      "socket-name": "/run/kea/kea4-ctrl-socket-sim"
    },
    "lease-database": {
      "type": "memfile",
      "persist": true,
      "name": "/var/lib/kea/kea-leases4-sim.csv"
    },
    "valid-lifetime": ${LEASE_SECS},
    "subnet4": [
      {
        "id": 1,
        "subnet": "${DHCP_SUBNET}/${DHCP_PREFIX}",
        "pools": [ { "pool": "${DHCP_RANGE_START} - ${DHCP_RANGE_END}" } ]
      }
    ],
    "loggers": [
      {
        "name": "kea-dhcp4",
        "output_options": [ { "output": "syslog" } ],
        "severity": "INFO"
      }
    ]
  }
}
EOF
        ok "kea-dhcp4-sim config written (169.253.1.0/24, no default gateway advertised)"

        # ── cs kea-ctrl-agent config — loopback only, port 8002 (the dhcp module
        #    uses 8001), control socket pointing at the -sim dhcp4 socket, no
        #    auth. Optional/nice-to-have: lets tooling read leases/health via the
        #    ctrl-agent RPC; dhcp_status reads the memfile CSV directly instead.
        KEA_CA_CONF="/etc/kea/kea-ctrl-agent-sim.conf"
        cat >"$KEA_CA_CONF" <<'EOF'
{
  "Control-agent": {
    "http-host": "127.0.0.1",
    "http-port": 8002,
    "control-sockets": {
      "dhcp4": {
        "socket-type": "unix",
        "socket-name": "/run/kea/kea4-ctrl-socket-sim"
      }
    },
    "loggers": [
      {
        "name": "kea-ctrl-agent",
        "output_options": [ { "output": "syslog" } ],
        "severity": "WARN"
      }
    ]
  }
}
EOF
        ok "kea-ctrl-agent-sim config written (127.0.0.1:8002 → -sim socket)"

        # The spoke runs as $SVC_USER (non-root) and reads the sim Kea config +
        # lease CSV every 10s for the Simulations "DHCP Server" card
        # (dhcp_status.collect_dhcp_status). The Kea package ships /etc/kea as
        # 0750 root:_kea, so svc_lm can't even traverse it — the collector then
        # logs "Permission denied: '/etc/kea/kea-dhcp4-sim.conf'" every telemetry
        # cycle. The sim config holds no secrets (a plain 169.253.1.0/24 pool),
        # so make /etc/kea traversable and both files world-readable (this is the
        # "only reads world-readable files" contract dhcp_status.py documents).
        chmod 0755 /etc/kea /var/lib/kea 2>/dev/null || true
        chmod 0644 "$KEA_DHCP4_CONF" "$KEA_CA_CONF" 2>/dev/null || true
        # Kea >= 2.6.3 (CVE-2025-32801/32802/32803 hardening) REJECTS a control-socket
        # dir more permissive than 0750 — RuntimeDirectory=kea defaults to 0755, which
        # makes kea-dhcp4-sim crash-loop on config-set: "socket path:/run/kea does
        # not exist or does not have permissions = 750". Force 0750 here so the
        # install-time `kea-dhcp4 -t` validation passes; the units below set
        # RuntimeDirectoryMode=0750 so the runtime dir is also 0750 after every start.
        chmod 0750 /run/kea 2>/dev/null || true

        # Fail loudly at install if the generated Kea config is invalid, instead of
        # discovering it later via a silently-inactive unit (kea-dhcp4 -t is the
        # same syntax check an operator would run).
        if ! "$KEA_DHCP4_BIN" -t "$KEA_DHCP4_CONF" >/tmp/kea-sim-check.log 2>&1; then
            warn "kea-dhcp4-sim config FAILED validation ($KEA_DHCP4_CONF):"
            sed 's/^/      /' /tmp/kea-sim-check.log 2>/dev/null || true
        fi

        # ── Custom systemd units (independent of the distro default kea units) ─
        # kea-dhcp4-sim waits for the DHCP interface to appear before it binds —
        # without this it fails with "no such interface" on reboot when the NIC
        # attachment isn't ready yet. The iface name is baked in at install time
        # (stable per host). RuntimeDirectory recreates /run/kea on boot.
        cat > /etc/systemd/system/kea-dhcp4-sim.service <<EOF
[Unit]
Description=CS sim-client Kea DHCP4 (isolated ${DHCP_SUBNET}/${DHCP_PREFIX})
After=network.target network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
RuntimeDirectory=kea
RuntimeDirectoryPreserve=yes
# Kea >= 2.6.3 requires the control-socket dir to be 0750 (CVE-2025-32801/32802/32803
# hardening); the default 0755 makes config-set fail ("permissions = 750") and the
# unit crash-loops. RuntimeDirectory recreates /run/kea at this mode every start.
RuntimeDirectoryMode=0750
# Bounded wait for the sim NIC, then force it up and idempotently re-apply the
# sim address (a reboot that did not process /etc/network/interfaces.d otherwise
# leaves the link down/unaddressed so Kea cannot bind). Never hard-fail here: on
# timeout exit 0 and let Kea + Restart=on-failure retry rather than parking the
# unit in 'failed' forever.
ExecStartPre=/bin/bash -c 'n=0; until ip link show "${DHCP_IFACE}" >/dev/null 2>&1; do n=\$((n+1)); [ \$n -ge 60 ] && break; sleep 1; done; ip link set "${DHCP_IFACE}" up 2>/dev/null || true; ip addr show dev "${DHCP_IFACE}" | grep -q "${DHCP_GATEWAY}/" || ip addr add "${DHCP_GATEWAY}/${DHCP_PREFIX}" dev "${DHCP_IFACE}" 2>/dev/null || true; exit 0'
ExecStart=${KEA_DHCP4_BIN} -c ${KEA_DHCP4_CONF}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

        cat > /etc/systemd/system/kea-ctrl-agent-sim.service <<EOF
[Unit]
Description=CS sim-client Kea Control Agent (127.0.0.1:8002)
After=network.target kea-dhcp4-sim.service
Wants=kea-dhcp4-sim.service

[Service]
Type=simple
RuntimeDirectory=kea
RuntimeDirectoryPreserve=yes
RuntimeDirectoryMode=0750
ExecStart=${KEA_CA_BIN} -c ${KEA_CA_CONF}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload >/dev/null 2>&1 || true
        ok "kea-dhcp4-sim + kea-ctrl-agent-sim systemd units written (wait for DHCP interface)"

        # Enable + (re)start ONLY the -sim units. Never enable the distro default
        # kea-dhcp4-server.service / kea-ctrl-agent.service — those are the
        # lm/dhcp module's. On a fresh install the packages may auto-enable the
        # defaults; leave them to the dhcp module (they bind :8001 / their own
        # socket and don't clash with the -sim instance's :8002 / -sim socket).
        systemctl enable --now kea-dhcp4-sim kea-ctrl-agent-sim >/dev/null 2>&1 || true
        systemctl restart kea-dhcp4-sim kea-ctrl-agent-sim >/dev/null 2>&1 || true

        # Let the spoke user READ diagnostics. The DHCP health panel runs as the
        # spoke (svc_lm) and could report "NOT serving" without being able to say
        # why: the journal needs the systemd-journal group, and the lease file is
        # 0640 _kea:_kea. Read-only group membership only -- nothing here grants
        # write access to Kea's data or lets the spoke change policy.
        if id -u "$SVC_USER" >/dev/null 2>&1; then
            usermod -aG systemd-journal "$SVC_USER" 2>/dev/null || true
            # NOTE: the _kea group does NOT get you the sim lease file. Our
            # kea-dhcp4-sim unit runs as ROOT (no User=), so Kea creates
            # kea-leases4-sim.csv as root:root 0640 — group _kea is irrelevant
            # to it. Only the PACKAGED instance's files are _kea:_kea. Kept
            # anyway so the packaged files are readable too, but the sim file
            # needs the explicit grant below.
            getent group _kea >/dev/null 2>&1 && usermod -aG _kea "$SVC_USER" 2>/dev/null || true

            # Durable read access to the SIM lease file for the health panel.
            # A one-off chgrp is not enough: Kea and the hourly LFC recreate the
            # file, and a fresh root:root 0640 drops any ownership we set. A
            # DEFAULT ACL on the directory is inherited by every file created in
            # it afterwards, so the grant survives recreation. Read-only, and
            # scoped to this one user.
            if command -v setfacl >/dev/null 2>&1; then
                setfacl -m   "u:${SVC_USER}:rx" /var/lib/kea 2>/dev/null || true
                setfacl -d -m "u:${SVC_USER}:r"  /var/lib/kea 2>/dev/null || true
                [ -f /var/lib/kea/kea-leases4-sim.csv ] && \
                    setfacl -m "u:${SVC_USER}:r" /var/lib/kea/kea-leases4-sim.csv 2>/dev/null || true
                ok "Diagnostics: ${SVC_USER} granted read on the sim lease DB (default ACL survives Kea recreating it)"
            else
                # No acl tooling: fall back to a group-readable file. This is
                # undone whenever Kea recreates the file, so say so rather than
                # implying it is permanent.
                [ -f /var/lib/kea/kea-leases4-sim.csv ] && \
                    chgrp _kea /var/lib/kea/kea-leases4-sim.csv 2>/dev/null && \
                    chmod 0640 /var/lib/kea/kea-leases4-sim.csv 2>/dev/null || true
                warn "Diagnostics: setfacl not installed — granted lease-DB read via group only; it reverts when Kea recreates the file. Install 'acl' for a durable grant."
            fi
        fi

        # ── Verify it actually SERVES, and fall back to complain mode ─────────
        # The local-include grant above is not always sufficient: Debian's kea
        # profile can carry an explicit `deny`, and in AppArmor an explicit deny
        # beats any later allow and CANNOT be overridden from
        # /etc/apparmor.d/local. The result is the worst kind of failure --
        # correct-looking rules, a clean apparmor_parser reload, and a daemon
        # that still cannot open its lease DB, with the denials often invisible
        # because the kernel rate-limits audit messages when the ctrl-agent is
        # also being denied several times a second. Proven on a live spoke: the
        # unit only started once the profile was unloaded.
        #
        # So: don't trust the grant, TEST it. If kea-dhcp4-sim will not become
        # active, drop the profile to complain (permits + logs, still confined
        # for observability) rather than leaving a DHCP server that hands out
        # nothing. Loud about it, because this is a policy relaxation.
        _kea_ok=""
        for _i in $(seq 1 12); do
            if systemctl is-active --quiet kea-dhcp4-sim; then _kea_ok=1; break; fi
            sleep 1
        done
        if [ -z "$_kea_ok" ] && command -v apparmor_parser >/dev/null 2>&1 \
           && [ -f /etc/apparmor.d/usr.sbin.kea-dhcp4 ]; then
            warn "kea-dhcp4-sim did not start with the AppArmor grant applied — falling back to complain mode for the kea profiles (permits + logs; the packaged profile likely carries a deny that a local include cannot override)."
            # PERSIST the complain mode. `apparmor_parser -C -r` changes only the
            # RUNNING profile: on the next boot AppArmor reloads every profile
            # from /etc/apparmor.d/ in ENFORCE, the grant is denied again, and
            # kea-dhcp4 crash-loops with "Unable to open database: unable to open
            # /var/lib/kea/kea-leases4-sim.csv". That is exactly what happened —
            # a whole fleet ran fine for days on runtime-only complain mode and
            # every spoke lost DHCP simultaneously at the first reboot, ~4300
            # restarts each, with a valid config and a perfectly readable lease
            # file. A fallback that silently expires at reboot is worse than no
            # fallback, because nothing reports it until the clients are dark.
            #
            # /etc/apparmor.d/force-complain/<profile> is the supported way to
            # make it stick: the AppArmor init unit honours these symlinks when
            # it loads policy at boot. Remove the symlink + `apparmor_parser -r`
            # to go back to enforce once the profile is fixed properly.
            _kea_force_complain
            for _i in $(seq 1 12); do
                if systemctl is-active --quiet kea-dhcp4-sim; then _kea_ok=1; break; fi
                sleep 1
            done
        fi
        # Reboot-survival check, independent of how we got here: if any kea
        # profile is complain ONLY at runtime, say so loudly — that box is one
        # reboot away from losing DHCP.
        _aa_transient=""
        for _cp in usr.sbin.kea-dhcp4 usr.sbin.kea-ctrl-agent usr.sbin.kea-lfc; do
            [ -f "/etc/apparmor.d/$_cp" ] || continue
            _m="$(sed -n "s/^[[:space:]]*profile[[:space:]].*flags=(\(.*\)).*/\1/p" \
                    "/etc/apparmor.d/$_cp" 2>/dev/null | head -1)"
            if aa-status 2>/dev/null | grep -q "^ *${_cp##usr.sbin.} (complain)" \
               || echo "$_m" | grep -q complain; then
                [ -L "/etc/apparmor.d/force-complain/$_cp" ] || _aa_transient="${_aa_transient} $_cp"
            fi
        done
        if [ -n "$_aa_transient" ]; then
            _kea_force_complain
            warn "AppArmor: made complain mode PERSISTENT for:${_aa_transient} (was runtime-only — it would have reverted to enforce at the next reboot and taken sim DHCP down)."
        fi
        if [ -n "$_kea_ok" ]; then
            ok "Sim DHCP serving: kea-dhcp4-sim active on ${DHCP_IFACE} (${DHCP_SUBNET}/${DHCP_PREFIX}, pool ${DHCP_RANGE_START}-${DHCP_RANGE_END})"
        else
            warn "Sim DHCP is NOT serving — kea-dhcp4-sim will not stay active. Clients on the sim network will get no address. Check: journalctl -u kea-dhcp4-sim -n 30; dmesg | grep -i 'apparmor.*kea'"
        fi
        if systemctl is-active --quiet kea-dhcp4-sim; then
            ok "kea-dhcp4-sim running — DHCP active on ${DHCP_IFACE}"
        else
            warn "kea-dhcp4-sim failed to start on ${DHCP_IFACE} — recent log:"
            journalctl -u kea-dhcp4-sim -n 20 --no-pager 2>/dev/null | sed 's/^/      /' || true
            warn "diagnose: systemctl status kea-dhcp4-sim ; ${KEA_DHCP4_BIN} -t ${KEA_DHCP4_CONF} ; ip -br link"
        fi
    fi
fi
}

# ── Agent listener prerequisites (split topology, --agent-listener opt-in) ────
# Mirrors install_pxmx.sh's standalone TLS-cert generation: a pxmx host agent
# dialing THIS cs spoke's /ws/agent needs wss on :443, so a self-signed cert is
# generated (once; preserved on re-run) and the agent_secret is written to
# /etc/lm-cs-agent/config.json. Skipped entirely when the listener is off — a
# relay-only cs spoke never touches any of this. Sets CS_AGENT_LISTENER_LINES
# (consumed by the full-install .env writer; harmless/ignored under --infra-only).
setup_sim_agent_listener() {
CS_AGENT_LISTENER_LINES=""
if [ "$CS_AGENT_LISTENER" = "1" ]; then
    CS_CERT_DIR="$LM_DIR/cs/certs"
    CS_CERT="$CS_CERT_DIR/hub.crt"
    CS_KEY="$CS_CERT_DIR/hub.key"
    mkdir -p "$CS_CERT_DIR"
    if ! command -v openssl >/dev/null 2>&1; then
        warn "openssl not found — skipping cs agent-listener TLS cert (listener stays plaintext :8767)."
    elif [ -f "$CS_CERT" ] && [ -f "$CS_KEY" ]; then
        ok "cs agent-listener TLS cert already present at $CS_CERT — preserving."
    else
        echo "🔒 Generating self-signed cs agent-listener TLS cert at $CS_CERT…"
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout "$CS_KEY" -out "$CS_CERT" -days 3650 \
            -subj "/CN=lm-cs" -addext "subjectAltName=IP:127.0.0.1,DNS:lm-hub,DNS:lm-hub.local" \
            >/dev/null 2>&1 || warn "openssl cert generation failed — agent listener stays plaintext."
    fi
    if [ -f "$CS_KEY" ]; then
        chmod 600 "$CS_KEY"
        chown "$SVC_USER:$SVC_USER" "$CS_KEY" "$CS_CERT" 2>/dev/null || true
    fi
    CS_AGENT_LISTENER_LINES="LM_CS_AGENT_LISTENER=1"
    if [ -f "$CS_CERT" ] && [ -f "$CS_KEY" ]; then
        CS_AGENT_LISTENER_LINES="$CS_AGENT_LISTENER_LINES
LM_TLS_CERT=$CS_CERT
LM_TLS_KEY=$CS_KEY"
    fi

    # --- Agent Secret (shared with a pxmx host agent dialing THIS cs spoke) ---
    # Mirrors install_pxmx.sh's AGENT_CONFIG block exactly. Without this,
    # AgentHostingControlPlane.agent_secret stays None, so
    # approve_pending_agent() pushes {"secret": null} to an approved agent —
    # the agent's "if provisioned_secret:" guard then skips saving it and
    # reconnects with the SAME empty secret, landing right back in
    # pending/"needs admin approval" forever. Preserve an existing secret so
    # a re-install doesn't break an already-approved agent.
    CS_AGENT_CONFIG="/etc/lm-cs-agent/config.json"
    EXISTING_CS_AGENT_SECRET=""
    if [ -f "$CS_AGENT_CONFIG" ]; then
        EXISTING_CS_AGENT_SECRET=$(python3 -c "import json,sys; d=json.load(open('$CS_AGENT_CONFIG')); print(d.get('agent_secret',''))" 2>/dev/null || true)
    fi

    if [ -z "$EXISTING_CS_AGENT_SECRET" ]; then
        if command -v openssl >/dev/null 2>&1; then
            CS_AGENT_SECRET=$(openssl rand -base64 32 | tr -d '/+=\n')
        else
            CS_AGENT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        fi
        echo "🔑 Generated new cs agent_secret."
    else
        CS_AGENT_SECRET="$EXISTING_CS_AGENT_SECRET"
        echo "🔑 Preserved existing cs agent_secret."
    fi

    mkdir -p /etc/lm-cs-agent
    python3 -c "
import json, sys
path = '$CS_AGENT_CONFIG'
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data['agent_secret'] = '$CS_AGENT_SECRET'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
    chmod 600 "$CS_AGENT_CONFIG"
    chown "$SVC_USER:$SVC_USER" "$CS_AGENT_CONFIG" 2>/dev/null || true
    echo "✅ cs agent secret written to $CS_AGENT_CONFIG"
fi
}

# ── --infra-only fast path (generic-agent-hosted CS role) ─────────────────────
# Lay down ONLY the host-level sim infra, then exit — no spoke clone, no venv, no
# lm core, no spoke .env, no lm-cs unit, no rollback watchdog/sudoers. The agent
# has already cloned this repo to $LM_DIR/cs and owns the process + updates + the
# role's runtime config (pushed from the hub). Idempotent + non-interactive.
if [ "$INFRA_ONLY" = "1" ]; then
    step "CS simulation infra-only setup (generic-agent-hosted role)"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 openssl
    ok "Base packages ready"
    setup_sim_dhcp
    setup_sim_agent_listener
    echo ""
    ok "CS simulation infra-only setup complete."
    echo "  DHCP:           $( [ -n "${DHCP_IFACE:-}" ] && [ "$DHCP_SKIP" != "1" ] && echo "cs-owned Kea (kea-dhcp4-sim) on ${DHCP_IFACE}" || echo "skipped (single NIC or --no-dhcp)" )"
    echo "  Agent listener: cert $LM_DIR/cs/certs/hub.crt + /etc/lm-cs-agent/config.json"
    echo "                  The root agent binds :443 directly (no CAP_NET_BIND_SERVICE / systemd unit created here)."
    echo "  Install log:    $INSTALL_LOG"
    exit 0
fi

step "Lab Manager — Generic Agent Installer"

# ── System packages ───────────────────────────────────────────────────────────
step "Installing system packages"
apt-get update -qq
# Full prerequisite set: python runtime+venv+pip, git/curl for clone+discovery,
# jq for JSON, sudo for the update watchdog, ca-certificates for TLS (git/pip/
# wss), iproute2 for 2nd-NIC detection, openssl for the agent-listener cert.
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3 python3-venv python3-pip git curl jq sudo ca-certificates iproute2 openssl
ok "Packages ready"

# ── DHCP on the second NIC (sim-client network) ───────────────────────────────
# (Implementation lifted into setup_sim_dhcp() near the top so --infra-only can
# reuse it; called here for the full install exactly where it used to run.)
setup_sim_dhcp

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SVC_USER" &>/dev/null; then
    useradd -r -s /bin/false -M "$SVC_USER"
    ok "Created service user $SVC_USER"
fi

# ── Optional identity reset (--purge-env) ────────────────────────────────────
# Delete the existing .env BEFORE the preserve-on-re-run reads below, so the
# spoke secret + INSTALL_UUID are regenerated fresh instead of being carried
# forward. Done here (not at arg-parse) so $LM_DIR is set and it lands right
# before the reads it must precede.
if [ "$PURGE_ENV" = "1" ]; then
    # Capture the id this box has been registering under BEFORE .env goes, so
    # the per-identity state dir below can be found. SPOKE_ID is only baked into
    # .env when it was explicitly pinned; otherwise the spoke derives the bare
    # hostname at startup (lm-spoke/src/control_plane.py), which is what we fall
    # back to.
    _old_id=""
    if [ -f "$LM_DIR/cs/.env" ] && grep -q '^SPOKE_ID=' "$LM_DIR/cs/.env" 2>/dev/null; then
        _old_id="$(grep '^SPOKE_ID=' "$LM_DIR/cs/.env" | head -1 | cut -d= -f2-)"
    fi
    [ -n "$_old_id" ] || _old_id="$(hostname)"

    if [ -f "$LM_DIR/cs/.env" ]; then
        rm -f "$LM_DIR/cs/.env"
        warn "--purge-env: removed $LM_DIR/cs/.env — secret + INSTALL_UUID will be regenerated (spoke re-registers on the hub → needs approval)."
    fi

    # mTLS CLIENT identity. A clone carries the ORIGIN's client cert and
    # presents it to the hub ("wss: presenting hub-CA mTLS client cert"), so a
    # reset that leaves it behind hands the new box the old box's credential.
    # Same file set core clears in its own reset path (control_plane.py:2014):
    # the current mtls-hub-client.* plus the legacy mtls-client.* names. The CA
    # and the agent-listener server cert (hub.crt) are deliberately NOT touched
    # — the CA is shared trust, not identity, and both are re-pushed by the hub.
    _mtls_cleared=0
    for _f in mtls-hub-client.crt mtls-hub-client.key mtls-client.crt mtls-client.key; do
        if [ -f "$LM_DIR/cs/certs/$_f" ]; then
            rm -f "$LM_DIR/cs/certs/$_f" && _mtls_cleared=1
        fi
    done
    [ "$_mtls_cleared" = "1" ] && \
        warn "--purge-env: cleared the mTLS client cert/key — the hub re-provisions one for the new identity."

    # Per-identity runtime/recovery state (update snapshots, rollback markers).
    # Keyed by the OLD spoke id, so it is orphaned the moment the id changes and
    # accumulates one directory per rebuild.
    if [ -n "$_old_id" ] && [ -d "/var/lib/lm/$_old_id" ]; then
        rm -rf "/var/lib/lm/$_old_id"
        warn "--purge-env: removed /var/lib/lm/$_old_id (recovery state for the discarded identity)."
    fi

    # SSH host keys — the same clone problem as machine-id: a Proxmox clone
    # copies /etc/ssh/ssh_host_* verbatim, so every clone presents an identical
    # SSH identity and any one of them can impersonate its siblings.
    if [ -d /etc/ssh ] && ls /etc/ssh/ssh_host_* >/dev/null 2>&1; then
        rm -f /etc/ssh/ssh_host_*
        if command -v ssh-keygen >/dev/null 2>&1 && ssh-keygen -A >/dev/null 2>&1; then
            # try-restart: a no-op when sshd isn't running, and it does NOT drop
            # established sessions (existing connections are separate processes).
            systemctl try-restart ssh 2>/dev/null || systemctl try-restart sshd 2>/dev/null || true
            warn "--purge-env: regenerated SSH host keys — clients that connected before will report REMOTE HOST IDENTIFICATION HAS CHANGED. Clear the old entry with: ssh-keygen -R <this-host>"
        else
            warn "--purge-env: removed SSH host keys but could NOT regenerate them (no ssh-keygen) — run 'ssh-keygen -A' before relying on SSH to this box."
        fi
    fi

    # A fresh .env alone does NOT make a clone distinct. core's
    # _ensure_install_uuid() binds INSTALL_UUID to a machine fingerprint whose
    # FIRST source is /etc/machine-id (control_plane.py:_machine_fingerprint),
    # and a Proxmox clone copies that file byte-for-byte — cloud-init would
    # regenerate it, a plain clone does not. Two clones therefore read the SAME
    # fingerprint, the clone-detection concludes "same machine", the copied
    # identity + HUB_SECRET are kept, and both boxes flap over one hub
    # connection (hub logs "[identity] CLONE COLLISION"). Regenerating it here
    # is what makes --purge-env a complete identity reset rather than a
    # half one that silently re-collides on the next clone.
    _old_mid="$(cat /etc/machine-id 2>/dev/null || true)"
    : > /etc/machine-id 2>/dev/null || true
    rm -f /var/lib/dbus/machine-id 2>/dev/null || true
    if command -v systemd-machine-id-setup >/dev/null 2>&1; then
        systemd-machine-id-setup >/dev/null 2>&1 || true
    fi
    # Fallback for a box whose systemd helper is absent or no-opped.
    if [ ! -s /etc/machine-id ]; then
        od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n' > /etc/machine-id 2>/dev/null || true
        echo >> /etc/machine-id 2>/dev/null || true
    fi
    # dbus keeps its own copy (usually a symlink to the same file); resync it so
    # the fingerprint's 2nd source can't hand back the ORIGIN's stale id.
    if [ -s /etc/machine-id ] && [ -d /var/lib/dbus ]; then
        cp -f /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
    fi
    _new_mid="$(cat /etc/machine-id 2>/dev/null || true)"
    if [ -n "$_new_mid" ] && [ "$_new_mid" != "$_old_mid" ]; then
        warn "--purge-env: regenerated /etc/machine-id (${_old_mid:0:8}… → ${_new_mid:0:8}…) — this box can no longer share a clone fingerprint with its origin."
    else
        warn "--purge-env: could NOT regenerate /etc/machine-id — if this box was cloned it may still collide with its origin (hub: '[identity] CLONE COLLISION'). Fix by hand: truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id && systemd-machine-id-setup"
    fi

    # A clean identity does NOT help if this box runs the spoke TWICE. Both
    # processes derive the same id, so they take turns evicting each other on
    # the hub and rotate each other's secret (symptom: connect → clean close →
    # reconnect every ~60-130s, alternating secret=yes/secret=no). A purge
    # cannot detect that from .env, so look for the second unit directly.
    _dupes=""
    for _u in lm-cs lm-agent lm-generic-agent; do
        systemctl is-enabled "$_u" >/dev/null 2>&1 && _dupes="$_dupes $_u"
    done
    _nproc="$(pgrep -fc 'control_plane' 2>/dev/null || echo 0)"
    case "$_dupes" in
        *lm-cs*lm-agent*|*lm-agent*lm-cs*)
            warn "--purge-env: BOTH${_dupes} are enabled on this box. If the agent hosts the 'simulation' role you now have TWO cs spokes sharing one id — they will flap against each other regardless of identity. Keep ONE: 'systemctl disable --now lm-generic-agent' for the legacy agent, or drop the simulation role from lm-agent." ;;
    esac
    if [ "${_nproc:-0}" -gt 1 ] 2>/dev/null; then
        warn "--purge-env: ${_nproc} control_plane processes are running — expected 1. Check 'pgrep -af control_plane'; a duplicate will flap against this spoke on the hub."
    fi

    # Say what this box will register AS. "I replaced a server with the same
    # name" is invisible until two boxes collide on the hub hours later; print
    # the derived id here so a reused hostname is obvious at install time.
    if [ "$SPOKE_ID_PINNED" = "1" ] && [ -n "${SPOKE_ID:-}" ]; then
        echo "   Identity reset. This spoke will register as: $SPOKE_ID (pinned via --id)"
    else
        echo "   Identity reset. This spoke will register as: $(hostname) (derived from the hostname)"
        echo "   If another live box already uses that hostname, the two WILL collide on the hub — rename one first."
    fi
fi

# ── Spoke secret (preserve on re-run) ────────────────────────────────────────
EXISTING_SECRET=""
[ -f "$LM_DIR/cs/.env" ] && \
    EXISTING_SECRET=$(grep "^SPOKE_SECRET=" "$LM_DIR/cs/.env" | cut -d= -f2-)

if [ -z "$SPOKE_SECRET" ]; then
    if [ -n "$EXISTING_SECRET" ]; then
        SPOKE_SECRET="$EXISTING_SECRET"
        ok "Reusing existing spoke secret"
    else
        warn "No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
    fi
fi

# ── Install UUID (preserve on re-run) ─────────────────────────────────────────
# Keep the minted INSTALL_UUID across re-runs so the hub-side fingerprint
# (install_uuid) stays stable. The cat > .env below truncates the file, so
# without this the UUID line is wiped and the spoke mints a fresh one on next
# start → hub records a `reimaged` (fingerprint-changed) event for a box that
# was only updated. _ensure_install_uuid mints on first start only when this
# line is absent, so a fresh install is unchanged.
INSTALL_UUID_LINE=""
if [ -f "$LM_DIR/cs/.env" ] && grep -q "^INSTALL_UUID=" "$LM_DIR/cs/.env"; then
    EXISTING_UUID=$(grep "^INSTALL_UUID=" "$LM_DIR/cs/.env" | cut -d= -f2-)
    if [ -n "$EXISTING_UUID" ]; then
        INSTALL_UUID_LINE="INSTALL_UUID=$EXISTING_UUID"
        ok "Preserving existing install UUID (hub fingerprint)"
    fi
fi

# --clone: strip minted identity so the .env ships with NO secret and NO
# INSTALL_UUID. Each clone mints its own on first boot (control_plane
# _ensure_install_uuid) → distinct hub identity per clone. HUB_URL + HUB_SECRET
# (PSK) are kept so clones auto-register without manual approval.
if [ "$CLONE_MODE" = "1" ]; then
    SPOKE_SECRET=""
    INSTALL_UUID_LINE=""
    warn "--clone: identity stripped — each clone mints a fresh UUID + id on first boot."
fi

# ── Retire any legacy lm-generic-agent on this box ───────────────────────────
# Vendored from lm/agent/install_agent.sh:retire_legacy_agent — keep in sync.
# The legacy leaf (lm-generic-agent, /opt/lm/generic-agent/src/agent.py) is
# protocol-incompatible with the session-key-adopting hub: it has no
# SPOKE_UPDATE_SESSION_KEY / LOAD_ROLE handler, connects + passes mTLS but never
# adopts a session key, and the hub refuses to dispatch to it (every role on
# the box times out while the WS stays "online"). Purge it before the clone so
# even an aborted install can't leave the zombie connecting under this box's
# id. Idempotent + non-fatal if absent; never touches this installer's own unit
# ($SERVICE_NAME) — it's (re)written below.
SERVICE_NAME="lm-cs"
retire_legacy_agent() {
    # Match the legacy leaf by BOTH its historical unit name AND any unit whose
    # definition ExecStarts /opt/lm/generic-agent/src/agent.py (older template
    # builders named the unit variously). Never touch this installer's own unit.
    local names="lm-generic-agent"
    local f
    for f in /etc/systemd/system/*.service /etc/systemd/system/*/*.service \
             /run/systemd/system/*.service \
             /lib/systemd/system/*.service /usr/lib/systemd/system/*.service; do
        [ -e "$f" ] || continue
        if grep -qE "/opt/lm/generic-agent" "$f" 2>/dev/null; then
            names="$names $(basename "$f" .service)"
        fi
    done
    local u
    for u in $(systemctl list-units --type=service --state=running,failed --no-legend --plain 2>/dev/null | awk '{print $1}'); do
        if systemctl show "$u" -p ExecStart 2>/dev/null | grep -q "/opt/lm/generic-agent"; then
            names="$names ${u%.service}"
        fi
    done
    local svc purged=0
    for svc in $(printf '%s\n' $names | sort -u); do
        [ -n "$svc" ] || continue
        [ "$svc" = "$SERVICE_NAME" ] && continue
        if [ -e "/etc/systemd/system/${svc}.service" ] \
           || systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -qE "^${svc}\.service"; then
            systemctl stop    "$svc" 2>/dev/null || true
            systemctl disable "$svc" 2>/dev/null || true
            rm -f "/etc/systemd/system/${svc}.service"
            systemctl mask    "$svc" 2>/dev/null || true
            echo "🧹  Purged legacy leaf unit ${svc}.service."
            purged=1
        fi
    done
    if [ -d /opt/lm/generic-agent ]; then
        pkill -f "/opt/lm/generic-agent/src/agent.py" 2>/dev/null || true
        rm -rf /opt/lm/generic-agent
        echo "🧹  Removed legacy leaf dir /opt/lm/generic-agent."
        purged=1
    fi
    if [ "$purged" = 1 ]; then
        systemctl daemon-reload 2>/dev/null || true
        echo "    The role-capable ${SERVICE_NAME} now owns this box's spoke connection."
    fi
}
retire_legacy_agent

# ── Clone / update ────────────────────────────────────────────────────────────
step "Installing Generic Agent"
mkdir -p "$LM_DIR"

# LM core (base_spoke + shared base classes). CSSpoke subclasses BaseSpoke and
# control_plane subclasses BaseControlPlane, both of which live ONLY in the lm
# hub repo's core/ subdir (lm/core/src/base_spoke.py, lm/core/src/messaging/).
# The service unit's PYTHONPATH below includes $LM_DIR/core/src so
# `from base_spoke` / `from core.src.base_spoke` resolve there. On a FRESH
# cs-only box /opt/lm/core does not exist, so the spoke crash-loops with
# "ModuleNotFoundError: No module named 'base_spoke'" (a prior install only
# worked because a hub install had already laid /opt/lm/core).
#
# Provision /opt/lm as a REAL lm.git checkout (the all-in-one layout
# install_agent.sh uses) — NOT a vendored core/ extraction. The whole repo
# clones to $LM_DIR, so /opt/lm/.git exists and base_spoke lands at the correct
# $LM_DIR/core/src/base_spoke.py (cloning into $LM_DIR/core would nest core/ one
# level deep — base_spoke at $LM_DIR/core/core/src/base_spoke.py — and break the
# PYTHONPATH). A real checkout means the spoke can `git pull` /opt/lm to pick up
# lm/core changes via the WebUI Update button / auto-update (SPOKE_UPDATE pulls
# the shared core too) — no CLI `git -C /opt/lm pull` + restart required.
LM_CORE_URL="https://github.com/lbockenstedt/lm.git"
LM_CORE_BRANCH="main"
_lm_core_refresh() {
    # $1 = reason. Idempotent: an existing real checkout (/.git present) is just
    # fetched + hard-reset to origin/$LM_CORE_BRANCH. A fresh box — or an OLD
    # cs install with a non-git vendored /opt/lm/core (no /opt/lm/.git) — gets a
    # one-time conversion: the co-located /opt/lm/cs clone is moved aside, /opt/lm
    # is wiped + re-cloned from lm.git, and /opt/lm/cs is restored.
    warn "LM core refresh ($1)"
    if [ -d "$LM_DIR/.git" ]; then
        local cur_origin
        cur_origin="$(git -C "$LM_DIR" config --get remote.origin.url 2>/dev/null || true)"
        [ "$cur_origin" = "$LM_CORE_URL" ] || \
            git -C "$LM_DIR" remote set-url origin "$LM_CORE_URL" 2>/dev/null || true
        git -C "$LM_DIR" fetch -q origin 2>/dev/null || true
        git -C "$LM_DIR" reset --hard "origin/$LM_CORE_BRANCH" >/dev/null 2>&1 || true
        return 0
    fi
    # No /opt/lm/.git → fresh checkout or conversion from the old vendored
    # layout. Preserve any existing /opt/lm/cs clone across the wipe so a
    # re-run on an already-installed box doesn't strand the cs repo.
    local cs_bak=""
    if [ -d "$LM_DIR/cs" ]; then
        cs_bak="$LM_DIR/.lm-cs-bak.$$"
        mv "$LM_DIR/cs" "$cs_bak"
    fi
    rm -rf "$LM_DIR"
    if ! git clone -q --branch "$LM_CORE_BRANCH" "$LM_CORE_URL" "$LM_DIR"; then
        if [ -n "$cs_bak" ]; then
            mkdir -p "$LM_DIR" 2>/dev/null || true
            mv "$cs_bak" "$LM_DIR/cs" 2>/dev/null || true
        fi
        return 1
    fi
    if [ -n "$cs_bak" ]; then
        mv "$cs_bak" "$LM_DIR/cs"
    fi
}
if [ -f "$LM_DIR/core/src/base_spoke.py" ]; then
    echo "   Updating LM core (base_spoke)"
    _lm_core_refresh "update" || { echo "❌ LM core update failed (network access to $LM_CORE_URL?)"; exit 1; }
else
    # A stale dir from a botched prior install (or an older installer that cloned
    # the whole lm repo here) is removed by _lm_core_refresh before the swap.
    echo "   Cloning LM core (base_spoke)"
    _lm_core_refresh "install" || { echo "❌ LM core clone failed (network access to $LM_CORE_URL?)"; exit 1; }
fi
# Verify base_spoke actually landed — a wrong-repo clone, a partial clone, or a
# future lm layout change would otherwise leave the spoke crash-looping with a
# confusing import error. Fail loudly with the exact missing path instead.
if [ ! -f "$LM_DIR/core/src/base_spoke.py" ]; then
    echo "❌ FATAL: base_spoke.py not found at $LM_DIR/core/src/base_spoke.py after clone."
    echo "   CS spoke subclasses BaseSpoke from the lm hub repo; the spoke cannot start without it."
    echo "   Check network access to $LM_CORE_URL and that lm main carries core/src/base_spoke.py."
    exit 1
fi
ok "LM core (base_spoke) ready"

if [ -d "$LM_DIR/cs/.git" ]; then
    echo "   Updating existing install"
    git -C "$LM_DIR/cs" pull --rebase --autostash origin main -q
else
    echo "   Cloning CS / Generic Agent repo"
    git clone -q https://github.com/lbockenstedt/cs.git "$LM_DIR/cs"
fi

# Every clone/pull above ran as ROOT, so each repo's .git/objects is root-owned.
# Chown EVERY repo's .git ($LM_DIR/.git + $LM_DIR/cs/.git) to the spoke user NOW —
# not only via the recursive $LM_DIR chown at the end — so the ongoing, unattended
# self-update (lm.self_update runs as $SVC_USER) can write objects. A root-owned
# .git/objects is the exact "insufficient permission for adding an object to
# repository database .git/objects" that fails `git fetch` and strands the spoke
# on stale code forever (it can't even pull the fix for itself). Belt-and-braces
# with the final chown + the runtime self-heal in self_update.py.
find "$LM_DIR" -maxdepth 2 -type d -name .git \
    -exec chown -R "$SVC_USER:$SVC_USER" {} + 2>/dev/null || true

# ── Python venv ───────────────────────────────────────────────────────────────
rm -rf "$LM_DIR/cs/venv"
python3 -m venv "$LM_DIR/cs/venv"
"$LM_DIR/cs/venv/bin/pip" install --upgrade pip -q
# Requirements live at lm-spoke/requirements.txt (the spoke's deps). Fall back
# to a top-level requirements.txt if one exists. The previous check installed
# from $LM_DIR/cs/requirements.txt, which does not exist in this repo, so the
# `&&` was skipped and the venv was created with NO packages — the spoke then
# crashed on import (websockets/fastapi). Installing the real file fixes that.
CS_REQ="$LM_DIR/cs/lm-spoke/requirements.txt"
[ -f "$LM_DIR/cs/requirements.txt" ] && CS_REQ="$LM_DIR/cs/requirements.txt"
"$LM_DIR/cs/venv/bin/pip" install -r "$CS_REQ" -q
# The spoke runs lm CORE code (messaging/, security/frame_crypto → cryptography,
# etc.), so the core's runtime deps MUST be in this venv too — the spoke's own
# requirements.txt does not list them. Install core/requirements.txt when the
# shared core checkout is present; fall back to an explicit cryptography floor
# so a missing/partial core tree still can't crash-loop the spoke on import.
if [ -f "$LM_DIR/core/requirements.txt" ]; then
    "$LM_DIR/cs/venv/bin/pip" install -r "$LM_DIR/core/requirements.txt" -q \
        || warn "core requirements install had issues — spoke may miss core deps"
else
    "$LM_DIR/cs/venv/bin/pip" install -q 'cryptography>=42,<50'
fi
ok "Dependencies installed"

# ── Hub auto-discovery ──────────────────────────────────────────────────────
# When --hub was not given (and no HUB_URL env), auto-locate the hub via DNS
# (lm-hub.<dns-suffix>) then mDNS (_lm-hub._tcp.local.) using the just-installed
# venv + lm core's messaging.hub_discovery. If nothing is found, leave HUB_URL
# empty — the spoke re-discovers at startup (BaseControlPlane.run sentinel) once
# the hub is up, so this never hard-fails a hub-box install where the hub isn't
# running yet at cs-install time.
CS_VENVPY="$LM_DIR/cs/venv/bin/python3"
if [ "$HUB_URL_PINNED" != "1" ]; then
    echo "🔎 No --hub given; auto-discovering the LM hub (DNS lm-hub.* / mDNS)…"
    DISCOVERED=$(PYTHONPATH="$LM_DIR/core/src" "$CS_VENVPY" -m messaging.hub_discovery --timeout 5 2>/dev/null || echo NONE)
    if [ -n "$DISCOVERED" ] && [ "$DISCOVERED" != "NONE" ]; then
        HUB_URL="$DISCOVERED"
        echo "✅ Discovered hub: $HUB_URL"
    else
        echo "⚠️  Hub not found via DNS/mDNS. Leaving HUB_URL empty — the spoke will"
        echo "    retry auto-discovery at startup. To pin it now, re-run with"
        echo "    --hub ws://HUB:8765 (or create an 'lm-hub' DNS record / enable mDNS on the hub)."
        HUB_URL=""
    fi
fi

# Bake SPOKE_ID into .env + the unit ONLY when it was explicitly pinned. In the
# derived case Python uses the bare `<hostname>` at startup, so a clone that was
# renamed reconnects under a new id (correlated to the old one via the install
# UUID). INSTALL_UUID is deliberately NOT written here — the spoke mints it at
# first start (BaseControlPlane._ensure_install_uuid), and prep-for-imaging
# strips it so a cloned image gets a fresh one.
SPOKE_ID_LINE=""
ID_ARG=""
if [ "$SPOKE_ID_PINNED" = "1" ]; then
    SPOKE_ID_LINE="SPOKE_ID=$SPOKE_ID"
    ID_ARG="--id $SPOKE_ID"
fi

# TLS verify lines for the .env (empty unless --tls-verify was passed).
TLS_VERIFY_LINE="LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV"
TLS_CA_LINE=""
[ -n "$HUB_TLS_CA_ENV" ] && TLS_CA_LINE="LM_HUB_CA_CERT=$HUB_TLS_CA_ENV"

# ── Agent listener (split topology, --agent-listener opt-in) ─────────────────
# (Implementation lifted into setup_sim_agent_listener() near the top so
# --infra-only can reuse it; called here for the full install exactly where it
# used to run. The CS_AGENT_LISTENER_LINES / LM_TLS_CERT / LM_TLS_KEY vars it
# sets are consumed by the .env writer just below and by the CAP_LINES check.)
setup_sim_agent_listener

# ── .env ──────────────────────────────────────────────────────────────────────
cat > "$LM_DIR/cs/.env" <<DOTENV
HUB_URL=$HUB_URL
${SPOKE_ID_LINE}
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=${HUB_SECRET:-}
CS_API_PORT=$CS_API_PORT
CS_API_HOST=$CS_API_HOST
${TLS_VERIFY_LINE}
${TLS_CA_LINE}
${CS_AGENT_LISTENER_LINES}
${INSTALL_UUID_LINE}
DOTENV
chmod 600 "$LM_DIR/cs/.env"

# ── systemd unit ──────────────────────────────────────────────────────────────

# Only pass --secret/--hub-secret when a value is present. Passing them with an
# empty value makes argparse abort with "argument --secret: expected one
# argument", crash-looping the service. Zero-touch provisioning handles the
# empty case (control_plane.py falls back to SPOKE_SECRET from the .env, then
# awaits admin approval in the WebUI).
SECRET_ARG=""
[ -n "$SPOKE_SECRET" ] && SECRET_ARG="--secret=$SPOKE_SECRET"
HUB_SECRET_ARG=""
[ -n "${HUB_SECRET:-}" ] && HUB_SECRET_ARG="--hub-secret=$HUB_SECRET"

# CAP_NET_BIND_SERVICE lets svc_lm (non-root) bind :443 for the agent listener.
# Only granted when --agent-listener is on; a relay-only cs spoke needs no
# extra capability.
# CAP_SYSLOG additionally lets the spoke READ the kernel ring buffer, which is
# what the Sim DHCP health panel needs to show AppArmor denials. Without it the
# panel can only say "NOT serving" and the operator is back to ssh + dmesg — the
# exact loop this panel exists to remove. Read-only: CAP_SYSLOG grants klogctl
# reads, not writes, and does not bypass file permissions.
CAP_LINES=""
if [ "$CS_AGENT_LISTENER" = "1" ]; then
    CAP_LINES="AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_SYSLOG
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_SYSLOG"
else
    CAP_LINES="AmbientCapabilities=CAP_SYSLOG
CapabilityBoundingSet=CAP_SYSLOG"
fi

cat > /etc/systemd/system/lm-cs.service <<SYSD
[Unit]
Description=Lab Manager Spoke - Generic Agent
After=network.target
# Never let a burst of restarts trip systemd's start-rate limit and PARK the
# service dead ("start-limit-hit" → systemd refuses to revive it even with
# Restart=always). This is how cs-svr-05 went permanently down: flapping
# reconnects + self-update restarts + hub-contact-watchdog restarts stacked past
# the default 5-in-10s limit. A spoke that keeps restarting is far better than a
# dead one; a genuine bad build is caught by update_recovery/dep_guard, not by
# starving the restart budget. (The Kea units already set this.)
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$LM_DIR/cs
EnvironmentFile=$LM_DIR/cs/.env
Environment="PYTHONPATH=$LM_DIR:$LM_DIR/core/src:$LM_DIR/cs/lm-spoke:$LM_DIR/cs/lm-spoke/src"
Environment="CS_API_PORT=$CS_API_PORT"
Environment="CS_API_HOST=$CS_API_HOST"
ExecStart=$LM_DIR/cs/venv/bin/python3 -m src.control_plane $ID_ARG --hub "\${HUB_URL}" $SECRET_ARG $HUB_SECRET_ARG --port $CS_API_PORT --host $CS_API_HOST
$CAP_LINES

StandardOutput=append:/var/log/lm/lm-cs.log
StandardError=append:/var/log/lm/lm-cs.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
# Clear any prior 'start-limit-hit'/failed state so a reinstall over a spoke that
# systemd previously PARKED (the bug StartLimitIntervalSec=0 above prevents going
# forward) actually starts instead of staying dead.
systemctl reset-failed lm-cs 2>/dev/null || true
systemctl enable lm-cs
if [ "$CLONE_MODE" = "1" ]; then
    # Golden-image prep: enable for next boot but do NOT start now — starting
    # would mint an INSTALL_UUID on the template and bake it into every clone.
    systemctl stop lm-cs 2>/dev/null || true
    ok "Clone template prepped — lm-cs enabled (starts on next boot), identity NOT minted"
else
    systemctl restart lm-cs

    # Verify the DIAGNOSTICS actually work, not just the service. The health
    # panel runs AS the spoke; if it cannot read the lease file or the kernel
    # log it degrades to "NOT serving" with no reason, and the operator is back
    # to ssh — which is precisely what this panel replaces. Group membership
    # only applies to processes started AFTER usermod, hence checking here,
    # after the restart, rather than assuming.
    sleep 2
    _lease="/var/lib/kea/kea-leases4-sim.csv"
    if [ -f "$_lease" ]; then
        if runuser -u "$SVC_USER" -- test -r "$_lease" 2>/dev/null; then
            ok "Diagnostics: spoke can read the lease DB (health panel will show real lease counts)"
        else
            warn "Diagnostics: ${SVC_USER} still cannot read $_lease — the health panel will show 'count unreadable'. The sim lease file is root:root (our Kea unit runs as root), so the _kea group does NOT cover it. Fix: setfacl -m u:${SVC_USER}:r $_lease && setfacl -d -m u:${SVC_USER}:r /var/lib/kea"
        fi
    fi
    ok "Generic Agent service started"
fi

# chown the WHOLE lm checkout to the spoke user so the spoke process can
# `git pull` /opt/lm itself during a hub-driven SPOKE_UPDATE (the shared core
# now lives in a real git checkout at $LM_DIR). Covers $LM_DIR/cs too.
chown -R "$SVC_USER:$SVC_USER" "$LM_DIR" 2>/dev/null || true

# The per-spoke recovery state dir lives under /var/lib/lm/ but the spoke runs
# as $SVC_USER (non-root) and CANNOT create a dir under /var/lib/lm itself — so
# without this, the pre-update snapshot + rollback silently disable with
# "Permission denied: '/var/lib/lm'". Create it (as root, here) and make it
# writable by the spoke. chown is non-recursive so a co-located hub's
# /var/lib/lm/state (root-owned) is untouched.
mkdir -p /var/lib/lm 2>/dev/null || true
chown "$SVC_USER:$SVC_USER" /var/lib/lm 2>/dev/null || true
chmod 755 /var/lib/lm 2>/dev/null || true

# ── Failed-update rollback watchdog + sudoers ─────────────────────────────────
# Per-spoke recovery state lives in /var/lib/lm/<spoke_id>/ (created on demand at
# runtime by the spoke): pre-swap code snapshot, pending-update manifest, healthy
# marker, bad-commit registry. The external health-gate watchdog below reads them
# and rolls back a self-update that crashes at boot (git reset --hard <from_commit>)
# instead of letting it crash-loop forever under Restart=always. The spoke (svc_lm)
# schedules it via `sudo -n` right before it os._exit(3)s to load new code; the
# sudoers entry grants only this path. Mirrors the hub's lm-update-restart.
cat > /usr/local/bin/lm-component-update-restart <<'HELPER'
#!/bin/bash
# lm-component-update-restart — external health-gate watchdog for spoke/agent
# self-updates. Scheduled by the component (sudo -n for spokes, direct for the
# root agent) right before it exits to load new code. Runs OUTSIDE the
# component's systemd cgroup (via systemd-run) so it survives the component's
# restart and can roll back a failed update instead of letting it crash-loop
# forever under Restart=always.
#
# Rollback policy: the watchdog waits up to --deadline for a `healthy` marker
# (written by the component after it re-auths with the hub/spoke). If instead
# it sees a crash-loop (NRestarts >= 3) or a failed/inactive unit, it rolls
# back — `git reset --hard <from_commit>` for a spoke (--repo-root, a git repo)
# or a file-tree restore for the agent (--install-dir, non-git) — marks the
# version/commit bad so the next update skips it, and restarts the component.
# A unit that is active-and-running but hasn't written the marker (the hub/spoke
# is unreachable so the component can't auth) is NOT rolled back — the code
# booted; the missing marker is a connectivity issue, not a code failure, and
# rolling back a good update during a hub outage would strand the component on
# old code and mark a good commit/version bad.
#
# Dual-repo rollback: when the spoke update ALSO pulled the shared /opt/lm core
# checkout (--core-repo-root + core_from_commit/core_to_commit in the pending
# manifest), a boot failure resets BOTH repos — the spoke first, then core.
# The core to_commit is marked bad so the next SPOKE_UPDATE skips a crash-
# looping core. v1 is NON-ATOMIC across the two repos: a watchdog crash between
# the two `git reset --hard`s leaves the spoke rolled back but core forward —
# recoverable via the on-disk manifest + the `writefailed` marker. Atomic
# two-repo rollback is deferred.
#
# State-file ops delegate to the Python CLI update_recovery.py (SINGLE SOURCE OF
# TRUTH for the on-disk recovery state machine). Only poll/systemd/git logic
# lives here. This file is the canonical source; install_cs.sh / install_pxmx.sh
# / install_agent.sh embed it verbatim via here-doc — keep them in sync.
set -uo pipefail

UNIT="" STATE_DIR="" REPO_ROOT="" INSTALL_DIR="" DEADLINE=90 CORE_REPO_ROOT=""
RECOVERY_PY="/opt/lm/core/src/update_recovery.py"

# Re-exec under a transient systemd unit outside the component's cgroup so this
# process survives the `systemctl restart <unit>` it issues (otherwise the
# restart kills us before we can poll or roll back). The guard prevents an
# infinite re-exec loop. Mirrors lm-update-restart's transient-unit trick.
if [ -z "${LM_COMP_UPDATE_GUARD:-}" ]; then
    export LM_COMP_UPDATE_GUARD=1
    exec systemd-run --no-block --quiet --collect \
        --unit="lm-comp-update-$$-$RANDOM" --service-type=oneshot \
        --setenv=LM_COMP_UPDATE_GUARD=1 \
        /usr/local/bin/lm-component-update-restart "$@"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --unit) UNIT="$2"; shift 2;;
        --state-dir) STATE_DIR="$2"; shift 2;;
        --repo-root) REPO_ROOT="$2"; shift 2;;
        --core-repo-root) CORE_REPO_ROOT="$2"; shift 2;;
        --install-dir) INSTALL_DIR="$2"; shift 2;;
        --deadline) DEADLINE="$2"; shift 2;;
        --recovery-py) RECOVERY_PY="$2"; shift 2;;
        *) shift;;
    esac
done

HEALTHY="$STATE_DIR/healthy"
PENDING="$STATE_DIR/pending_update.json"

# Self-link the lmctl operator CLI on every update. The spoke self-update runs as
# the non-root service user and can't write the root-owned /usr/local/bin symlink;
# this watchdog runs as root right after each SPOKE_UPDATE pulls fresh code, so it
# is the natural place to keep lmctl current WITHOUT a reinstall. Idempotent.
for _lmctl in /opt/lm/scripts/lmctl /opt/lm/cs/lm-spoke/scripts/lmctl; do
    if [ -f "$_lmctl" ]; then
        chmod +x "$_lmctl" 2>/dev/null || true
        ln -sf "$_lmctl" /usr/local/bin/lmctl 2>/dev/null || true
        break
    fi
done

# 0 if the component is healthy (marker present) OR booted-but-pending-auth
# (active, not crash-looping); 1 if still failing (crash-loop / failed / unknown).
unit_ok() {
    [ -f "$HEALTHY" ] && return 0
    local a n
    a="$(systemctl show "$UNIT" -p ActiveState --value 2>/dev/null || echo "")"
    n="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo 0)"
    n="${n:-0}"
    [ "$a" = "active" ] && [ "$n" -lt 3 ] && return 0
    return 1
}

clear_and_prune() {
    python3 "$RECOVERY_PY" clearpending --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    python3 "$RECOVERY_PY" prune --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
}

# 1) Wait up to DEADLINE for the new code to boot + re-auth (healthy marker).
waited=0
while [ "$waited" -lt "$DEADLINE" ]; do
    if [ -f "$HEALTHY" ]; then
        clear_and_prune
        exit 0
    fi
    sleep 5; waited=$((waited + 5))
done

# 2) Deadline elapsed, no marker. Active-and-stable → connectivity, not code.
if unit_ok; then
    echo "lm-component-update-restart: $UNIT active but no healthy marker within ${DEADLINE}s — assuming hub/spoke unreachable (not a code failure); no rollback." >&2
    clear_and_prune
    exit 0
fi

# 3) Crash-loop or failed → roll back to the pre-swap code.
pending="$(cat "$PENDING" 2>/dev/null || true)"
bdir="$(printf '%s' "$pending" | jq -r '.backup_dir // empty' 2>/dev/null)"
from_commit="$(printf '%s' "$pending" | jq -r '.from_commit // empty' 2>/dev/null)"
to_commit="$(printf '%s' "$pending" | jq -r '.to_commit // empty' 2>/dev/null)"
to_v="$(printf '%s' "$pending" | jq -r '.to_version // empty' 2>/dev/null)"
core_from="$(printf '%s' "$pending" | jq -r '.core_from_commit // empty' 2>/dev/null)"
core_to="$(printf '%s' "$pending" | jq -r '.core_to_commit // empty' 2>/dev/null)"

echo "lm-component-update-restart: $UNIT failed to boot (crash-loop/failed); rolling back." >&2

if [ -n "$REPO_ROOT" ]; then
    # Spoke (git repo): reset hard to the pre-update commit + clean stray files.
    if [ -n "$from_commit" ]; then
        git -C "$REPO_ROOT" reset --hard "$from_commit" >/dev/null 2>&1 || true
        git -C "$REPO_ROOT" clean -fd >/dev/null 2>&1 || true
    fi
    if [ -n "$to_commit" ]; then
        python3 "$RECOVERY_PY" markbadcommit "$to_commit" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    fi
elif [ -n "$INSTALL_DIR" ]; then
    # Agent (non-git install dir): file-tree restore from the pre-swap snapshot.
    if [ -n "$bdir" ] && [ -d "$bdir/src" ]; then
        python3 "$RECOVERY_PY" rollback --hub-root "$INSTALL_DIR" --backup-dir "$bdir" \
            --tree src --state-dir "$STATE_DIR" --chown-user root >/dev/null 2>&1 || true
    fi
    if [ -n "$to_v" ]; then
        python3 "$RECOVERY_PY" markbad "$to_v" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    fi
fi

# Dual-repo rollback: reset the shared /opt/lm core checkout AFTER the spoke
# repo so a crash-looping core (e.g. a bad BaseControlPlane change) is rolled
# back too. The core to_commit is marked bad so the next SPOKE_UPDATE skips it
# (the spoke's _is_known_bad_commit guard) instead of re-pulling it. Skipped
# entirely when no --core-repo-root / core fields were recorded — single-repo
# behavior is unchanged.
if [ -n "$CORE_REPO_ROOT" ] && [ -n "$core_from" ]; then
    echo "lm-component-update-restart: rolling back shared core at $CORE_REPO_ROOT to $core_from." >&2
    git -C "$CORE_REPO_ROOT" reset --hard "$core_from" >/dev/null 2>&1 || true
    git -C "$CORE_REPO_ROOT" clean -fd >/dev/null 2>&1 || true
    if [ -n "$core_to" ]; then
        python3 "$RECOVERY_PY" markbadcommit "$core_to" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    fi
fi

python3 "$RECOVERY_PY" clearpending --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
systemctl restart "$UNIT" 2>/dev/null || true

# 4) Did the rolled-back code come back? (marker OR active-and-stable.)
waited=0
while [ "$waited" -lt 30 ]; do
    if unit_ok; then
        echo "lm-component-update-restart: $UNIT rolled back; marked bad; recovered." >&2
        python3 "$RECOVERY_PY" prune --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
        exit 0
    fi
    sleep 5; waited=$((waited + 5))
done

# 5) Rolled-back code ALSO failed — last-resort marker for manual recovery.
python3 "$RECOVERY_PY" writefailed --to-version "${to_v:-${to_commit:-unknown}}" \
    --backup-dir "$bdir" --reason "rollback did not come healthy within 30s" \
    --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
echo "lm-component-update-restart: $UNIT rollback also failed; left for manual recovery (snapshot at $bdir)." >&2
exit 1
HELPER
chmod 0755 /usr/local/bin/lm-component-update-restart

# lmctl — standard operator CLI (update / debug / logs / status / restart /
# recover / harden / stall). Symlink to the copy in the core checkout so it
# tracks SPOKE_UPDATE automatically; fall back to a static copy if scripts/ isn't
# present in this layout. `lmctl help` lists commands.
if [ -f "$LM_DIR/scripts/lmctl" ]; then
    chmod +x "$LM_DIR/scripts/lmctl" 2>/dev/null || true
    ln -sf "$LM_DIR/scripts/lmctl" /usr/local/bin/lmctl
    ok "lmctl installed (→ $LM_DIR/scripts/lmctl)"
fi

# Grant svc_lm passwordless sudo ONLY for the watchdog path (mirrors the hub's
# NOPASSWD: /usr/local/bin/lm-update-restart in install_all.sh). /etc/sudoers.d
# is created by the sudo package's postinst (installed above); mkdir here too
# as a defensive belt-and-suspenders in case a minimal image ever lacks it.
mkdir -p /etc/sudoers.d
cat > /etc/sudoers.d/lm-component-update <<SUDOERS
$SVC_USER ALL=(ALL) NOPASSWD: /usr/local/bin/lm-component-update-restart
SUDOERS
chmod 0440 /etc/sudoers.d/lm-component-update
visudo -cf /etc/sudoers.d/lm-component-update >/dev/null 2>&1 || true

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "this-host")
echo ""
echo "════════════════════════════════════════════"
if [ "$CLONE_MODE" = "1" ]; then
    ok "Clone template prepared!"
else
    ok "Generic Agent installation complete!"
fi
echo "════════════════════════════════════════════"
if [ "$CLONE_MODE" = "1" ]; then
    echo "  CLONE MODE:   lm-cs is ENABLED but NOT started; identity is unset."
    echo "                → Shut this box down and clone/template it now."
    echo "                → Each clone, on first boot, mints a fresh UUID, derives"
    echo "                  its id from its hostname, and connects to the hub."
    echo "                → Rename each clone's hostname BEFORE first boot for a"
    echo "                  distinct spoke id."
    [ -z "${HUB_SECRET:-}" ] && echo "                ⚠ No --hub-secret (PSK): each clone will await manual approval in the WebUI."
fi
if [ -n "$HUB_URL" ]; then
    echo "  LM Hub:       $HUB_URL"
else
    echo "  LM Hub:       (auto-discover at startup — no lm-hub DNS/mDNS found yet)"
fi
if [ "$SPOKE_ID_PINNED" = "1" ]; then
    echo "  Spoke ID:     $SPOKE_ID  (pinned)"
else
    echo "  Spoke ID:     $(hostname)  (derived from hostname at startup)"
fi
echo "  Version:      $(cat $LM_DIR/cs/VERSION 2>/dev/null || echo unknown)"
echo "  Status:       sudo systemctl status lm-cs"
echo "  Service log:  /var/log/lm/lm-cs.log  (sudo journalctl -u lm-cs -f)"
echo "  Install log:  $INSTALL_LOG"
echo "  Rollback:     /usr/local/bin/lm-component-update-restart — a failed self-"
echo "                update (crash at boot) is rolled back to the prior commit"
echo "                automatically. NOTE: this watchdog + sudoers land only on a"
echo "                full installer re-run; a box that only git-pulled the new"
echo "                spoke code must be re-installed once to enable rollback."
if [[ -n "${DHCP_IFACE:-}" && "$DHCP_SKIP" != "1" ]]; then
    if systemctl is-active --quiet kea-dhcp4-sim 2>/dev/null; then
        echo "  DHCP:          cs-owned Kea (kea-dhcp4-sim) RUNNING on ${DHCP_IFACE} (${DHCP_RANGE_START}–${DHCP_RANGE_END}); ctrl-agent :8002"
    else
        echo "  DHCP:          ${DHCP_IFACE} configured — kea-dhcp4-sim NOT RUNNING (journalctl -u kea-dhcp4-sim)"
    fi
else
    echo "  DHCP:          skipped (single NIC or --no-dhcp)"
fi
if [ "$CS_AGENT_LISTENER" = "1" ]; then
    echo "  Agent listener: ENABLED — a pxmx host agent can dial this cs spoke directly."
    echo "                  Pin the agent install to THIS spoke (not the pxmx spoke) —"
    echo "                  supply just this spoke's IP; the agent auto-determines the rest:"
    echo "                  agent/install_agent.sh --spoke-ip ${LOCAL_IP}"
    echo "                  (a cs spoke does not broadcast _lm-hub mDNS — the agent must be pinned)"
else
    echo "  Agent listener: disabled (--no-agent-listener was passed; relay-only, this cs spoke never binds :443)"
fi
echo ""
