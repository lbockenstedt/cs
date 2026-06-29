#!/bin/bash
# uninstall-proxmox-agent.sh — Remove the Client-Sim Proxmox agent from this host.
# Usage: bash uninstall-proxmox-agent.sh [--purge-logs] [--purge-pve-scripts] [--yes]

set -euo pipefail

AGENT_BIN="/usr/local/bin/client-sim-proxmox-agent"
WATCHDOG_BIN="/usr/local/bin/proxmox-watchdog"
SERVICE_NAME="client-sim-proxmox-agent"
ENV_FILE="/etc/client-sim-proxmox-agent.env"
SYSTEMD_DIR="/etc/systemd/system"
INSTALLER_DIR="/opt/proxmox-agent-installer"
WATCHDOG_STATE_DIR="/var/lib/proxmox-watchdog"
LOG_FILE="/var/log/client-sim-proxmox-agent.log"
SYSCTL_CONF="/etc/sysctl.d/99-client-sim-watchdog.conf"
MODULES_CONF="/etc/modules-load.d/client-sim-watchdog.conf"
PVE_SCRIPTS_DIR="/etc/pve/scripts"
PVE_SCRIPT_FILES=(
    "clone.sh"
    "ini-parser.sh"
    "check_guest.sh"
    "client-setup.conf"
    "sync-scripts.sh"
)

PURGE_LOGS=0
PURGE_PVE_SCRIPTS=0
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge-logs)        PURGE_LOGS=1; shift ;;
        --purge-pve-scripts) PURGE_PVE_SCRIPTS=1; shift ;;
        --yes)               YES=1; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo "=== Client-Sim Proxmox Agent Uninstaller ==="
echo "  Agent bin     : $AGENT_BIN"
echo "  Env file      : $ENV_FILE"
echo "  Installer dir : $INSTALLER_DIR"
echo "  Watchdog state: $WATCHDOG_STATE_DIR"
echo "  Purge logs    : $([[ $PURGE_LOGS -eq 1 ]] && echo yes || echo no)"
echo "  Purge PVE scripts: $([[ $PURGE_PVE_SCRIPTS -eq 1 ]] && echo yes || echo no)"
echo

if [[ $YES -eq 0 ]]; then
    read -rp "Proceed with uninstall? [y/N]: " _confirm
    [[ "$_confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

echo "[1/6] Stopping and disabling services..."
for _unit in proxmox-watchdog.timer proxmox-watchdog.service "$SERVICE_NAME"; do
    if systemctl is-active --quiet "$_unit" 2>/dev/null; then
        systemctl stop "$_unit" && echo "  Stopped: $_unit" || echo "  WARNING: could not stop $_unit"
    fi
    if systemctl is-enabled --quiet "$_unit" 2>/dev/null; then
        systemctl disable "$_unit" && echo "  Disabled: $_unit" || echo "  WARNING: could not disable $_unit"
    fi
done

echo "[2/6] Removing systemd unit files..."
for _unit_file in \
    "${SYSTEMD_DIR}/${SERVICE_NAME}.service" \
    "${SYSTEMD_DIR}/proxmox-agent.service" \
    "${SYSTEMD_DIR}/proxmox-watchdog.service" \
    "${SYSTEMD_DIR}/proxmox-watchdog.timer"
do
    if [[ -e "$_unit_file" ]]; then
        rm -f "$_unit_file" && echo "  Removed: $_unit_file"
    fi
done
systemctl daemon-reload
echo "  OK: systemd daemon reloaded"

echo "[3/6] Removing binaries and environment file..."
for _f in "$AGENT_BIN" "$WATCHDOG_BIN" "$ENV_FILE"; do
    if [[ -e "$_f" ]]; then
        rm -f "$_f" && echo "  Removed: $_f"
    fi
done

echo "[4/6] Removing installer and watchdog state directories..."
if [[ -d "$INSTALLER_DIR" ]]; then
    rm -rf "$INSTALLER_DIR" && echo "  Removed: $INSTALLER_DIR"
fi
if [[ -d "$WATCHDOG_STATE_DIR" ]]; then
    rm -rf "$WATCHDOG_STATE_DIR" && echo "  Removed: $WATCHDOG_STATE_DIR"
fi

echo "[5/6] Removing crash-hardening config..."
if [[ -f "$SYSCTL_CONF" ]]; then
    rm -f "$SYSCTL_CONF" && echo "  Removed: $SYSCTL_CONF"
    sysctl --system >/dev/null 2>&1 && echo "  OK: sysctl reloaded" \
        || echo "  WARNING: sysctl reload failed — reboot to clear kernel settings"
fi
if [[ -f "$MODULES_CONF" ]]; then
    rm -f "$MODULES_CONF" && echo "  Removed: $MODULES_CONF"
fi
if lsmod 2>/dev/null | grep -q '^softdog'; then
    rmmod softdog 2>/dev/null && echo "  Unloaded: softdog kernel module" \
        || echo "  WARNING: could not unload softdog — it will be absent after next reboot"
fi

echo "[6/6] Optional cleanup..."
if [[ $PURGE_LOGS -eq 1 ]]; then
    if [[ -f "$LOG_FILE" ]]; then
        rm -f "$LOG_FILE" && echo "  Removed: $LOG_FILE"
    fi
else
    echo "  Skipped log removal — pass --purge-logs to delete $LOG_FILE"
fi

if [[ $PURGE_PVE_SCRIPTS -eq 1 ]]; then
    if [[ -d "$PVE_SCRIPTS_DIR" ]]; then
        for _pve_file in "${PVE_SCRIPT_FILES[@]}"; do
            _pve_dest="${PVE_SCRIPTS_DIR}/${_pve_file}"
            if [[ -e "$_pve_dest" ]]; then
                rm -f "$_pve_dest" && echo "  Removed: $_pve_dest"
            fi
        done
    fi
else
    echo "  Skipped PVE scripts — pass --purge-pve-scripts to remove files in $PVE_SCRIPTS_DIR"
fi

echo
echo "=== Uninstall complete ==="
echo "  The agent is no longer installed on this host."
echo "  No VMs or LXC containers were modified."
