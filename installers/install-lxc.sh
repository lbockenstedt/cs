#!/usr/bin/env bash
###############################################################################
# Client Simulator — LXC Installer v0.03
#
# Usage:
#   sudo bash install-lxc.sh              # install or update in-place
#   sudo bash install-lxc.sh --reinstall  # full wipe and reinstall
#
# Environment variable overrides (set before running):
#   REPO_URL, REPO_BRANCH, INSTALL_DIR, REPO_CACHE, SERVICE_USER, PORT,
#   OFFLINE_TIMEOUT, DHCP_IFACE, DHCP_SUBNET, DHCP_GATEWAY, DHCP_RANGE_START,
#   DHCP_RANGE_END, DHCP_LEASE_TIME
#
# DHCP (dnsmasq on vmbr255 second NIC):
#   Set DHCP_IFACE="" to skip DHCP setup entirely.
#   The interface must already be attached to the LXC in Proxmox.
###############################################################################

set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

###############################################################################
# Flags
###############################################################################
REINSTALL=0
FORCE=0
UNATTENDED=0
CLI_BRANCH=""
CLI_PORT=""
ADMIN_PASSWORD_ARG=""
HUB_URL_ARG=""
HUB_TENANT_ARG=""
HUB_USER_ARG=""
HUB_PASS_ARG=""
HUB_PSK_ARG=""
# LM hub relay (combined Client-Sim spoke). When --lm-hub-url is given, the
# spoke connects to the LM hub (lm/core) as module_type "Client-Sim" instead of
# the legacy webui-hub relay. --lm-core-path tells the service where the lm
# repo root (the dir containing core/) lives so `core.src.*` imports resolve.
LM_HUB_URL_ARG=""
LM_HUB_SECRET_ARG=""
LM_CORE_PATH="${LM_CORE_PATH:-/opt/lm}"

usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --branch <name>             Git branch to sync from (overrides REPO_BRANCH env var)
  --port   <number>           TCP port to serve on    (overrides PORT env var)
  --admin-password <value>    Spoke admin password written to .env (default: none — open access)
  --hub-url <url>             Hub URL to auto-configure after install (e.g. https://cs-hub.westus3.azurecontainer.io:8443)
  --hub-tenant <id-or-name>   Hub tenant ID or name (e.g. contoso or contoso.onmicrosoft.com)
  --hub-user <username>       Hub admin username (used to resolve tenant name → ID)
  --hub-password <password>   Hub admin password (used to resolve tenant name → ID)
  --hub-psk <token>           Onboarding PSK for auto-approval (bypasses manual approval in hub)
  --lm-hub-url <ws-url>       Relay to the Lab Manager hub (lm/core) as a combined Client-Sim
                              spoke (module_type "Client-Sim") instead of the legacy webui-hub
                              relay. Example: ws://hub:8765  (enables lm_hub_enabled in settings.json)
  --lm-hub-secret <secret>   Hub mutual-auth secret (HUB_SECRET) for the LM relay
  --lm-core-path <dir>        Directory containing the lm repo's core/ package so `core.src.*`
                              imports resolve in the combined spoke (default: /opt/lm)
  --reinstall                 Full wipe and fresh install (default: safe in-place update)
  --force                     Like --reinstall but also clears hub config so --hub-url/--hub-tenant re-apply
  --unattended                Non-interactive mode (accepted for automation/watchdog)
  --help                      Show this message

Examples:
  sudo bash install-lxc.sh
  sudo bash install-lxc.sh --branch main --port 9000
  sudo bash install-lxc.sh --admin-password 'MySecret123!'
  sudo bash install-lxc.sh --reinstall --branch main
  sudo bash install-lxc.sh --force --hub-url https://cs-hub.westus3.azurecontainer.io:8443 --hub-tenant ssplm
  sudo bash install-lxc.sh --hub-url https://cs-hub.westus3.azurecontainer.io:8443 --hub-tenant "My Tenant" --hub-user admin --hub-password MySecret123!
  sudo bash install-lxc.sh --hub-url https://cs-hub.westus3.azurecontainer.io:8443 --hub-tenant contoso.onmicrosoft.com
  sudo bash install-lxc.sh --hub-url https://cs-hub.westus3.azurecontainer.io:8443 --hub-tenant caf117e2-a73d-4439-a759-ecc629158954
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall|-r)   REINSTALL=1;              shift ;;
    --force|-f)       FORCE=1; REINSTALL=1;     shift ;;
    --unattended)     UNATTENDED=1;             shift ;;
    --branch=*)       CLI_BRANCH="${1#*=}";          shift ;;
    --branch|-b)      CLI_BRANCH="${2:-}";           shift 2 ;;
    --port=*)         CLI_PORT="${1#*=}";            shift ;;
    --port|-p)        CLI_PORT="${2:-}";             shift 2 ;;
    --admin-password) ADMIN_PASSWORD_ARG="${2:-}";   shift 2 ;;
    --hub-url=*)      HUB_URL_ARG="${1#*=}";         shift ;;
    --hub-url)        HUB_URL_ARG="${2:-}";          shift 2 ;;
    --hub-tenant=*)   HUB_TENANT_ARG="${1#*=}";      shift ;;
    --hub-tenant)     HUB_TENANT_ARG="${2:-}";       shift 2 ;;
    --hub-user=*)     HUB_USER_ARG="${1#*=}";        shift ;;
    --hub-user)       HUB_USER_ARG="${2:-}";         shift 2 ;;
    --hub-password=*) HUB_PASS_ARG="${1#*=}";        shift ;;
    --hub-password)   HUB_PASS_ARG="${2:-}";         shift 2 ;;
    --hub-psk=*)      HUB_PSK_ARG="${1#*=}";         shift ;;
    --hub-psk)        HUB_PSK_ARG="${2:-}";          shift 2 ;;
    --lm-hub-url=*)   LM_HUB_URL_ARG="${1#*=}";      shift ;;
    --lm-hub-url)     LM_HUB_URL_ARG="${2:-}";       shift 2 ;;
    --lm-hub-secret=*) LM_HUB_SECRET_ARG="${1#*=}";  shift ;;
    --lm-hub-secret)  LM_HUB_SECRET_ARG="${2:-}";    shift 2 ;;
    --lm-core-path=*) LM_CORE_PATH="${1#*=}";        shift ;;
    --lm-core-path)   LM_CORE_PATH="${2:-}";         shift 2 ;;
    --help|-h)        usage ;;
    *) echo "Unknown option: $1 — run with --help for usage" >&2; exit 1 ;;
  esac
done

###############################################################################
# Resolve tenant name → UUID via hub API
# If --hub-tenant is not already a UUID, use --hub-user/--hub-password to
# authenticate against the hub and look up the tenant ID by name.
###############################################################################
if [[ -n "$HUB_TENANT_ARG" ]] && ! [[ "$HUB_TENANT_ARG" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  if [[ -z "$HUB_URL_ARG" ]]; then
    echo "ERROR: --hub-url is required to resolve a tenant name" >&2
    exit 1
  fi
  # When a PSK is supplied, the hub resolves the tenant name/ID server-side —
  # no need to log in and resolve it here.
  if [[ -n "$HUB_PSK_ARG" ]]; then
    : # pass through as-is; hub will match by id or name during PSK registration
  elif [[ -z "$HUB_USER_ARG" || -z "$HUB_PASS_ARG" ]]; then
    echo "ERROR: --hub-user and --hub-password are required to resolve tenant name '${HUB_TENANT_ARG}' (or supply --hub-psk to skip login)" >&2
    exit 1
  else

  # Log in to get a JWT
  _login_resp=$(curl -sk -X POST "${HUB_URL_ARG}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${HUB_USER_ARG}\",\"password\":\"${HUB_PASS_ARG}\"}" 2>/dev/null || true)
  _hub_token=$(echo "$_login_resp" | grep -oP '"access_token"\s*:\s*"\K[^"]+' | head -1)

  if [[ -z "$_hub_token" ]]; then
    echo "ERROR: Could not log in to hub at ${HUB_URL_ARG} — check --hub-user and --hub-password" >&2
    exit 1
  fi

  # Fetch tenant list and match by name (case-insensitive)
  _tenants_resp=$(curl -sk -H "Authorization: Bearer ${_hub_token}" \
    "${HUB_URL_ARG}/api/superadmin/tenants" 2>/dev/null || true)
  _resolved_uuid=$(echo "$_tenants_resp" | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
name = '${HUB_TENANT_ARG}'.lower()
tenants = data if isinstance(data, list) else data.get('tenants', [])
match = next((t for t in tenants if t.get('name','').lower() == name), None)
print(match['id'] if match else '')
" 2>/dev/null || true)

  if [[ -n "$_resolved_uuid" ]]; then
    echo "[info] Resolved hub tenant '${HUB_TENANT_ARG}' → ${_resolved_uuid}"
    HUB_TENANT_ARG="$_resolved_uuid"
  else
    echo "ERROR: No hub tenant named '${HUB_TENANT_ARG}' found — check the name or supply the UUID directly" >&2
    exit 1
  fi
  fi  # end: not PSK path
fi  # end: tenant name resolution block
###############################################################################
if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: Run as root (e.g. sudo $0)" >&2
  exit 1
fi

###############################################################################
# Config — override via environment variables before running
###############################################################################
REPO_URL="${REPO_URL:-https://github.com/solutions-hpe/client-sim.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
HUB_REPO_RAW="${HUB_REPO_RAW:-https://raw.githubusercontent.com/solutions-hpe/webui-hub}"
CS_WEBUI_REPO_RAW="${CS_WEBUI_REPO_RAW:-https://raw.githubusercontent.com/solutions-hpe/cs-webui}"
INSTALL_DIR="${INSTALL_DIR:-/opt/client-sim-dashboard}"
REPO_CACHE="${REPO_CACHE:-/opt/client-sim-repo}"
SERVICE_USER="${SERVICE_USER:-dashboard}"
PORT="${PORT:-8000}"
OFFLINE_TIMEOUT="${OFFLINE_TIMEOUT:-60}"
LOG="/var/log/client-sim-dashboard-install.log"

# DHCP / dnsmasq — auto-detected: only enabled if a second NIC is present.
# Override any value via environment variable before running.
# Leave DHCP_IFACE unset (default) to auto-detect.
DHCP_IFACE="${DHCP_IFACE:-}"           # auto-detected below after logging starts
DHCP_SUBNET="${DHCP_SUBNET:-169.253.1.0}"
DHCP_PREFIX="${DHCP_PREFIX:-24}"
DHCP_GATEWAY="${DHCP_GATEWAY:-169.253.1.1}"
DHCP_RANGE_START="${DHCP_RANGE_START:-169.253.1.11}"
DHCP_RANGE_END="${DHCP_RANGE_END:-169.253.1.254}"
DHCP_LEASE_TIME="${DHCP_LEASE_TIME:-1h}"

# CLI flags take priority over environment variables
[[ -n "$CLI_BRANCH" ]] && REPO_BRANCH="$CLI_BRANCH"
[[ -n "$CLI_PORT"   ]] && PORT="$CLI_PORT"
# --force with no explicit --admin-password leaves the spoke open (no login required).
# Fresh installs without --admin-password use the default password.
if [[ "$FORCE" -eq 1 && -z "$ADMIN_PASSWORD_ARG" ]]; then
  ADMIN_PASSWORD_VAL=""
else
ADMIN_PASSWORD_VAL="${ADMIN_PASSWORD_ARG:-}"
fi

# Validate branch name
if [[ ! "$REPO_BRANCH" =~ ^[a-zA-Z0-9._/\-]+$ ]]; then
  echo "ERROR: Invalid branch name '${REPO_BRANCH}'" >&2
  exit 1
fi

# Validate port number
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "ERROR: Invalid port '${PORT}' — must be 1-65535." >&2
  exit 1
fi

###############################################################################
# Self-bootstrap: always run the latest version from GitHub.
# When the WebUI triggers an update, it calls the local repo copy which may be
# outdated or broken.  Re-fetching from GitHub here ensures we always execute
# the current installer — eliminating the chicken-and-egg problem.
# _CLIENT_SIM_BOOTSTRAPPED is exported to the child so it doesn't loop.
###############################################################################
if [[ -z "${_CLIENT_SIM_BOOTSTRAPPED:-}" ]]; then
  export _CLIENT_SIM_BOOTSTRAPPED=1
  # If REPO_BRANCH is still the default "main", try reading from the installed
  # .env file — this happens when the WebUI calls the script without --branch.
  # Only inherit the stored branch if it is not the legacy lrb value.
  if [[ "${REPO_BRANCH}" == "main" && -f "${INSTALL_DIR}/.env" ]]; then
    _env_br=$(grep '^REPO_BRANCH=' "${INSTALL_DIR}/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'"' ')
    [[ -n "${_env_br}" && "${_env_br}" != "lrb" ]] && REPO_BRANCH="${_env_br}"
  fi
  _bs_url="https://raw.githubusercontent.com/solutions-hpe/client-sim/${REPO_BRANCH}/install-lxc.sh"
  _local_ver=$(grep '^VERSION=' "$0" 2>/dev/null | head -1 | tr -d '"' | cut -d= -f2)
  echo "[bootstrap] Local installer version : ${_local_ver:-unknown}"
  echo "[bootstrap] Fetching latest installer from ${_bs_url} ..."
  _bs_args=(--branch "$REPO_BRANCH" --port "$PORT")
  [[ -n "$ADMIN_PASSWORD_ARG" ]] && _bs_args+=(--admin-password "$ADMIN_PASSWORD_ARG")
  [[ -n "$HUB_URL_ARG" ]]        && _bs_args+=(--hub-url "$HUB_URL_ARG")
  [[ -n "$HUB_TENANT_ARG" ]]     && _bs_args+=(--hub-tenant "$HUB_TENANT_ARG")
  [[ -n "$HUB_USER_ARG" ]]       && _bs_args+=(--hub-user "$HUB_USER_ARG")
  [[ -n "$HUB_PASS_ARG" ]]       && _bs_args+=(--hub-password "$HUB_PASS_ARG")
  [[ -n "$HUB_PSK_ARG" ]]        && _bs_args+=(--hub-psk "$HUB_PSK_ARG")
  [[ "$REINSTALL" -eq 1 ]] && _bs_args+=(--reinstall)
  [[ "$FORCE" -eq 1 ]]    && _bs_args+=(--force)
  [[ "$UNATTENDED" -eq 1 ]] && _bs_args+=(--unattended)
  bash <(curl -fsSL "$_bs_url") "${_bs_args[@]}"
  exit $?
fi

VERSION=1.11
INSTALL_START=$(date +%s)
MODE="Update"
[[ "$REINSTALL" -eq 1 ]] && MODE="Full Reinstall"

###############################################################################
# Colours & logging
###############################################################################
COL_RESET="\033[0m"
COL_GREEN="\033[0;32m"
COL_YELLOW="\033[1;33m"
COL_RED="\033[0;31m"
COL_BOLD="\033[1m"

: >"$LOG"
ts()   { date "+%H:%M:%S"; }
info() { echo -e "[$(ts)] ${COL_BOLD}INFO${COL_RESET}  $*" | tee -a "$LOG"; }
ok()   { echo -e "[$(ts)] ${COL_GREEN}OK${COL_RESET}    $*" | tee -a "$LOG"; }
warn() { echo -e "[$(ts)] ${COL_YELLOW}WARN${COL_RESET}  $*" | tee -a "$LOG"; }
err()  { echo -e "[$(ts)] ${COL_RED}ERR${COL_RESET}   $*" | tee -a "$LOG" >&2; }

trap 'err "Installer failed at line $LINENO — check $LOG"' ERR

###############################################################################
# Banner
###############################################################################
echo
echo "============================================================"
echo " Client Simulator Installer v${VERSION}  [${MODE}]"
echo " $(date)"
echo "============================================================"
echo " Repo URL   : $REPO_URL"
echo " Branch     : $REPO_BRANCH"
echo " Hub raw    : $HUB_REPO_RAW"
echo " CS WebUI   : $CS_WEBUI_REPO_RAW"
echo " Install dir: $INSTALL_DIR"
echo " Port       : $PORT"
echo " Mode       : $MODE"
echo " Log        : $LOG"
echo " DHCP iface : auto-detect (2nd NIC) — default subnet ${DHCP_SUBNET}/${DHCP_PREFIX}"
echo " DHCP range : ${DHCP_RANGE_START} — ${DHCP_RANGE_END}  (${DHCP_LEASE_TIME})"
echo "============================================================"
echo

###############################################################################
# STEP 1 — OS check
###############################################################################
info "Checking OS..."
if [[ ! -f /etc/debian_version ]]; then
  err "This installer requires Debian or Ubuntu. Detected: $(uname -a)"
  exit 1
fi
OS_NAME=$(grep '^PRETTY_NAME' /etc/os-release | cut -d'"' -f2)
ok "OS: $OS_NAME"

###############################################################################
# STEP 2 — System packages
###############################################################################
info "Updating package lists..."
apt-get update --quiet=2 >>"$LOG" 2>&1
ok "Package lists updated"

info "Installing dependencies..."
apt-get install -y --quiet=2 \
  python3 python3-pip python3-venv \
  git curl rsync sudo \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  >>"$LOG" 2>&1
ok "System packages installed"

###############################################################################
# STEP 3 — Auto-detect second NIC; configure DHCP if present
###############################################################################
info "Detecting network interfaces..."

# Build list of ethernet interfaces excluding loopback.
# WHY: In LXC containers, interfaces from Proxmox bridge attachments often appear
# as "eth0@if5" (veth pair notation). We strip the @suffix so eth0 is still
# detected. Using mapfile instead of $(...) avoids word-splitting issues and
# prevents grep exit-code 1 (no matches) from tripping set -euo pipefail.
mapfile -t IFACES < <(
  ip -o link show \
    | awk -F': ' '{print $2}' \
    | sed 's/@.*//' \
    | grep -v '^lo$' \
    || true
)
NIC_COUNT=${#IFACES[@]}
info "Found ${NIC_COUNT} interface(s): ${IFACES[*]}"

# Determine DHCP interface:
#   - If DHCP_IFACE was explicitly set by user, honour it
#   - If only 1 NIC exists, skip DHCP
#   - If 2+ NICs exist, use the second one (index 1)
if [[ -z "$DHCP_IFACE" ]]; then
  if (( NIC_COUNT >= 2 )); then
    DHCP_IFACE="${IFACES[1]}"
    info "Second NIC detected: ${DHCP_IFACE} — DHCP will be configured"
  else
    info "Only one NIC found — DHCP setup skipped"
  fi
fi

if [[ -n "$DHCP_IFACE" ]]; then
  # Install dnsmasq only when we actually need it
  info "Installing dnsmasq..."
  apt-get install -y --quiet=2 dnsmasq \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    >>"$LOG" 2>&1
  ok "dnsmasq installed"

  info "Configuring DHCP on ${DHCP_IFACE} (${DHCP_GATEWAY}/${DHCP_PREFIX})..."

  # ── Static IP on the internal interface ───────────────────────────────────
  IFACE_CFG="/etc/network/interfaces.d/${DHCP_IFACE}.conf"
  cat >"$IFACE_CFG" <<EOF
auto ${DHCP_IFACE}
iface ${DHCP_IFACE} inet static
    address ${DHCP_GATEWAY}
    netmask $(python3 -c "import ipaddress; print(ipaddress.IPv4Network('${DHCP_SUBNET}/${DHCP_PREFIX}',False).netmask)")
EOF
  ok "Interface config written to ${IFACE_CFG}"

  # Bring the interface up (ignore errors if already up)
  ip link set "$DHCP_IFACE" up 2>/dev/null || true
  ip addr flush dev "$DHCP_IFACE" 2>/dev/null || true
  ip addr add "${DHCP_GATEWAY}/${DHCP_PREFIX}" dev "$DHCP_IFACE" 2>/dev/null || true
  ok "${DHCP_IFACE} configured with ${DHCP_GATEWAY}/${DHCP_PREFIX}"

  # ── rp_filter: use loose mode so DestNat / port-forwarded traffic from
  #    other subnets isn't silently dropped by the kernel's strict reverse-
  #    path check.  Without this, a firewall DestNat pointing at this LXC
  #    (which has 2 NICs) works from the same subnet but fails from others.
  sysctl -w net.ipv4.conf.all.rp_filter=2     >>"$LOG" 2>&1 || true
  sysctl -w net.ipv4.conf.default.rp_filter=2 >>"$LOG" 2>&1 || true
  cat >/etc/sysctl.d/10-client-sim.conf <<'SYSCTL'
# Loose reverse-path filter — allows DestNat / port-forward traffic on
# multi-homed LXC containers (management NIC + sim-client DHCP NIC).
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
SYSCTL
  ok "rp_filter set to loose mode (DestNat-compatible)"

  # ── dnsmasq config scoped only to the internal interface ──────────────────
  DNSMASQ_CONF="/etc/dnsmasq.d/client-sim.conf"
  cat >"$DNSMASQ_CONF" <<EOF
# Client-Sim isolated network DHCP — managed by install-lxc.sh
# Only listen on the internal interface; never touches eth0 or other NICs
interface=${DHCP_IFACE}
bind-interfaces
except-interface=lo

# DHCP scope
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${DHCP_LEASE_TIME}

# Explicitly suppress default gateway — clients must not receive a router option.
# Sim clients route through their own WiFi/USB adapter; an injected gateway
# would override that and break traffic generation.
dhcp-option=option:router

# No DNS forwarding — isolated network has no upstream
port=0

# Lease file
dhcp-leasefile=/var/lib/misc/dnsmasq.leases

log-dhcp
EOF
  ok "dnsmasq config written to ${DNSMASQ_CONF} (no default gateway advertised)"

  # Ensure dnsmasq default config doesn't conflict
  if [[ -f /etc/dnsmasq.conf ]]; then
    sed -i 's/^#\?interface=.*$//' /etc/dnsmasq.conf 2>/dev/null || true
  fi

  # Systemd drop-in: wait for the DHCP interface to appear before dnsmasq starts.
  # Without this, dnsmasq fails with "unknown interface" on reboot because the
  # LXC bridge attachment isn't ready when dnsmasq is first started.
  # The ExecStartPre reads the interface name from the dnsmasq config at runtime
  # so it works regardless of the interface name (net1, eth1, ens3, etc.).
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
  systemctl daemon-reload >>"$LOG" 2>&1
  ok "dnsmasq systemd drop-in written (waits for DHCP interface from config)"

  systemctl enable dnsmasq >>"$LOG" 2>&1
  systemctl restart dnsmasq >>"$LOG" 2>&1

  if systemctl is-active --quiet dnsmasq; then
    ok "dnsmasq running — DHCP active on ${DHCP_IFACE}"
  else
    warn "dnsmasq failed to start — check: journalctl -u dnsmasq"
  fi
else
  ok "Single NIC — dnsmasq not installed"
fi

###############################################################################
# STEP 3b — Service user
###############################################################################
info "Checking service user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd -r -s /bin/false -d "$INSTALL_DIR" "$SERVICE_USER" >>"$LOG" 2>&1
  ok "Created service user '$SERVICE_USER'"
else
  ok "Service user '$SERVICE_USER' already exists"
fi

###############################################################################
# STEP 4 — Clone / update client-sim repo
###############################################################################
info "Setting up client-sim repo at $REPO_CACHE..."

# Git 2.35.2+ rejects repos owned by a different user.
# Mark the cache dir as safe at the system level so root can operate on it
# even when it was previously chown'd to the service user.
git config --system --add safe.directory "$REPO_CACHE" >>"$LOG" 2>&1 || true

# Ensure root owns the repo dir before git operates on it; Step 9 re-chowns
# everything to the service user once all git work is finished.
[[ -d "$REPO_CACHE" ]] && chown -R root:root "$REPO_CACHE"

if [[ -d "$REPO_CACHE/.git" ]]; then
  # Unlock all remote branches — needed when repo was previously cloned
  # --single-branch (e.g. lrb) so origin/main doesn't exist locally yet
  git -C "$REPO_CACHE" remote set-branches origin '*' >>"$LOG" 2>&1 || true
  git -C "$REPO_CACHE" fetch origin >>"$LOG" 2>&1
  # -B creates the local branch if missing, or resets it — handles repos
  # previously cloned on a different branch (e.g. lrb → main migration)
  git -C "$REPO_CACHE" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH" >>"$LOG" 2>&1
  ok "Repo updated to latest $REPO_BRANCH"
elif [[ -d "$REPO_CACHE" ]]; then
  warn "Directory exists but is not a git repo — removing and re-cloning"
  rm -rf "$REPO_CACHE"
  git clone --depth=1 -b "$REPO_BRANCH" "$REPO_URL" "$REPO_CACHE" >>"$LOG" 2>&1
  ok "Repo cloned"
else
  git clone --depth=1 -b "$REPO_BRANCH" "$REPO_URL" "$REPO_CACHE" >>"$LOG" 2>&1
  ok "Repo cloned"
fi

###############################################################################
# STEP 5 — Deploy dashboard application files
#
# --reinstall : wipe INSTALL_DIR first (settings.json is backed up / restored)
# update      : rsync from repo so deleted files are removed; user data preserved
###############################################################################
info "Deploying dashboard app to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# Back up user-generated files that must survive a reinstall
# --force skips the backup so hub config is cleared and re-applied via bootstrap
SETTINGS_BACKUP=""
if [[ "$FORCE" -eq 0 && -f "$INSTALL_DIR/settings.json" ]]; then
  SETTINGS_BACKUP=$(cat "$INSTALL_DIR/settings.json")
fi

if [[ "$REINSTALL" -eq 1 ]]; then
  if [[ "$FORCE" -eq 1 ]]; then
    info "Force reinstall — removing existing application files and hub configuration..."
  else
    info "Reinstall mode — removing existing application files..."
  fi
  # Remove app files only; keep venv dir removal for Step 6
  find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
    ! -name 'venv' ! -name '.env' ! -name 'settings.json' ! -name '.secret_key' \
    -exec rm -rf {} + 2>/dev/null || true
  # --force: delete settings.json so hub config can be written fresh
  [[ "$FORCE" -eq 1 ]] && rm -f "$INSTALL_DIR/settings.json"

  # --force + hub args: write relay config directly to settings.json now,
  # before the service starts. This avoids the bootstrap 409 race condition
  # (deferred service restart can still be running when bootstrap is called).
  if [[ "$FORCE" -eq 1 && -n "$HUB_URL_ARG" ]]; then
    python3 -c "
import json, sys
s = {'relay_enabled': 'on', 'relay_server_url': sys.argv[1]}
if sys.argv[2]: s['relay_tenant_id'] = sys.argv[2]; s['relay_tenant_hint'] = sys.argv[2]
if sys.argv[3]: s['relay_onboarding_psk'] = sys.argv[3]
print(json.dumps(s))
" "$HUB_URL_ARG" "$HUB_TENANT_ARG" "$HUB_PSK_ARG" > "$INSTALL_DIR/settings.json"
    info "Hub relay config written to settings.json (will activate on service start)"
  fi
fi

# --lm-hub-url: configure the combined Client-Sim spoke to relay to the LM
# hub (lm/core) as module_type "Client-Sim". Merges into settings.json so
# existing keys (relay config, proxmox tokens, ...) are preserved.
if [[ -n "$LM_HUB_URL_ARG" ]]; then
  python3 -c "
import json, os, sys
p = sys.argv[1]
s = {}
if os.path.exists(p):
    try: s = json.load(open(p))
    except Exception: s = {}
s['lm_hub_enabled'] = 'on'
s['lm_hub_url'] = sys.argv[2]
if sys.argv[3]: s['lm_hub_secret'] = sys.argv[3]
print(json.dumps(s))
" "$INSTALL_DIR/settings.json" "$LM_HUB_URL_ARG" "$LM_HUB_SECRET_ARG" > "$INSTALL_DIR/settings.json.tmp" \
    && mv "$INSTALL_DIR/settings.json.tmp" "$INSTALL_DIR/settings.json"
  info "LM hub relay config written to settings.json (lm_hub_enabled=on -> $LM_HUB_URL_ARG)"
fi

# Sync webui files from repo cache.
# rsync --delete removes files in INSTALL_DIR that no longer exist in the repo.
# venv/, .env, settings.json, and .secret_key are excluded so user data is never wiped.
# static/app.js, style.css, index.html, and VERSION are excluded because they are
# managed exclusively by step 5c (downloaded fresh from cs-webui) — the copies in
# webui-spoke/static/ are stale and must not overwrite the freshly-fetched files.
rsync -a --delete \
  --exclude='venv/' \
  --exclude='.env' \
  --exclude='settings.json' \
  --exclude='.secret_key' \
  --exclude='static/app.js' \
  --exclude='static/style.css' \
  --exclude='static/index.html' \
  --exclude='VERSION' \
  "$REPO_CACHE/webui-spoke/" "$INSTALL_DIR/" >>"$LOG" 2>&1

# Restore settings.json if it existed before sync
if [[ -n "$SETTINGS_BACKUP" && ! -f "$INSTALL_DIR/settings.json" ]]; then
  # On reinstall, wipe auth credentials (admin password, local users) so a fresh
  # password can be set on first login — hub relay config is preserved.
  echo "$SETTINGS_BACKUP" | python3 -c "
import sys, json
s = json.load(sys.stdin)
s.pop('admin_password', None)
s.pop('local_users', None)
print(json.dumps(s))" > "$INSTALL_DIR/settings.json"
fi

ok "Dashboard files synced"

###############################################################################
# STEP 5b — Encryption key (generated once, never overwritten on update)
# Uses stdlib only (base64 + os) so system python3 is sufficient here.
# Fernet key = URL-safe base64 of 32 random bytes — no cryptography pkg needed.
###############################################################################
SECRET_KEY_FILE="$INSTALL_DIR/.secret_key"
if [[ ! -f "$SECRET_KEY_FILE" ]]; then
  info "Generating encryption key for credential storage..."
  python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
    > "$SECRET_KEY_FILE"
  chmod 600 "$SECRET_KEY_FILE"
  chown root:root "$SECRET_KEY_FILE" 2>/dev/null || true
  ok "Encryption key created at $SECRET_KEY_FILE"
else
  ok "Encryption key exists — preserved"
fi

###############################################################################
# STEP 5c — cs-webui frontend (modular JS files, style.css, index.html from cs-webui repo)
#
# Files are fetched from cs-webui at the same branch as the spoke repo so dev
# work on main automatically pulls the main version of the unified frontend.
# After fetch, WEBUI_MODE is injected as 'spoke' into index.html.
# Failures are non-fatal — the spoke continues with whatever version exists.
###############################################################################
info "Fetching cs-webui frontend from cs-webui repo (branch: ${REPO_BRANCH})..."
SHARED_STATIC_DIR="${INSTALL_DIR}/static"
mkdir -p "$SHARED_STATIC_DIR/js/spoke"
_cw_raw="${CS_WEBUI_REPO_RAW}/${REPO_BRANCH}"

# Download legacy app.js (still used for backwards compat) and style.css
for _sf in static/app.js static/style.css; do
  _dest="${SHARED_STATIC_DIR}/$(basename "${_sf}")"
  if curl -sSf --max-time 30 "${_cw_raw}/${_sf}" -o "${_dest}" >>"$LOG" 2>&1; then
    ok "Downloaded $(basename ${_sf}) from cs-webui:${REPO_BRANCH}"
  else
    warn "Could not fetch $(basename ${_sf}) from cs-webui — keeping existing file if present"
  fi
done

# Download modular ES6 JavaScript files
for _sf in static/js/main.js static/js/state.js static/js/websocket.js static/js/nav.js static/js/agent-log.js static/js/utils.js static/js/spoke/dashboard.js static/js/spoke/central.js; do
  _dest="${SHARED_STATIC_DIR}/$(echo ${_sf} | sed 's|^static/||')"
  _dest_dir="$(dirname "${_dest}")"
  mkdir -p "${_dest_dir}"
  if curl -sSf --max-time 30 "${_cw_raw}/${_sf}" -o "${_dest}" >>"$LOG" 2>&1; then
    ok "Downloaded $(basename ${_sf}) from cs-webui:${REPO_BRANCH}"
  else
    warn "Could not fetch ${_sf} from cs-webui — keeping existing file if present"
  fi
done
# Fetch index.html from templates/ and inject WEBUI_MODE=spoke
_idx="${SHARED_STATIC_DIR}/index.html"
if curl -sSf --max-time 30 "${_cw_raw}/templates/index.html" -o "${_idx}" >>"$LOG" 2>&1; then
  sed -i 's/{{WEBUI_MODE}}/spoke/g' "${_idx}"
  ok "Downloaded index.html from cs-webui:${REPO_BRANCH} (WEBUI_MODE=spoke injected)"
else
  warn "Could not fetch index.html from cs-webui — keeping existing file if present"
fi
# Fetch VERSION file so APP_VERSION reflects the cs-webui version, not the installer version
if curl -sSf --max-time 30 "${_cw_raw}/VERSION" -o "${INSTALL_DIR}/VERSION" >>"$LOG" 2>&1; then
  ok "Downloaded cs-webui VERSION ($(cat "${INSTALL_DIR}/VERSION")) from cs-webui:${REPO_BRANCH}"
else
  warn "Could not fetch VERSION from cs-webui — version display may fall back to installer version"
fi
unset _cw_raw _sf _dest _idx

###############################################################################
# STEP 6 — Python virtual environment + dependencies
#
# --reinstall : remove and recreate venv from scratch
# update      : skip recreation; just upgrade deps
###############################################################################
if [[ "$REINSTALL" -eq 1 && -d "$INSTALL_DIR/venv" ]]; then
  info "Reinstall mode — removing existing virtual environment..."
  rm -rf "$INSTALL_DIR/venv"
fi

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  info "Creating Python virtual environment..."
  python3 -m venv "$INSTALL_DIR/venv" >>"$LOG" 2>&1
  ok "Virtual environment created"
else
  ok "Virtual environment exists — skipping creation"
fi

info "Installing/updating Python dependencies..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip >>"$LOG" 2>&1
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" >>"$LOG" 2>&1
ok "Python dependencies up to date"

###############################################################################
# STEP 7 — Environment file
#
# --reinstall : overwrite with fresh defaults
# update      : only write keys that are missing (preserves user customisations)
###############################################################################
write_env_key() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$INSTALL_DIR/.env" 2>/dev/null; then
    : # key already set by user — leave it alone
  else
    echo "${key}=${value}" >> "$INSTALL_DIR/.env"
  fi
}

update_env_key() {
  local key="$1" value="$2"
  python3 - "$INSTALL_DIR/.env" "$key" "$value" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
prefix = f"{key}="
lines = path.read_text().splitlines() if path.exists() else []
for idx, line in enumerate(lines):
    if line.startswith(prefix):
        lines[idx] = prefix + value
        break
else:
    lines.append(prefix + value)
path.write_text("\n".join(lines) + "\n")
PY
}

is_valid_uuid() {
  python3 - "$1" <<'PY'
import sys
import uuid

candidate = (sys.argv[1] or "").strip()
try:
    print(str(uuid.UUID(candidate)) == candidate.lower())
except ValueError:
    print(False)
PY
}

existing_spoke_id=""
if [[ -f "$INSTALL_DIR/.env" ]]; then
  existing_spoke_id=$(grep '^SPOKE_ID=' "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d ' ' || true)
fi
# --force always regenerates SPOKE_ID to avoid reusing a cloned machine's ID.
# Normal reinstalls preserve the existing SPOKE_ID so the spoke stays known to the hub.
if [[ "$FORCE" -eq 1 ]]; then
  SPOKE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
  ok "Force-reinstall: generated fresh SPOKE_ID (old ID discarded)"
elif [[ "$(is_valid_uuid "$existing_spoke_id")" == "True" ]]; then
  SPOKE_ID="$existing_spoke_id"
  ok "Preserving existing SPOKE_ID"
else
  SPOKE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
  ok "Generated unique SPOKE_ID"
fi

if [[ "$REINSTALL" -eq 1 || ! -f "$INSTALL_DIR/.env" ]]; then
  info "Writing fresh environment config..."
  cat >"$INSTALL_DIR/.env" <<EOF
REPO_URL=$REPO_URL
REPO_BRANCH=$REPO_BRANCH
CS_WEBUI_REPO_RAW=$CS_WEBUI_REPO_RAW
REPO_DIR=$REPO_CACHE
OFFLINE_TIMEOUT=$OFFLINE_TIMEOUT
SPOKE_ID=$SPOKE_ID
INSTALLER_VERSION=$VERSION
ADMIN_PASSWORD=$ADMIN_PASSWORD_VAL
EOF
  ok "Environment file written"
else
  info "Updating environment config (preserving existing values)..."
  write_env_key "REPO_URL"             "$REPO_URL"
  write_env_key "REPO_BRANCH"          "$REPO_BRANCH"
  write_env_key "CS_WEBUI_REPO_RAW"    "$CS_WEBUI_REPO_RAW"
  write_env_key "REPO_DIR"             "$REPO_CACHE"
  write_env_key "OFFLINE_TIMEOUT"      "$OFFLINE_TIMEOUT"
  update_env_key "SPOKE_ID"            "$SPOKE_ID"
  update_env_key "INSTALLER_VERSION"   "$VERSION"
  write_env_key  "ADMIN_PASSWORD"      "$ADMIN_PASSWORD_VAL"
  ok "Environment file checked — existing values preserved"
fi
chmod 640 "$INSTALL_DIR/.env"

###############################################################################
# STEP 8 — systemd service (always update — service config may change between versions)
###############################################################################
info "Installing systemd service..."
cat >/etc/systemd/system/client-sim-dashboard.service <<EOF
[Unit]
Description=Client Simulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
# PYTHONPATH: $INSTALL_DIR (so `server`/`lm_relay` import), the lm repo root
# ($LM_CORE_PATH, so the `core` package resolves), and its core/src (extra
# convenience). Only required when lm_hub_enabled turns this into a combined
# Client-Sim spoke; harmless otherwise.
Environment="PYTHONPATH=$INSTALL_DIR:$LM_CORE_PATH:$LM_CORE_PATH/core/src"
ExecStart=$INSTALL_DIR/venv/bin/uvicorn server:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=client-sim-dashboard

[Install]
WantedBy=multi-user.target
EOF

info "Installing WebUI watchdog..."
install -d -m 755 /usr/local/bin /etc/systemd/system /var/lib/webui-watchdog
install -m 755 "$INSTALL_DIR/watchdog.sh" /usr/local/bin/webui-watchdog.sh
install -m 644 "$INSTALL_DIR/webui-watchdog.service" /etc/systemd/system/webui-watchdog.service
install -m 644 "$INSTALL_DIR/webui-watchdog.timer" /etc/systemd/system/webui-watchdog.timer

systemctl daemon-reload
systemctl enable client-sim-dashboard >>"$LOG" 2>&1
systemctl enable webui-watchdog.timer >>"$LOG" 2>&1
systemctl restart webui-watchdog.timer >>"$LOG" 2>&1
ok "systemd service installed and enabled"
ok "WebUI watchdog installed and timer enabled"

###############################################################################
# STEP 9 — Permissions
###############################################################################
info "Setting permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_CACHE"
# Write installer version so the dashboard can display it and detect updates
echo "$VERSION" > "$INSTALL_DIR/INSTALLER_VERSION"
# Allow service user to self-update by re-running this installer as root
SUDOERS_FILE="/etc/sudoers.d/client-sim-dashboard"
SUDOERS_LINE="${SERVICE_USER} ALL=(root) NOPASSWD: /bin/bash ${REPO_CACHE}/install-lxc.sh *"
mkdir -p /etc/sudoers.d
# Remove old file first (may have 440 perms from a prior install)
rm -f "$SUDOERS_FILE"
# Write to a temp file, validate syntax, then install atomically
_sudoers_tmp=$(mktemp)
echo "$SUDOERS_LINE" > "$_sudoers_tmp"
if visudo -c -f "$_sudoers_tmp" >>"$LOG" 2>&1; then
  install -m 440 -o root -g root "$_sudoers_tmp" "$SUDOERS_FILE"
  ok "Sudoers entry written for self-update"
else
  warn "visudo validation failed — skipping sudoers entry (self-update button will require manual sudo setup)"
fi
rm -f "$_sudoers_tmp"
ok "Permissions set"

###############################################################################
# STEP 10 — Start / restart service
###############################################################################
if systemctl is-active --quiet client-sim-dashboard; then
  info "Restarting client-sim-dashboard service..."
  # Schedule restart AFTER this script exits — if the installer was launched
  # by the running server, a synchronous restart here would send SIGTERM to
  # the server mid-install, which cascades back and kills this script (-15).
  # The subshell is disowned so it outlives this process and any parent.
  (sleep 2 && systemctl restart client-sim-dashboard) &
  disown
else
  info "Starting client-sim-dashboard service..."
  systemctl start client-sim-dashboard
fi
# Give the (possibly deferred) restart time to complete before health check.
sleep 5

_service_ready=0
info "Waiting for dashboard API to be ready..."
for _i in $(seq 1 90); do
  if systemctl is-active --quiet client-sim-dashboard \
    && curl -fsSL --connect-timeout 2 --max-time 2 "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
    _service_ready=1
    ok "Dashboard API ready on :${PORT}"
    break
  fi
  [[ $((_i % 15)) -eq 0 ]] && info "Still waiting... (${_i}×2s elapsed)"
  sleep 2
done

if systemctl is-active --quiet client-sim-dashboard; then
  ok "Service running"
else
  warn "Service may not have started — check: journalctl -u client-sim-dashboard"
fi

if [[ "${_service_ready}" -ne 1 ]]; then
  warn "Dashboard API did not become ready before installer exit — check: journalctl -u client-sim-dashboard"
fi
unset _service_ready _i

###############################################################################
# HUB BOOTSTRAP (if --hub-url was provided)
###############################################################################
if [[ -n "$HUB_URL_ARG" ]]; then
  echo
  # --force already wrote settings.json directly before service start — no bootstrap needed.
  if [[ "$FORCE" -eq 1 ]]; then
    ok "Hub relay configured (via settings.json) → ${HUB_URL_ARG}"
    [[ -n "$HUB_TENANT_ARG" ]] && ok "Tenant ID           → ${HUB_TENANT_ARG}"
  else
  info "Configuring hub relay..."
  _bootstrap_payload="{\"relay_server_url\":\"${HUB_URL_ARG}\""
  [[ -n "$HUB_TENANT_ARG" ]] && _bootstrap_payload+=",\"relay_tenant_id\":\"${HUB_TENANT_ARG}\""
  [[ -n "$HUB_PSK_ARG" ]]    && _bootstrap_payload+=",\"relay_onboarding_psk\":\"${HUB_PSK_ARG}\""
  _bootstrap_payload+="}"

  # Wait up to 30s for the API to be reachable before bootstrapping
  for _bsi in $(seq 1 15); do
    curl -fsSL --connect-timeout 2 --max-time 2 "http://localhost:${PORT}/api/health" >/dev/null 2>&1 && break
    sleep 2
  done
  unset _bsi

  _bootstrap_result=$(curl -sf -X POST "http://localhost:${PORT}/api/bootstrap" \
    -H "Content-Type: application/json" \
    -d "$_bootstrap_payload" 2>&1 || true)

  if echo "$_bootstrap_result" | grep -q '"status":"ok"'; then
    ok "Hub relay configured → ${HUB_URL_ARG}"
    [[ -n "$HUB_TENANT_ARG" ]] && ok "Tenant ID           → ${HUB_TENANT_ARG}"
  elif echo "$_bootstrap_result" | grep -q "409"; then
    warn "Hub already configured (bootstrap is one-time only — use Setup page to change)"
  else
    warn "Hub bootstrap failed — configure manually via the Setup page"
    warn "Response: ${_bootstrap_result:-no response}"
  fi
  fi  # end: not --force path
fi

###############################################################################
# HEALTH CHECK
###############################################################################
echo
echo "================ HEALTH CHECK ================"
CONTAINER_IP=$(hostname -I | awk '{print $1}')

id "$SERVICE_USER" &>/dev/null \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Service user ($SERVICE_USER)   OK" \
  || echo -e "  ${COL_RED}✗${COL_RESET}  Service user ($SERVICE_USER)   MISSING"

[[ -d "$INSTALL_DIR/venv" ]] \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Python venv                   OK" \
  || echo -e "  ${COL_RED}✗${COL_RESET}  Python venv                   MISSING"

[[ -f "$INSTALL_DIR/.env" ]] \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Environment file              OK" \
  || echo -e "  ${COL_RED}✗${COL_RESET}  Environment file              MISSING"

[[ -d "$REPO_CACHE/.git" ]] \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Repo cache                    OK" \
  || echo -e "  ${COL_RED}✗${COL_RESET}  Repo cache                    MISSING"

systemctl is-active --quiet client-sim-dashboard \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Dashboard service             RUNNING" \
  || echo -e "  ${COL_YELLOW}✗${COL_RESET}  Dashboard service             NOT RUNNING"

systemctl is-enabled --quiet client-sim-dashboard \
  && echo -e "  ${COL_GREEN}✓${COL_RESET}  Auto-start on boot            ENABLED" \
  || echo -e "  ${COL_YELLOW}✗${COL_RESET}  Auto-start on boot            DISABLED"

if curl -fsSL --connect-timeout 5 "http://localhost:${PORT}/api/health" >>/dev/null 2>&1; then
  echo -e "  ${COL_GREEN}✓${COL_RESET}  API responding on :${PORT}         OK"
else
  echo -e "  ${COL_YELLOW}✗${COL_RESET}  API not yet responding on :${PORT}  (may still be starting)"
fi

if [[ -n "$DHCP_IFACE" ]]; then
  systemctl is-active --quiet dnsmasq \
    && echo -e "  ${COL_GREEN}✓${COL_RESET}  dnsmasq DHCP (${DHCP_IFACE})       RUNNING" \
    || echo -e "  ${COL_YELLOW}✗${COL_RESET}  dnsmasq DHCP (${DHCP_IFACE})       NOT RUNNING"
  ip addr show "$DHCP_IFACE" 2>/dev/null | grep -q "${DHCP_GATEWAY}" \
    && echo -e "  ${COL_GREEN}✓${COL_RESET}  ${DHCP_IFACE} IP (${DHCP_GATEWAY})   OK" \
    || echo -e "  ${COL_YELLOW}✗${COL_RESET}  ${DHCP_IFACE} IP (${DHCP_GATEWAY})   NOT SET (attach NIC in Proxmox first)"
fi

echo "============================================="
echo

ELAPSED=$(( $(date +%s) - INSTALL_START ))
echo -e "${COL_GREEN}${COL_BOLD}${MODE} complete${COL_RESET} in ${ELAPSED}s"
echo
echo -e "  Dashboard  : ${COL_BOLD}http://${CONTAINER_IP}:${PORT}${COL_RESET}"
echo -e "  API docs   : ${COL_BOLD}http://${CONTAINER_IP}:${PORT}/docs${COL_RESET}"
[[ -n "$HUB_URL_ARG" ]] && echo -e "  Hub relay  : ${COL_BOLD}${HUB_URL_ARG}${COL_RESET}"
echo -e "  Logs       : journalctl -u client-sim-dashboard -f"
echo -e "  Install log: $LOG"
echo
if [[ -n "$DHCP_IFACE" ]]; then
echo -e "  ${COL_YELLOW}Client network (vmbr255):${COL_RESET}"
echo -e "  WebUI address : ${DHCP_GATEWAY}"
echo -e "  DHCP range    : ${DHCP_RANGE_START} — ${DHCP_RANGE_END}"
echo
fi
echo -e "  ${COL_YELLOW}Set in simulation.conf on each client:${COL_RESET}"
echo -e "  [server]"
echo -e "  server_url=http://${DHCP_GATEWAY:-${CONTAINER_IP}}:${PORT}"
echo

