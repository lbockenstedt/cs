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
# DHCP (dnsmasq on the second NIC):
#   The spoke provides DHCP for the isolated simulation-client network on a
#   second NIC, ported verbatim from installers/install-lxc.sh STEP 3. The 2nd
#   NIC is auto-detected; if only one NIC is present, DHCP is skipped. The NIC
#   must already be attached to the container/host in Proxmox.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh \
#     | sudo bash -s -- --hub ws://HUB_IP:8765
#
# Environment variable overrides (set before running):
#   DHCP_IFACE, DHCP_SUBNET, DHCP_PREFIX, DHCP_GATEWAY, DHCP_RANGE_START,
#   DHCP_RANGE_END, DHCP_LEASE_TIME, DHCP_SKIP (1 to skip DHCP entirely)
# ============================================================

HUB_URL="ws://localhost:8765"
SPOKE_ID="cs-spoke-1"
SPOKE_SECRET=""
HUB_SECRET=""
ADMIN_TOKEN=""
SVC_USER="svc_lm"
LM_DIR="/opt/lm"
# Client API listener. The spoke owns the isolated sim-client DHCP scope
# (169.253.1.1/24 on the 2nd NIC) and is also the client API gateway on
# 169.253.1.1:8000. Binding 0.0.0.0 puts the listener on the DHCP NIC too;
# dnsmasq serves 169.253.1.0/24 with no router option, so clients reach it
# directly. Override either before running.
CS_API_PORT="${CS_API_PORT:-8000}"
CS_API_HOST="${CS_API_HOST:-0.0.0.0}"

# ── DHCP (sim-client isolated network) — port of install-lxc.sh STEP 3 ─────────
# A second NIC runs dnsmasq DHCP for the isolated simulation-client network.
# Auto-detected (2nd NIC) unless DHCP_IFACE is set. DHCP_SKIP=1 skips entirely.
DHCP_IFACE="${DHCP_IFACE:-}"
DHCP_SKIP="${DHCP_SKIP:-0}"
DHCP_SUBNET="${DHCP_SUBNET:-169.253.1.0}"
DHCP_PREFIX="${DHCP_PREFIX:-24}"
DHCP_GATEWAY="${DHCP_GATEWAY:-169.253.1.1}"
DHCP_RANGE_START="${DHCP_RANGE_START:-169.253.1.11}"
DHCP_RANGE_END="${DHCP_RANGE_END:-169.253.1.254}"
DHCP_LEASE_TIME="${DHCP_LEASE_TIME:-1h}"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub)         HUB_URL="$2";      shift ;;
        --id|--name)   SPOKE_ID="$2";     shift ;;
        --secret)      SPOKE_SECRET="$2"; shift ;;
        --hub-secret)  HUB_SECRET="$2";   shift ;;
        --dhcp-iface)  DHCP_IFACE="$2";   shift ;;
        --no-dhcp)     DHCP_SKIP=1 ;;
        --admin-token) ;; # deprecated
        --all-prereqs) ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "❌ Must be run as root."; exit 1; }

GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✅  $*${NC}"; }
warn() { echo -e "${YLW}⚠️   $*${NC}"; }
step() { echo -e "\n${GRN}━━  $*  ━━${NC}"; }

step "Lab Manager — Generic Agent Installer"

# ── System packages ───────────────────────────────────────────────────────────
step "Installing system packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3 python3-venv python3-pip git curl jq
ok "Packages ready"

# ── DHCP on the second NIC (sim-client network) ───────────────────────────────
# Port of installers/install-lxc.sh STEP 3: auto-detect a second NIC and run
# dnsmasq DHCP (169.253.1.0/24) on it for the isolated simulation-client
# network. Skipped on single-NIC hosts or when --no-dhcp / DHCP_SKIP=1.
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
        if (( NIC_COUNT >= 2 )); then
            DHCP_IFACE="${IFACES[1]}"
            ok "Second NIC detected: ${DHCP_IFACE} — DHCP will be configured"
        else
            warn "Only one NIC found — DHCP setup skipped"
        fi
    fi

    if [[ -n "$DHCP_IFACE" ]]; then
        echo "   Configuring DHCP on ${DHCP_IFACE} (${DHCP_GATEWAY}/${DHCP_PREFIX})"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q dnsmasq \
            -o Dpkg::Options::="--force-confdef" \
            -o Dpkg::Options::="--force-confold"
        ok "dnsmasq installed"

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

        # ── dnsmasq config scoped only to the internal interface ─────────────
        DNSMASQ_CONF="/etc/dnsmasq.d/client-sim.conf"
        cat >"$DNSMASQ_CONF" <<EOF
# Client-Sim isolated network DHCP — managed by install_cs.sh
# Only listen on the internal interface; never touches eth0 or other NICs
interface=${DHCP_IFACE}
bind-interfaces
except-interface=lo

# DHCP scope
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${DHCP_LEASE_TIME}

# Explicitly suppress default gateway — clients must not receive a router
# option. Sim clients route through their own WiFi/USB adapter; an injected
# gateway would override that and break traffic generation.
dhcp-option=option:router

# No DNS forwarding — isolated network has no upstream
port=0

# Lease file
dhcp-leasefile=/var/lib/misc/dnsmasq.leases

log-dhcp
EOF
        ok "dnsmasq config written (no default gateway advertised)"

        # Ensure dnsmasq's default config doesn't conflict with our interface
        if [[ -f /etc/dnsmasq.conf ]]; then
            sed -i 's/^#\?interface=.*$//' /etc/dnsmasq.conf 2>/dev/null || true
        fi

        # Systemd drop-in: wait for the DHCP interface to appear before dnsmasq
        # starts. Without this, dnsmasq fails with "unknown interface" on reboot
        # because the NIC attachment isn't ready yet. ExecStartPre reads the
        # interface name from the dnsmasq config at runtime, so it works
        # regardless of the interface name (net1, eth1, ens3, etc.).
        mkdir -p /etc/systemd/system/dnsmasq.service.d
        cat > /etc/systemd/system/dnsmasq.service.d/wait-for-interface.conf <<'DROPIN'
[Unit]
After=network.target network-online.target

[Service]
ExecStartPre=/bin/bash -c '\
  iface=$(grep "^interface=" /etc/dnsmasq.d/client-sim.conf 2>/dev/null | cut -d= -f2 | tr -d " \t"); \
  [ -z "$iface" ] && exit 0; \
  n=0; until ip link show "$iface" >/dev/null 2>&1; do \
    n=$((n+1)); [ $n -ge 30 ] && exit 1; sleep 1; \
  done'
Restart=on-failure
RestartSec=5
DROPIN
        systemctl daemon-reload >/dev/null 2>&1 || true
        ok "dnsmasq systemd drop-in written (waits for DHCP interface)"

        systemctl enable dnsmasq >/dev/null 2>&1 || true
        systemctl restart dnsmasq >/dev/null 2>&1 || true
        if systemctl is-active --quiet dnsmasq; then
            ok "dnsmasq running — DHCP active on ${DHCP_IFACE}"
        else
            warn "dnsmasq failed to start — check: journalctl -u dnsmasq"
        fi
    fi
fi

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SVC_USER" &>/dev/null; then
    useradd -r -s /bin/false -M "$SVC_USER"
    ok "Created service user $SVC_USER"
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

# ── Clone / update ────────────────────────────────────────────────────────────
step "Installing Generic Agent"
mkdir -p "$LM_DIR"
if [ -d "$LM_DIR/cs/.git" ]; then
    echo "   Updating existing install"
    git -C "$LM_DIR/cs" pull --rebase --autostash origin main -q
else
    echo "   Cloning CS / Generic Agent repo"
    git clone -q https://github.com/lbockenstedt/cs.git "$LM_DIR/cs"
fi

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
ok "Dependencies installed"

# ── .env ──────────────────────────────────────────────────────────────────────
cat > "$LM_DIR/cs/.env" <<DOTENV
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=${HUB_SECRET:-}
CS_API_PORT=$CS_API_PORT
CS_API_HOST=$CS_API_HOST
DOTENV
chmod 600 "$LM_DIR/cs/.env"

# ── systemd unit ──────────────────────────────────────────────────────────────

# Only pass --secret/--hub-secret when a value is present. Passing them with an
# empty value makes argparse abort with "argument --secret: expected one
# argument", crash-looping the service. Zero-touch provisioning handles the
# empty case (control_plane.py falls back to SPOKE_SECRET from the .env, then
# awaits admin approval in the WebUI).
SECRET_ARG=""
[ -n "$SPOKE_SECRET" ] && SECRET_ARG="--secret $SPOKE_SECRET"
HUB_SECRET_ARG=""
[ -n "${HUB_SECRET:-}" ] && HUB_SECRET_ARG="--hub-secret $HUB_SECRET"

cat > /etc/systemd/system/lm-cs.service <<SYSD
[Unit]
Description=Lab Manager Spoke - Generic Agent
After=network.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$LM_DIR/cs
EnvironmentFile=$LM_DIR/cs/.env
Environment="PYTHONPATH=$LM_DIR:$LM_DIR/core/src:$LM_DIR/cs/lm-spoke:$LM_DIR/cs/lm-spoke/src"
Environment="CS_API_PORT=$CS_API_PORT"
Environment="CS_API_HOST=$CS_API_HOST"
ExecStart=$LM_DIR/cs/venv/bin/python3 -m src.control_plane --id $SPOKE_ID --hub $HUB_URL $SECRET_ARG $HUB_SECRET_ARG --port $CS_API_PORT --host $CS_API_HOST

StandardOutput=append:/var/log/lm/lm-cs.log
StandardError=append:/var/log/lm/lm-cs.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
systemctl enable lm-cs
systemctl restart lm-cs
ok "Generic Agent service started"

chown -R "$SVC_USER:$SVC_USER" "$LM_DIR/cs" 2>/dev/null || true

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "this-host")
echo ""
echo "════════════════════════════════════════════"
ok "Generic Agent installation complete!"
echo "════════════════════════════════════════════"
echo "  LM Hub:       $HUB_URL"
echo "  Spoke ID:     $SPOKE_ID"
echo "  Version:      $(cat $LM_DIR/cs/VERSION 2>/dev/null || echo unknown)"
echo "  Status:       sudo systemctl status lm-cs"
if [[ -n "${DHCP_IFACE:-}" && "$DHCP_SKIP" != "1" ]]; then
    if systemctl is-active --quiet dnsmasq 2>/dev/null; then
        echo "  DHCP:          dnsmasq RUNNING on ${DHCP_IFACE} (${DHCP_RANGE_START}–${DHCP_RANGE_END})"
    else
        echo "  DHCP:          ${DHCP_IFACE} configured — dnsmasq NOT RUNNING (journalctl -u dnsmasq)"
    fi
else
    echo "  DHCP:          skipped (single NIC or --no-dhcp)"
fi
echo ""
