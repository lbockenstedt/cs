#!/usr/bin/env bash
###############################################################################
# Client-Sim — Proxmox LXC Provisioner  v0.01
#
# Run this script on the Proxmox HOST to:
#   1. Download the latest Debian LXC template
#   2. Create and configure the Client-Sim webUI LXC container
#      - eth0 on management bridge (internet / admin access)
#      - eth1 on vmbr255 (isolated client-sim network / DHCP server)
#
# Usage:
#   bash proxmox_create_lxc.sh [OPTIONS]
#
# Options:
#   --id      <CTID>       Container ID              (default: 1000)
#   --storage <pool>       Disk storage pool         (default: local-lvm)
#   --tmpl-storage <pool>  Template storage pool     (default: local)
#   --bridge  <bridge>     Management bridge (eth0)  (default: vmbr0)
#   --client-bridge <br>   Client network bridge     (default: vmbr255)
#   --hostname <name>      Container hostname        (default: client-sim)
#   --password <pw>        Root password             (default: prompted)
#   --cores   <n>          CPU cores                 (default: 2)
#   --memory  <MB>         RAM in MB                 (default: 1024)
#   --disk    <GB>         Root disk size in GB      (default: 8)
#   --ip      <CIDR>       eth0 static IP (CIDR) or 'dhcp' (default: dhcp)
#   --gw      <IP>         eth0 default gateway      (default: none)
#   --help                 Show this message
#
# Environment variable overrides (same names as flags, uppercased):
#   CTID, STORAGE, TMPL_STORAGE, MGMT_BRIDGE, CLIENT_BRIDGE, CT_HOSTNAME,
#   CT_PASSWORD, CT_CORES, CT_MEMORY, CT_DISK, CT_IP, CT_GW
#
# Examples:
#   # Minimal — accept all defaults, will prompt for password
#   bash proxmox_create_lxc.sh
#
#   # Full example
#   bash proxmox_create_lxc.sh \
#     --id 200 \
#     --storage local-lvm \
#     --bridge vmbr0 \
#     --hostname webui \
#     --ip 192.168.1.50/24 \
#     --gw 192.168.1.1 \
#     --memory 2048 \
#     --disk 16
###############################################################################

set -euo pipefail

###############################################################################
# Defaults (override via env vars or CLI flags)
###############################################################################
CTID="${CTID:-1000}"
STORAGE="${STORAGE:-local-lvm}"
TMPL_STORAGE="${TMPL_STORAGE:-local}"
MGMT_BRIDGE="${MGMT_BRIDGE:-vmbr0}"
CLIENT_BRIDGE="${CLIENT_BRIDGE:-vmbr255}"
CT_HOSTNAME="${CT_HOSTNAME:-client-sim}"
CT_PASSWORD="${CT_PASSWORD:-}"
CT_CORES="${CT_CORES:-2}"
CT_MEMORY="${CT_MEMORY:-1024}"
CT_DISK="${CT_DISK:-8}"
CT_IP="${CT_IP:-dhcp}"          # 'dhcp' or CIDR e.g. 192.168.1.50/24
CT_GW="${CT_GW:-}"              # leave blank for dhcp or if no gateway needed

###############################################################################
# Argument parsing
###############################################################################
usage() {
  grep '^#' "$0" | grep -v '#!/' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id)             CTID="$2";          shift 2 ;;
    --id=*)           CTID="${1#*=}";     shift ;;
    --storage)        STORAGE="$2";       shift 2 ;;
    --storage=*)      STORAGE="${1#*=}";  shift ;;
    --tmpl-storage)   TMPL_STORAGE="$2";  shift 2 ;;
    --tmpl-storage=*) TMPL_STORAGE="${1#*=}"; shift ;;
    --bridge)         MGMT_BRIDGE="$2";   shift 2 ;;
    --bridge=*)       MGMT_BRIDGE="${1#*=}"; shift ;;
    --client-bridge)  CLIENT_BRIDGE="$2"; shift 2 ;;
    --client-bridge=*)CLIENT_BRIDGE="${1#*=}"; shift ;;
    --hostname)       CT_HOSTNAME="$2";   shift 2 ;;
    --hostname=*)     CT_HOSTNAME="${1#*=}"; shift ;;
    --password)       CT_PASSWORD="$2";   shift 2 ;;
    --password=*)     CT_PASSWORD="${1#*=}"; shift ;;
    --cores)          CT_CORES="$2";      shift 2 ;;
    --cores=*)        CT_CORES="${1#*=}"; shift ;;
    --memory)         CT_MEMORY="$2";     shift 2 ;;
    --memory=*)       CT_MEMORY="${1#*=}"; shift ;;
    --disk)           CT_DISK="$2";       shift 2 ;;
    --disk=*)         CT_DISK="${1#*=}";  shift ;;
    --ip)             CT_IP="$2";         shift 2 ;;
    --ip=*)           CT_IP="${1#*=}";    shift ;;
    --gw)             CT_GW="$2";         shift 2 ;;
    --gw=*)           CT_GW="${1#*=}";    shift ;;
    --help|-h)        usage ;;
    *) echo "Unknown option: $1  (use --help)" >&2; exit 1 ;;
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

trap 'err "Script failed at line $LINENO"' ERR

###############################################################################
# Preflight checks
###############################################################################
if [[ $EUID -ne 0 ]]; then
  err "Run as root on the Proxmox host."
  exit 1
fi

if [[ ! -f /etc/pve/version ]]; then
  err "This script must be run on a Proxmox VE host."
  exit 1
fi

if ! command -v pct &>/dev/null; then
  err "pct not found — is this a Proxmox host?"
  exit 1
fi

# Prompt for password if not set
if [[ -z "$CT_PASSWORD" ]]; then
  echo -n "Root password for the new LXC: "
  read -rs CT_PASSWORD
  echo
  echo -n "Confirm password: "
  read -rs CT_PASSWORD2
  echo
  if [[ "$CT_PASSWORD" != "$CT_PASSWORD2" ]]; then
    err "Passwords do not match."
    exit 1
  fi
fi

###############################################################################
# Banner
###############################################################################
echo
echo "============================================================"
echo "  Client-Sim LXC Provisioner  v0.01"
echo "  $(date)"
echo "============================================================"
echo "  Container ID  : $CTID"
echo "  Hostname      : $CT_HOSTNAME"
echo "  Disk storage  : $STORAGE  (${CT_DISK}GB)"
echo "  Tmpl storage  : $TMPL_STORAGE"
echo "  CPU / RAM     : ${CT_CORES} cores / ${CT_MEMORY}MB"
echo "  eth0 bridge   : $MGMT_BRIDGE  ($CT_IP${CT_GW:+ gw $CT_GW})"
echo "  eth1 bridge   : $CLIENT_BRIDGE  (configured by install-lxc.sh)"
echo "============================================================"
echo

###############################################################################
# STEP 1 — Check CTID is free
###############################################################################
info "Checking container ID ${CTID}..."
if pct status "$CTID" &>/dev/null; then
  err "Container ${CTID} already exists. Choose a different --id."
  exit 1
fi
ok "CTID ${CTID} is available"

###############################################################################
# STEP 2 — Pull latest Debian LXC template
###############################################################################
info "Fetching latest Debian LXC template list from Proxmox..."

# Update the template list from the official Proxmox mirror
pveam update

# Find the latest debian-12 (Bookworm) template
TEMPLATE=$(pveam available --section system \
  | awk '/debian-12/ {print $2}' \
  | sort -V \
  | tail -1)

if [[ -z "$TEMPLATE" ]]; then
  # Fallback: try debian-11
  TEMPLATE=$(pveam available --section system \
    | awk '/debian-11/ {print $2}' \
    | sort -V \
    | tail -1)
fi

if [[ -z "$TEMPLATE" ]]; then
  err "Could not find a Debian template. Run 'pveam available' to check."
  exit 1
fi

info "Latest template: $TEMPLATE"

# Check if already downloaded
TMPL_PATH="${TMPL_STORAGE}:vztmpl/${TEMPLATE}"
if pveam list "$TMPL_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  ok "Template already downloaded — skipping download"
else
  info "Downloading ${TEMPLATE}..."
  pveam download "$TMPL_STORAGE" "$TEMPLATE"
  ok "Template downloaded"
fi

###############################################################################
# STEP 3 — Build pct create arguments
###############################################################################
info "Building container configuration..."

# eth0 network string
if [[ "$CT_IP" == "dhcp" ]]; then
  NET0="name=eth0,bridge=${MGMT_BRIDGE},ip=dhcp,firewall=0"
else
  NET0="name=eth0,bridge=${MGMT_BRIDGE},ip=${CT_IP}${CT_GW:+,gw=${CT_GW}},firewall=0"
fi

# eth1 — no IP set here; install-lxc.sh configures it
NET1="name=eth1,bridge=${CLIENT_BRIDGE},ip=manual,firewall=0"

###############################################################################
# STEP 4 — Create the LXC container
###############################################################################
info "Creating LXC container ${CTID}..."

pct create "$CTID" "${TMPL_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname    "$CT_HOSTNAME" \
  --password    "$CT_PASSWORD" \
  --cores       "$CT_CORES" \
  --memory      "$CT_MEMORY" \
  --swap        512 \
  --rootfs      "${STORAGE}:${CT_DISK}" \
  --net0        "$NET0" \
  --net1        "$NET1" \
  --ostype      debian \
  --unprivileged 1 \
  --features    nesting=1 \
  --start       0 \
  --onboot      1 \
  --startup     "order=1,up=30"

ok "Container ${CTID} created"

###############################################################################
# STEP 5 — Apply recommended settings
###############################################################################
info "Applying recommended settings..."

# Allow the container to run nested (needed for some tools)
pct set "$CTID" --features nesting=1

# Set timezone to match Proxmox host
HOST_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "UTC")
pct set "$CTID" --timezone "$HOST_TZ" 2>/dev/null || true

ok "Settings applied (timezone: ${HOST_TZ})"

###############################################################################
# STEP 6 — Start the container
###############################################################################
info "Starting container ${CTID}..."
pct start "$CTID"
sleep 5

if pct status "$CTID" | grep -q "running"; then
  ok "Container ${CTID} is running"
else
  err "Container ${CTID} failed to start — check: journalctl -xe"
  exit 1
fi

###############################################################################
# STEP 7 — Install prerequisites and run Client-Sim dashboard installer
###############################################################################
info "Running initial apt update inside container..."
pct exec "$CTID" -- bash -c "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl git
"
ok "Base packages installed in container"

info "Running Client-Sim dashboard installer inside container..."
pct exec "$CTID" -- bash -c "
  curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/installers/install-lxc.sh \
    | bash -s -- --branch main --port 8000
"
ok "Client-Sim dashboard installed"

###############################################################################
# Summary
###############################################################################
echo
echo "============================================================"
echo "  Container ready"
echo "============================================================"

CONTAINER_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

echo -e "  ${COL_GREEN}✓${COL_RESET}  CTID        : $CTID"
echo -e "  ${COL_GREEN}✓${COL_RESET}  Hostname    : $CT_HOSTNAME"
echo -e "  ${COL_GREEN}✓${COL_RESET}  IP (eth0)   : ${CONTAINER_IP}"
echo -e "  ${COL_GREEN}✓${COL_RESET}  eth1        : attached to ${CLIENT_BRIDGE} (no IP yet)"
echo -e "  ${COL_GREEN}✓${COL_RESET}  Auto-start  : enabled"
echo
echo "  Dashboard installer ran automatically — enter the container to check:"
echo -e "  ${COL_BOLD}pct enter ${CTID}${COL_RESET}"
echo "============================================================"
echo
