#!/usr/bin/env bash
###############################################################################
# Client-Sim — Proxmox Host Setup  v0.02
#
# Run this script directly on the Proxmox host (not inside an LXC).
#
# What it does:
#   - Creates vmbr255: an internal Linux bridge with no uplink
#     Used as an isolated client-sim network. LXC containers and VMs
#     attached to vmbr255 can only talk to each other and to the
#     Client-Sim webUI LXC (which will have a second NIC on this bridge).
#   - Installs git
#   - Pulls the latest proxmox/ scripts from GitHub into /etc/pve/scripts/
#   - Creates a daily cron job to keep scripts up to date automatically
#
# Usage:
#   sudo bash proxmox_setup.sh [--branch <name>]
#
# Configuration can be overridden via environment variable or CLI flag:
#   BRIDGE=vmbr100 bash proxmox_setup.sh
#   bash proxmox_setup.sh --branch main
#   REPO_BRANCH=main bash proxmox_setup.sh
#
# Requirements:
#   - Proxmox VE 7 or 8 (Debian-based)
#   - Run as root on the Proxmox host itself
###############################################################################

set -euo pipefail

###############################################################################
# Configuration — override via environment variables if needed
###############################################################################
BRIDGE="${BRIDGE:-vmbr255}"
BRIDGE_COMMENT="${BRIDGE_COMMENT:-Client-Sim internal isolated network}"
INTERFACES_FILE="/etc/network/interfaces"

# Script sync configuration
# Default branch is main — override with --branch or REPO_BRANCH env var
REPO_URL="https://github.com/lbockenstedt/cs.git"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_CACHE="/opt/client-sim-repo"
SCRIPT_DST="/etc/pve/scripts"
CRON_FILE="/etc/cron.d/client-sim-sync"
SYNC_LOG="/var/log/client-sim-sync.log"

###############################################################################
# CLI argument parsing — --branch overrides REPO_BRANCH env var
###############################################################################
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      [[ -z "${2:-}" ]] && { echo "ERROR: --branch requires a value" >&2; exit 1; }
      REPO_BRANCH="$2"
      shift 2
      ;;
    --bridge)
      [[ -z "${2:-}" ]] && { echo "ERROR: --bridge requires a value" >&2; exit 1; }
      BRIDGE="$2"
      shift 2
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      echo "Usage: bash proxmox_setup.sh [--branch <name>] [--bridge <name>]" >&2
      exit 1
      ;;
  esac
done

###############################################################################
# Colours & logging
###############################################################################
COL_RESET="\033[0m"
COL_GREEN="\033[0;32m"
COL_YELLOW="\033[1;33m"
COL_RED="\033[0;31m"
COL_BOLD="\033[1m"

ts()   { date "+%H:%M:%S"; }
info() { echo -e "[$(ts)] ${COL_BOLD}INFO${COL_RESET}  $*"; }
ok()   { echo -e "[$(ts)] ${COL_GREEN}OK${COL_RESET}    $*"; }
warn() { echo -e "[$(ts)] ${COL_YELLOW}WARN${COL_RESET}  $*"; }
err()  { echo -e "[$(ts)] ${COL_RED}ERR${COL_RESET}   $*" >&2; }

###############################################################################
# Preflight checks
###############################################################################
echo
echo "============================================================"
echo "  Client-Sim Proxmox Setup  v0.02"
echo "  $(date)"
echo "============================================================"
echo "  Bridge    : $BRIDGE"
echo "  Comment   : $BRIDGE_COMMENT"
echo "  Config    : $INTERFACES_FILE"
echo "  Repo      : $REPO_URL  ($REPO_BRANCH)"
echo "  Scripts   : $SCRIPT_DST"
echo "  Sync cron : $CRON_FILE  (daily 2am)"
echo "============================================================"
echo

if [[ $EUID -ne 0 ]]; then
  err "This script must be run as root."
  exit 1
fi

if [[ ! -f /etc/pve/version ]]; then
  err "This script must be run on a Proxmox VE host."
  exit 1
fi

###############################################################################
# STEP 1 — Check if bridge already exists
###############################################################################
info "Checking if ${BRIDGE} already exists..."

if grep -qE "^iface ${BRIDGE} " "$INTERFACES_FILE" 2>/dev/null; then
  warn "${BRIDGE} is already defined in ${INTERFACES_FILE} — skipping creation."
  echo
  echo "Current definition:"
  grep -A8 "iface ${BRIDGE}" "$INTERFACES_FILE" || true
  echo
else
  ###############################################################################
  # STEP 2 — Append bridge definition to /etc/network/interfaces
  ###############################################################################
  info "Adding ${BRIDGE} to ${INTERFACES_FILE}..."

  # Backup interfaces file first
  cp "$INTERFACES_FILE" "${INTERFACES_FILE}.bak.$(date +%Y%m%d%H%M%S)"
  ok "Backed up ${INTERFACES_FILE}"

  cat >> "$INTERFACES_FILE" <<EOF

# ${BRIDGE_COMMENT}
auto ${BRIDGE}
iface ${BRIDGE} inet manual
        bridge-ports none
        bridge-stp off
        bridge-fd 0
        bridge-maxwait 0
# END ${BRIDGE}
EOF

  ok "${BRIDGE} definition added to ${INTERFACES_FILE}"
fi

###############################################################################
# STEP 3 — Bring the bridge up now without rebooting
###############################################################################
info "Bringing up ${BRIDGE}..."

if ip link show "$BRIDGE" &>/dev/null; then
  ok "${BRIDGE} is already up"
else
  if command -v ifup &>/dev/null; then
    ifup "$BRIDGE" 2>/dev/null && ok "${BRIDGE} brought up with ifup" || {
      warn "ifup failed — trying ip link directly"
      ip link add name "$BRIDGE" type bridge
      ip link set "$BRIDGE" up
      ok "${BRIDGE} brought up with ip link"
    }
  else
    ip link add name "$BRIDGE" type bridge
    ip link set "$BRIDGE" up
    ok "${BRIDGE} brought up with ip link"
  fi
fi

###############################################################################
# STEP 4 — Verify bridge is up
###############################################################################
info "Verifying ${BRIDGE}..."

if ip link show "$BRIDGE" &>/dev/null; then
  STATE=$(ip link show "$BRIDGE" | grep -oE 'state \S+' | awk '{print $2}')
  ok "${BRIDGE} is up (state: ${STATE})"
else
  err "${BRIDGE} was not brought up — check ${INTERFACES_FILE} manually"
fi

###############################################################################
# STEP 5 — Install git
###############################################################################
info "Checking for git..."
if command -v git &>/dev/null; then
  ok "git already installed ($(git --version))"
else
  info "Installing git..."
  apt-get update -qq
  apt-get install -y git -qq
  ok "git installed ($(git --version))"
fi

###############################################################################
# STEP 6 — Pull proxmox scripts from GitHub into /etc/pve/scripts/
#
# NOTE: /etc/pve is Proxmox's cluster filesystem (pmxcfs).
# It does not support execute permissions — never chmod +x files here.
# Always invoke scripts with: bash /etc/pve/scripts/<name>.sh
###############################################################################
info "Syncing scripts from GitHub ($REPO_BRANCH branch)..."
mkdir -p "$SCRIPT_DST"

if [[ ! -d "$REPO_CACHE/.git" ]]; then
  info "Cloning repo for the first time..."
  rm -rf "$REPO_CACHE"
  git clone --depth=1 -b "$REPO_BRANCH" "$REPO_URL" "$REPO_CACHE"
  ok "Repo cloned"
else
  info "Updating existing repo..."
  git -C "$REPO_CACHE" fetch --depth=1 origin "$REPO_BRANCH"
  git -C "$REPO_CACHE" reset --hard "origin/$REPO_BRANCH"
  ok "Repo updated to $(git -C "$REPO_CACHE" rev-parse --short HEAD)"
fi

updated=0
for f in "$REPO_CACHE/proxmox"/*.sh; do
  [[ -f "$f" ]] || continue
  dest="$SCRIPT_DST/$(basename "$f")"
  if ! cmp -s "$f" "$dest" 2>/dev/null; then
    cp "$f" "$dest"
    ok "Installed: $(basename "$f")"
    (( updated++ )) || true
  fi
done
[[ $updated -eq 0 ]] && ok "All scripts already up to date" || ok "$updated script(s) installed to $SCRIPT_DST"

###############################################################################
# STEP 7 — Create daily cron job to keep scripts in sync
###############################################################################
info "Setting up daily sync cron job..."

cat > "$CRON_FILE" <<EOF
# Client-Sim script sync — pulls latest proxmox/ scripts from GitHub daily
# Branch: $REPO_BRANCH  |  Repo: $REPO_URL
# To run manually: bash $SCRIPT_DST/sync-scripts.sh
0 2 * * * root bash $SCRIPT_DST/sync-scripts.sh >> $SYNC_LOG 2>&1
EOF

ok "Cron job created: $CRON_FILE (runs daily at 2am)"
ok "Sync log: $SYNC_LOG"

###############################################################################
# Summary
###############################################################################
echo
echo "============================================================"
echo "  Setup Complete"
echo "============================================================"
if ip link show "$BRIDGE" &>/dev/null; then
  STATE=$(ip link show "$BRIDGE" | grep -oE 'state \S+' | awk '{print $2}')
  echo -e "  ${COL_GREEN}✓${COL_RESET}  ${BRIDGE} bridge   (state: ${STATE})"
else
  echo -e "  ${COL_RED}✗${COL_RESET}  ${BRIDGE} NOT found"
fi
if grep -q "auto ${BRIDGE}" "$INTERFACES_FILE"; then
  echo -e "  ${COL_GREEN}✓${COL_RESET}  ${BRIDGE} in ${INTERFACES_FILE}"
else
  echo -e "  ${COL_RED}✗${COL_RESET}  ${BRIDGE} NOT in ${INTERFACES_FILE}"
fi
echo -e "  ${COL_GREEN}✓${COL_RESET}  Scripts synced to $SCRIPT_DST"
echo -e "  ${COL_GREEN}✓${COL_RESET}  Daily cron job active ($CRON_FILE)"
echo
echo "  Next steps:"
echo "  1. In Proxmox UI → System → Network, confirm ${BRIDGE} is visible"
echo "  2. Attach the Client-Sim webUI LXC to ${BRIDGE} as a second NIC"
echo "  3. Run:  bash $SCRIPT_DST/install-lxc.sh"
echo "  4. Attach client VMs/LXCs to ${BRIDGE} and set them to DHCP"
echo "============================================================"
echo
