#!/bin/bash
# uninstall_cs.sh — Remove the Client-Sim (cs) spoke from this host.
#
# Reverses lm-spoke/install_cs.sh: stops+removes the lm-cs systemd unit,
# deletes the /opt/lm/cs install tree (repo + venv + .env), and (optionally)
# tears down the isolated sim-client DHCP network (the cs-owned Kea instance
# kea-dhcp4-sim + 2nd-NIC config) the installer provisioned, and/or deregisters
# the spoke from the hub.
#
# SAFETY: this NEVER removes assets shared with other LM services:
#   - the svc_lm system user        (cs, netbox, … share it)
#   - /opt/lm/core                  (base_spoke; pxmx/cs/hub all depend on it)
#   - /var/log/lm                   (shared log dir — only cs-specific logs go)
#   - the kea-dhcp4-server/kea-ctrl-agent packages AND the dhcp module's default
#     kea-dhcp4-server.service / kea-ctrl-agent.service (the lm/dhcp module shares
#     the packages; only the cs-owned -sim instance is removed, never apt-purged)
# Re-running is safe (every removal is existence-guarded).
#
# Usage:
#   bash uninstall_cs.sh [--yes] [--purge-logs] [--remove-dhcp]
#                        [--hub-api <url>] [--spoke-id <id>]
#
# One-liner from GitHub:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/uninstall_cs.sh | sudo bash
#   # …then, to also tear down DHCP + deregister from the hub:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/uninstall_cs.sh | sudo bash -s -- --yes --remove-dhcp --hub-api http://localhost:8000 --spoke-id cs-spoke-1

set -euo pipefail

LM_DIR="/opt/lm"
CS_DIR="$LM_DIR/cs"
SERVICE="lm-cs"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
CS_LOGS=(/var/log/lm/lm-cs.log /var/log/lm/lm-cs-install.log)
SHARED_CORE="$LM_DIR/core"

# DHCP / sim-client network artifacts the installer may have created — the
# cs-OWNED Kea instance (kea-dhcp4-sim), distinct from the dhcp module's default.
KEA_DHCP4_CONF="/etc/kea/kea-dhcp4-sim.conf"
KEA_CA_CONF="/etc/kea/kea-ctrl-agent-sim.conf"
KEA_LEASES="/var/lib/kea/kea-leases4-sim.csv"
KEA_DHCP4_UNIT="/etc/systemd/system/kea-dhcp4-sim.service"
KEA_CA_UNIT="/etc/systemd/system/kea-ctrl-agent-sim.service"
SYSCTL_CONF="/etc/sysctl.d/10-client-sim.conf"
INTERFACES_DIR="/etc/network/interfaces.d"

YES=0
PURGE_LOGS=0
REMOVE_DHCP=0
HUB_API=""
SPOKE_ID=""
SPOKE_ID_SET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)             YES=1; shift ;;
        --purge-logs)      PURGE_LOGS=1; shift ;;
        --remove-dhcp)     REMOVE_DHCP=1; shift ;;
        --hub-api)         HUB_API="$2"; shift 2 ;;
        --spoke-id)        SPOKE_ID="$2"; SPOKE_ID_SET=1; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "❌ Run as root (sudo)."; exit 1; }

# Auto-detect the installed spoke id from the cs .env so the uninstall targets
# the RIGHT spoke for the pkill + hub deregister — the installer defaults the
# id to `cs-$(hostname -s)` when no --id is supplied (curl one-liner installs),
# while install_all.sh pins it to cs-spoke-1. Only fall back to cs-spoke-1 if
# the .env is gone and --spoke-id wasn't passed.
if [[ $SPOKE_ID_SET -eq 0 ]]; then
    _ENV="$CS_DIR/.env"
    if [[ -f "$_ENV" ]]; then
        SPOKE_ID=$(grep '^SPOKE_ID=' "$_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
    fi
    [[ -z "$SPOKE_ID" ]] && SPOKE_ID="cs-spoke-1"
fi

echo "=== Client-Sim (cs) Spoke Uninstaller ==="
echo "  Service      : $SERVICE ($UNIT_FILE)"
echo "  Install tree : $CS_DIR"
echo "  Purge logs   : $([[ $PURGE_LOGS -eq 1 ]] && echo yes || echo no)"
echo "  Remove DHCP  : $([[ $REMOVE_DHCP -eq 1 ]] && echo yes || echo no)  (cs-owned Kea kea-dhcp4-sim; packages kept — shared with dhcp module)"
echo "  Hub deregister: $([[ -n $HUB_API ]] && echo "$HUB_API (spoke=$SPOKE_ID)" || echo no)"
echo "  PRESERVED (shared): svc_lm user, $SHARED_CORE, /var/log/lm dir"
echo

if [[ $YES -eq 0 ]] && [ -t 0 ]; then
    # Interactive TTY: confirm before destroying anything.
    read -rp "Proceed with uninstall? [y/N]: " _confirm
    [[ "$_confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi
# Non-TTY stdin (e.g. `curl ... | bash`) proceeds without a prompt — the pipe
# is consent — so the documented one-liner works without an extra --yes.

# ── [1] Stop + disable the cs spoke service ───────────────────────────────
echo "[1/5] Stopping and disabling $SERVICE..."
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl stop "$SERVICE" && echo "  Stopped: $SERVICE" || echo "  WARNING: could not stop $SERVICE"
fi
if systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
    systemctl disable "$SERVICE" && echo "  Disabled: $SERVICE" || echo "  WARNING: could not disable $SERVICE"
fi
# Reap any stray control_plane process for this spoke (mirror install_all.sh).
pkill -f "control_plane.*--id ${SPOKE_ID}" 2>/dev/null || true

# ── [2] Remove the systemd unit ─────────────────────────────────────────────
echo "[2/5] Removing systemd unit..."
if [[ -e "$UNIT_FILE" ]]; then
    rm -f "$UNIT_FILE" && echo "  Removed: $UNIT_FILE"
fi
systemctl daemon-reload && echo "  systemd daemon reloaded"

# ── [3] Remove the cs install tree ─────────────────────────────────────────
echo "[3/5] Removing cs install tree..."
if [[ -d "$CS_DIR" ]]; then
    rm -rf "$CS_DIR" && echo "  Removed: $CS_DIR"
else
    echo "  $CS_DIR not present (already removed)"
fi
# Deliberately NOT touching $SHARED_CORE or the svc_lm user — other LM
# services (pxmx/netbox/…) depend on them.

# ── [4] Logs ───────────────────────────────────────────────────────────────
echo "[4/5] Logs..."
if [[ $PURGE_LOGS -eq 1 ]]; then
    for _log in "${CS_LOGS[@]}"; do
        [[ -e "$_log" ]] && { rm -f "$_log" && echo "  Removed: $_log"; }
    done
else
    echo "  Kept cs logs (${CS_LOGS[*]}). Pass --purge-logs to delete them."
fi

# ── [5] Optional: DHCP / sim-client network + hub deregister ───────────────
echo "[5/5] Optional cleanup..."

if [[ $REMOVE_DHCP -eq 1 ]]; then
    # Detect the DHCP interface from the cs kea-dhcp4-sim JSON conf so we can
    # drop its static-IP interface file too. Best-effort.
    DHCP_IFACE=""
    if [[ -f "$KEA_DHCP4_CONF" ]]; then
        DHCP_IFACE=$(python3 -c "import json,sys;print((json.load(open('$KEA_DHCP4_CONF')).get('Dhcp4',{}).get('interfaces-config',{}).get('interfaces') or [''])[0].split('/')[0])" 2>/dev/null || true)
    fi

    # Stop + disable ONLY the cs-owned -sim units (never the dhcp module's
    # default kea-dhcp4-server.service / kea-ctrl-agent.service).
    for _svc in kea-ctrl-agent-sim kea-dhcp4-sim; do
        systemctl is-active   --quiet "$_svc" 2>/dev/null && { systemctl stop    "$_svc" 2>/dev/null && echo "  Stopped: $_svc"; }
        systemctl is-enabled  --quiet "$_svc" 2>/dev/null && { systemctl disable "$_svc" 2>/dev/null && echo "  Disabled: $_svc"; }
    done

    for _f in "$KEA_DHCP4_CONF" "$KEA_CA_CONF" "$KEA_LEASES" "$KEA_DHCP4_UNIT" "$KEA_CA_UNIT" "$SYSCTL_CONF"; do
        [[ -e "$_f" ]] && { rm -f "$_f" && echo "  Removed: $_f"; }
    done
    systemctl daemon-reload 2>/dev/null && echo "  systemd daemon reloaded" || true

    if [[ -n "$DHCP_IFACE" ]]; then
        IFACE_CFG="${INTERFACES_DIR}/${DHCP_IFACE}.conf"
        [[ -e "$IFACE_CFG" ]] && { rm -f "$IFACE_CFG" && echo "  Removed: $IFACE_CFG"; }
        # Release the static IP the installer set on the sim-client NIC.
        ip addr flush dev "$DHCP_IFACE" 2>/dev/null && echo "  Flushed addr on $DHCP_IFACE" || true
        ip link set "$DHCP_IFACE" down 2>/dev/null && echo "  Brought $DHCP_IFACE down" || true
    fi
    # Re-apply sysctl so the loose rp_filter setting clears.
    sysctl --system >/dev/null 2>&1 && echo "  sysctl reloaded" || echo "  WARNING: sysctl reload failed — reboot to clear rp_filter"
    echo "  Kea packages LEFT installed (shared with the lm/dhcp module)."
else
    echo "  Skipped DHCP/network teardown — pass --remove-dhcp to remove"
    echo "    $KEA_DHCP4_CONF, $KEA_CA_CONF, $KEA_LEASES, the -sim units, $SYSCTL_CONF, and the 2nd-NIC static-IP file"
fi

if [[ -n "$HUB_API" ]]; then
    echo "  Deregistering spoke '$SPOKE_ID' from hub at $HUB_API ..."
    # DELETE /setup/spokes/{spoke_id} — mirrors the unauthenticated pre-approve
    # install_all.sh uses. Best-effort: a down hub or 401 just logs a warning.
    if curl -sf -X DELETE "${HUB_API}/setup/spokes/${SPOKE_ID}" >/dev/null 2>&1; then
        echo "  Deregistered: $SPOKE_ID"
    else
        echo "  WARNING: could not deregister $SPOKE_ID (hub unreachable / not found / auth) — remove it manually in Setup → Spokes"
    fi
fi

echo
echo "=== Uninstall complete ==="
echo "  The cs spoke is no longer installed on this host."
echo "  Preserved: svc_lm user, $SHARED_CORE, /var/log/lm directory."
echo "  Reinstall: curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh | sudo bash -s -- --hub <ws://hub:8765> --id $SPOKE_ID"