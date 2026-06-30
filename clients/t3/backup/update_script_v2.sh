#!/bin/bash
# update_script.sh — Self-updater for the wireless simulation
# Brings up the wired interface to pull the latest scripts from GitHub,
# then hands back to wireless.sh via exec (no stack growth).

LOGFILE="/usr/scripts/wireless.log"
SCRIPT_DIR="/usr/scripts"
GITHUB_BASE="https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/t3"

log() { echo "$1" | tee -a "$LOGFILE"; }

log "=== update_script.sh — $(date) ==="

# ── Bring up wired interface for the download ─────────────────────────────────
log "Bringing up wired interface for script update"
sudo ifconfig enp6s18 up
sleep 15
sudo dhcpcd enp6s18

# Low metric so wired is preferred for the download over any active WLAN route
sudo ifmetric enp6s18 10

# ── Download latest scripts to /tmp first (safe staging) ─────────────────────
log "Downloading latest scripts from GitHub"
sudo wget -q "$GITHUB_BASE/wireless.sh"      -O /tmp/wireless.sh
sudo wget -q "$GITHUB_BASE/update_script.sh" -O /tmp/update_script.sh

# ── Validate and move — skip any file that downloaded as 0 bytes ──────────────
for script in wireless.sh update_script.sh; do
    if [ -s "/tmp/$script" ]; then
        sudo mv -f "/tmp/$script" "$SCRIPT_DIR/$script"
        sudo chmod 755 "$SCRIPT_DIR/$script"
        log "Updated $script"
    else
        sudo rm -f "/tmp/$script"
        log "Skipping $script — download failed or empty, keeping existing version"
    fi
done

# ── Take wired interface back down — simulation traffic must go out WLAN ──────
sudo ifconfig enp6s18 down

# ── Clean up any leftover files from simulation downloads ────────────────────
rm -f www.* 2>/dev/null

# ── Hand off to wireless.sh — exec replaces this process (no stack growth) ───
log "Restarting wireless simulation — $(date)"
exec bash "$SCRIPT_DIR/wireless.sh"