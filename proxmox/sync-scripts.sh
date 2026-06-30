#!/bin/bash
# sync-scripts.sh — Pull the latest proxmox scripts and config from GitHub and install them.
#
# Syncs all .sh and .conf files from the proxmox/ folder in the repo to
# /etc/pve/scripts/ so clone.sh, check_guest.sh, ini-parser.sh, and
# client-setup.conf are always current without manual copying.
#
# NOTE: /etc/pve is Proxmox's cluster filesystem (pmxcfs) and does not support
# chmod. Never set the execute bit on files here. Always invoke scripts with:
#   bash /etc/pve/scripts/<script>.sh
#
# Run once to install:
#   bash /etc/pve/scripts/sync-scripts.sh
#
# Add to cron for daily auto-update (runs at 2am):
#   0 2 * * * root bash /etc/pve/scripts/sync-scripts.sh >> /var/log/client-sim-sync.log 2>&1
#
# Configuration — edit these to match your environment
REPO_URL="https://github.com/lbockenstedt/cs.git"
# Default branch is main — override with REPO_BRANCH env var
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_CACHE="/opt/client-sim-repo"
SCRIPT_SRC="$REPO_CACHE/proxmox"          # folder inside the repo to install from
SCRIPT_DST="/etc/pve/scripts"             # where scripts live on the Proxmox host

set -euo pipefail

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Starting client-sim sync (branch: $REPO_BRANCH)"

# Install git if missing (Proxmox Debian base has it, but just in case)
if ! command -v git &>/dev/null; then
    log "git not found — installing..."
    apt-get install -y git -qq
fi

# First run: clone the repo
if [[ ! -d "$REPO_CACHE/.git" ]]; then
    log "No local repo found — cloning $REPO_URL"
    rm -rf "$REPO_CACHE"
    git clone --depth=1 -b "$REPO_BRANCH" "$REPO_URL" "$REPO_CACHE"
    log "Clone complete"
else
    # Subsequent runs: fetch and hard-reset to track branch tip exactly
    # Hard reset ensures local edits never block an update
    log "Updating existing repo..."
    git -C "$REPO_CACHE" fetch --depth=1 origin "$REPO_BRANCH"
    git -C "$REPO_CACHE" reset --hard "origin/$REPO_BRANCH"
    log "Repo updated to $(git -C "$REPO_CACHE" rev-parse --short HEAD)"
fi

# Sync all .sh and .conf files from proxmox/ into /etc/pve/scripts/
# .sh  — executable scripts (clone.sh, check_guest.sh, ini-parser.sh, etc.)
# .conf — config files (client-setup.conf with VM name→VMID mappings)
# cmp -s skips files that haven't changed so mtime noise is avoided.
# chmod +x is intentionally omitted — pmxcfs does not support execute permissions.
mkdir -p "$SCRIPT_DST"
updated=0
for f in "$SCRIPT_SRC"/*.sh "$SCRIPT_SRC"/*.conf; do
    [[ -f "$f" ]] || continue
    dest="$SCRIPT_DST/$(basename "$f")"
    if ! cmp -s "$f" "$dest" 2>/dev/null; then
        cp "$f" "$dest"
        log "Updated: $(basename "$f")"
        (( updated++ )) || true
    fi
done

[[ $updated -eq 0 ]] && log "All files already up to date" || log "$updated file(s) updated"
log "Sync complete"
