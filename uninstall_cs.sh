#!/bin/bash
# uninstall_cs.sh — Remove the Client-Sim (cs) spoke from this host.
#
# Reverses lm-spoke/install_cs.sh: stops+removes the lm-cs systemd unit,
# deletes the /opt/lm/cs install tree (repo + venv + .env), and (optionally)
# tears down the isolated sim-client DHCP network dnsmasq + 2nd-NIC config
# the installer provisioned, and/or deregisters the spoke from the hub.
#
# SAFETY: this NEVER removes assets shared with other LM services:
#   - the svc_lm system user        (cs, netbox, … share it)
#   - /opt/lm/core                  (base_spoke; pxmx/cs/hub all depend on it)
#   - /var/log/lm                   (shared log dir — only cs-specific logs go)
#   - the dnsmasq package           (only its cs config; --purge-dnsmasq to apt-purge)
# Re-running is safe (every removal is existence-guarded).
#
# Usage:
#   bash uninstall_cs.sh [--yes] [--purge-logs] [--remove-dhcp]
#                        [--purge-dnsmasq] [--hub-api <url>] [--spoke-id <id>]
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

# DHCP / sim-client network artifacts the installer may have created.
DNSMASQ_CONF="/etc/dnsmasq.d/client-sim.conf"
DNSMASQ_DROPIN_DIR="/etc/systemd/system/dnsmasq.service.d"
DNSMASQ_DROPIN="${DNSMASQ_DROPIN_DIR}/wait-for-interface.conf"
SYSCTL_CONF="/etc/sysctl.d/10-client-sim.conf"
INTERFACES_DIR="/etc/network/interfaces.d"

YES=0
PURGE_LOGS=0
REMOVE_DHCP=0
PURGE_DNSMASQ=0
HUB_API=""
SPOKE_ID=""
SPOKE_ID_SET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes)             YES=1; shift ;;
        --purge-logs)      PURGE_LOGS=1; shift ;;
        --remove-dhcp)     REMOVE_DHCP=1; shift ;;
        --purge-dnsmasq)   PURGE_DNSMASQ=1; REMOVE_DHCP=1; shift ;;
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
echo "  Remove DHCP  : $([[ $REMOVE_DHCP -eq 1 ]] && echo yes || echo no)"
echo "  Purge dnsmasq: $([[ $PURGE_DNSMASQ -eq 1 ]] && echo yes || echo no)"
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
    # Detect the DHCP interface from the installer's dnsmasq conf so we can
    # drop its static-IP interface file too. Best-effort.
    DHCP_IFACE=""
    if [[ -f "$DNSMASQ_CONF" ]]; then
        DHCP_IFACE=$(grep '^interface=' "$DNSMASQ_CONF" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' \t')
    fi

    for _f in "$DNSMASQ_CONF" "$SYSCTL_CONF" "$DNSMASQ_DROPIN"; do
        [[ -e "$_f" ]] && { rm -f "$_f" && echo "  Removed: $_f"; }
    done
    # Remove the empty drop-in dir only if we left it empty.
    if [[ -d "$DNSMASQ_DROPIN_DIR" ]] && [[ -z "$(ls -A "$DNSMASQ_DROPIN_DIR" 2>/dev/null)" ]]; then
        rmdir "$DNSMASQ_DROPIN_DIR" 2>/dev/null && echo "  Removed empty: $DNSMASQ_DROPIN_DIR"
    fi
    if [[ -n "$DHCP_IFACE" ]]; then
        IFACE_CFG="${INTERFACES_DIR}/${DHCP_IFACE}.conf"
        [[ -e "$IFACE_CFG" ]] && { rm -f "$IFACE_CFG" && echo "  Removed: $IFACE_CFG"; }
        # Release the static IP the installer set on the sim-client NIC.
        ip addr flush dev "$DHCP_IFACE" 2>/dev/null && echo "  Flushed addr on $DHCP_IFACE" || true
        ip link set "$DHCP_IFACE" down 2>/dev/null && echo "  Brought $DHCP_IFACE down" || true
    fi
    # Re-apply sysctl so the loose rp_filter setting clears.
    sysctl --system >/dev/null 2>&1 && echo "  sysctl reloaded" || echo "  WARNING: sysctl reload failed — reboot to clear rp_filter"

    if [[ $PURGE_DNSMASQ -eq 1 ]]; then
        systemctl stop dnsmasq 2>/dev/null || true
        systemctl disable dnsmasq 2>/dev/null || true
        DEBIAN_FRONTEND=noninteractive apt-get purge -y dnsmasq && echo "  Purged: dnsmasq package" \
            || echo "  WARNING: could not purge dnsmasq"
    else
        # Without the cs config, restart dnsmasq so it stops serving the
        # sim-client scope (leave the package installed).
        if systemctl is-active --quiet dnsmasq 2>/dev/null; then
            systemctl restart dnsmasq 2>/dev/null && echo "  Restarted dnsmasq (cs scope removed)" \
                || echo "  WARNING: dnsmasq restart failed — it may still serve stale config until reloaded"
        else
            echo "  dnsmasq not running — nothing to restart"
        fi
    fi
else
    echo "  Skipped DHCP/network teardown — pass --remove-dhcp (or --purge-dnsmasq) to remove"
    echo "    $DNSMASQ_CONF, $SYSCTL_CONF, $DNSMASQ_DROPIN, and the 2nd-NIC static-IP file"
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