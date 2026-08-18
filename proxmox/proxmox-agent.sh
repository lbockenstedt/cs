#!/bin/bash
# proxmox-agent.sh — Client-Sim Proxmox Host Agent
# Collects VM + node telemetry, polls for commands, and auto-provisions USB-backed VMs.
# Runs as a systemd service on the Proxmox HOST (not in the LXC container).

set -euo pipefail

AGENT_VERSION="1.15"
AGENT_LOG="/var/log/client-sim-proxmox-agent.log"
AGENT_LOG_OFFSET_FILE="/var/lib/client-sim/agent-log-offset"
PIDFILE="/var/run/client-sim-proxmox-agent.pid"
ENV_FILE="/etc/client-sim-proxmox-agent.env"
AGENT_SHELL_PID=$$

# Load persisted env (API key, etc.) before applying defaults
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE" 2>/dev/null || true

SERVER_URL="${CLIENT_SIM_SERVER_URL:-}"  # Prefer persisted URL from env file; can be overridden by --server arg
API_KEY="${CLIENT_SIM_API_KEY:-}"
POLL_INTERVAL="${CLIENT_SIM_POLL_INTERVAL:-15}"
TELEMETRY_INTERVAL="${CLIENT_SIM_TELEMETRY_INTERVAL:-3}"
INBOX_INTERVAL="${CLIENT_SIM_INBOX_INTERVAL:-10}"
SELF_UPDATE_INTERVAL="${CLIENT_SIM_SELF_UPDATE_INTERVAL:-3600}"   # 1 hour
SELF_UPDATE_RETRY_INTERVAL="${CLIENT_SIM_SELF_UPDATE_RETRY_INTERVAL:-300}"  # 5 minutes after a failed update check
SPOKE_OFFLINE_REINSTALL_SECS=3600
STATE_FILE="/etc/client-sim-usb-state.conf"
STATE_LOCK_FILE="${STATE_FILE}.lock"
EXCLUDED_BUS_FILE="/etc/client-sim-excluded-buses.conf"
AGENT_PORT="${CLIENT_SIM_AGENT_PORT:-9105}"
HEALTH_STALE_SECS="${CLIENT_SIM_AGENT_HEALTH_STALE_SECS:-180}"
HEALTH_FILE="/var/lib/client-sim/agent-health.json"
USB_STATE_CACHE="/tmp/client-sim-usb-state.cache"
USB_PRESENT_CACHE="/tmp/client-sim-usb-present.cache"
USB_UNKNOWN_CACHE="/tmp/client-sim-usb-unknown.cache"
USB_QUARANTINE_CACHE="/tmp/client-sim-usb-quarantine.cache"
USB_QUARANTINE_FILE="/etc/client-sim-usb-quarantine.json"
USB_QUARANTINE_THRESHOLD=3
RECLONE_STATE_CACHE="/var/lib/client-sim/reclone-state.json"
RESEED_LOCK_FILE="/tmp/.proxmox_reseed_lock"
PROGRESS_EVENT_QUEUE_DIR="/var/lib/client-sim/progress-events"
ORPHAN_VMS_FILE="/var/lib/client-sim/orphan_vms.json"
ORPHAN_VMS_CACHE="/tmp/client-sim-orphan-vms.cache"
DESTROY_MAX_FAILS=3
SERVER_URL_AUTO_DETECTED=0
HUB_CONTACT_LOSS_REDETECT_SECS=300
HUB_STATE_DIR="/var/lib/client-sim"
HUB_LAST_SUCCESS_FILE="${HUB_STATE_DIR}/hub-last-success"
HUB_SERVER_URL_FILE="${HUB_STATE_DIR}/hub-server-url"
HUB_REDETECT_LOCK_FILE="${HUB_STATE_DIR}/hub-redetect.lock"
USB_PROVISION_LOCK_FILE="${HUB_STATE_DIR}/usb-provision.lock"
PROVISION_HALT_CACHE="${HUB_STATE_DIR}/provision_halt.json"
PROVISION_COOLDOWN_RESET_FILE="${HUB_STATE_DIR}/cooldown-reset.signal"

# ── Driver Blacklist ───────────────────────────────────────────────────────────
DONGLE_BLACKLIST_CONF="/etc/modprobe.d/cs-dongle-blacklist.conf"
BLACKLISTED_DRIVERS_JSON="[]"

# ── Hardware Watchdog ──────────────────────────────────────────────────────────
HW_WATCHDOG_INTERVAL="${CLIENT_SIM_HW_WATCHDOG_INTERVAL:-60}"   # seconds between scans
HW_WATCHDOG_ENABLED="${CLIENT_SIM_HW_WATCHDOG_ENABLED:-1}"       # 0 to disable
HW_FAULT_LOG="/var/lib/client-sim/hw-faults.json"
HW_WATCHDOG_CURSOR="/var/lib/client-sim/hw-watchdog-cursor"
HW_RESET_RECORD="/var/lib/client-sim/hw-last-reset.json"
# How many Tier-2 fault hits within the scan window before rebooting
HW_TIER2_REBOOT_THRESHOLD="${CLIENT_SIM_HW_TIER2_THRESHOLD:-3}"
# Minimum seconds between watchdog-triggered reboots (prevent reboot storm)
HW_REBOOT_COOLDOWN="${CLIENT_SIM_HW_REBOOT_COOLDOWN:-300}"

# ── VM Guest Agent Watchdog ────────────────────────────────────────────────────
# Monitors each sim VM's QEMU guest agent; reboots then reclones unresponsive VMs.
GUEST_AGENT_WATCHDOG_ENABLED="${CLIENT_SIM_GUEST_AGENT_WATCHDOG_ENABLED:-on}"
GUEST_AGENT_GRACE_MINUTES="${CLIENT_SIM_GUEST_AGENT_GRACE_MINUTES:-20}"
GUEST_AGENT_CHECK_INTERVAL_MINUTES="${CLIENT_SIM_GUEST_AGENT_CHECK_INTERVAL_MINUTES:-10}"
GUEST_AGENT_REBOOT_AFTER_MINUTES="${CLIENT_SIM_GUEST_AGENT_REBOOT_AFTER_MINUTES:-10}"
GUEST_AGENT_RECLONE_AFTER_MINUTES="${CLIENT_SIM_GUEST_AGENT_RECLONE_AFTER_MINUTES:-30}"
# When "off", watchdog errors are logged/reported but no automatic reboots are issued.
WATCHDOG_REBOOT_ENABLED="${CLIENT_SIM_WATCHDOG_REBOOT_ENABLED:-on}"
_LAST_AGENT_WATCHDOG_CHECK=0

# Worker subprocesses dispatched by the main agent's Python WS client must NOT be
# blocked by the duplicate-instance guard — they are always called while the main
# agent is running (which would otherwise look like a duplicate).
_IS_SUBPROCESS=0
case "${1:-}" in
    --process-single-command|--collect-telemetry|--process-backup-command|--process-reseed-command)
        _IS_SUBPROCESS=1 ;;
esac

# Prevent duplicate daemon instances (skip for worker subprocesses)
if [[ "$_IS_SUBPROCESS" -eq 0 ]]; then
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Another instance already running (PID $(cat "$PIDFILE")), exiting."
        exit 1
    fi
    echo $$ > "$PIDFILE"
    trap 'rm -f "$PIDFILE"; [[ -n "${TELEMETRY_PID:-}" ]] && kill "$TELEMETRY_PID" 2>/dev/null; [[ -n "${INBOX_PID:-}" ]] && kill "$INBOX_PID" 2>/dev/null; [[ -n "${HW_WATCHDOG_PID:-}" ]] && kill "$HW_WATCHDOG_PID" 2>/dev/null; true' EXIT
fi

AUTO_PROVISION="off"
MISSING_TIMEOUT=60
PROV_DIR=/tmp/client-sim-prov
mkdir -p "$PROV_DIR"
TEMPLATE_LOCK_STATUS_FILE="${PROV_DIR}/template_lock_status"
IMAGE1_TEMPLATE_ID=100
IMAGE1_TEMPLATE_SPEC=""
IMAGE1_TEMPLATE_SPEC_SEEN=0
IMAGE2_TEMPLATE_ID=200
IMAGE2_TEMPLATE_SPEC=""
IMAGE2_TEMPLATE_SPEC_SEEN=0
IMAGE1_PCT=50
VM_SET_OVERRIDE=0
SIM_PHY="wireless"
USE_ALL_DONGLES="false"
RECLONE_CONCURRENCY=1
L1_VLAN_START=100
L1_VLAN_END=199
MAX_USB_SLOTS=24
UNKNOWN_USB_JSON="[]"
USB_STATE_JSON="[]"
PRESENT_USB_JSON="[]"

h=$(hostname)
# Extract trailing numeric suffix — handles svr-01, svr-02, svr-001, etc.
num_suffix=$(printf '%s' "$h" | grep -oE '[0-9]+$' || true)
if [[ -n "$num_suffix" && "$num_suffix" =~ ^[0-9]+$ ]]; then
    id_num=$((10#$num_suffix))
    [[ $id_num -lt 1 ]] && id_num=1
else
    id_num=1
fi
HOSTNAME_ID_NUM=$id_num
host_id=$(printf '%03d' $id_num)
# VMID_BLOCK_STRIDE is the fixed per-host block size used for VMID range calculation.
# Set to 24 to match the existing deployed layout (svr-001→90001, svr-002→90025, svr-003→90049).
# Changing usb_max_slots no longer shifts start_vmid — only end_vmid moves.
# To use more than 25 slots on a host, set vmid_start manually in the hub spoke config.
VMID_BLOCK_STRIDE=24
# MAX_USB_SLOTS caps how many slots are *used* within this host's block; updated from usb-config at runtime.
MAX_USB_SLOTS=24
start_vmid=$((90000 + (id_num - 1) * VMID_BLOCK_STRIDE + 1))
end_vmid=$((start_vmid + MAX_USB_SLOTS - 1))

recompute_vmid_range() {
    local manual_vmid_start="${1:-0}"

    id_num=$HOSTNAME_ID_NUM
    host_id=$(printf '%03d' "$id_num")

    if [[ -n "$VM_SET_OVERRIDE" && "$VM_SET_OVERRIDE" =~ ^[0-9]+$ ]] && (( VM_SET_OVERRIDE >= 1 && VM_SET_OVERRIDE <= 99 )); then
        if (( VM_SET_OVERRIDE != HOSTNAME_ID_NUM )); then
            log "VM set override: using id_num=$VM_SET_OVERRIDE instead of hostname-derived id_num=$HOSTNAME_ID_NUM"
        fi
        id_num=$VM_SET_OVERRIDE
        host_id=$(printf '%03d' "$id_num")
    fi

    start_vmid=$((90000 + (id_num - 1) * VMID_BLOCK_STRIDE + 1))

    if (( MAX_USB_SLOTS > 25 )); then
        if [[ "$manual_vmid_start" =~ ^[0-9]+$ ]] && (( manual_vmid_start > 0 )); then
            start_vmid="$manual_vmid_start"
            log "VMID range: manual override — start_vmid=$start_vmid (usb_max_slots=$MAX_USB_SLOTS)"
        else
            log "ERROR: usb_max_slots=$MAX_USB_SLOTS exceeds 25 but no vmid_start is configured for this host."
            log "ERROR: Set vmid_start in the hub spoke config to use more than 25 slots."
            log "WARNING: Capping MAX_USB_SLOTS at 25 to prevent VM range overlap."
            MAX_USB_SLOTS=25
        fi
    fi

    end_vmid=$((start_vmid + MAX_USB_SLOTS - 1))
}

declare -A CERTIFIED_TYPES CERTIFIED_LABELS IGNORED_VIDPIDS
declare -A USB_NAME_BY_BUS USB_VIDPID_BY_BUS PRESENT_BUSES
declare -A STATE_VMID_TO_IMAGE
declare -A STATE_BUS_TO_VMID STATE_VMID_TO_BUS STATE_MISSING_BY_BUS STATE_VIDPID_BY_BUS STATE_EXCLUDED_BUS
declare -A USB_FAIL_COUNT USB_QUARANTINED
declare -A _RECLONE_CMD_IDS=()   # vmid -> cmd_id, used for parallel reclone ACKs

declare -a UNKNOWN_USB_LINES USB_STATE_LINES

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$AGENT_LOG" 2>/dev/null || true
}

sed_escape() {
    printf '%s\n' "$1" | sed -e 's/[\/&]/\\&/g'
}

atomic_write_file() {
    local target="$1"
    local content="${2:-}"
    local tmp_file="${target}.tmp"
    {
        printf '%s\n' "$content"
    } > "$tmp_file" && mv "$tmp_file" "$target"
}

persist_runtime_server_url() {
    [[ -n "${SERVER_URL:-}" ]] || return 0
    mkdir -p "$HUB_STATE_DIR"
    atomic_write_file "$HUB_SERVER_URL_FILE" "$SERVER_URL"
}

refresh_runtime_server_url() {
    [[ "${SERVER_URL_AUTO_DETECTED:-0}" -eq 1 ]] || return 0
    [[ -f "$HUB_SERVER_URL_FILE" ]] || return 0
    local runtime_url
    runtime_url=$(tr -d '[:space:]' < "$HUB_SERVER_URL_FILE" 2>/dev/null || true)
    [[ -n "$runtime_url" ]] && SERVER_URL="$runtime_url"
}

mark_hub_contact_success() {
    mkdir -p "$HUB_STATE_DIR"
    printf '%s\n' "$(date +%s)" > "$HUB_LAST_SUCCESS_FILE"
}

last_hub_contact_ts() {
    local now ts
    now=$(date +%s)
    [[ -f "$HUB_LAST_SUCCESS_FILE" ]] || { printf '%s\n' "$now"; return 0; }
    ts=$(tr -d '[:space:]' < "$HUB_LAST_SUCCESS_FILE" 2>/dev/null || true)
    [[ "$ts" =~ ^[0-9]+$ ]] || ts="$now"
    printf '%s\n' "$ts"
}

maybe_redetect_hub_url() {
    [[ "${SERVER_URL_AUTO_DETECTED:-0}" -eq 1 ]] || return 0
    mkdir -p "$HUB_STATE_DIR"
    local now last_success
    now=$(date +%s)
    last_success=$(last_hub_contact_ts)
    (( now - last_success >= HUB_CONTACT_LOSS_REDETECT_SECS )) || return 0

    if command -v flock >/dev/null 2>&1; then
        (
            flock -n 200 || exit 0
            local locked_now locked_last
            locked_now=$(date +%s)
            locked_last=$(last_hub_contact_ts)
            (( locked_now - locked_last >= HUB_CONTACT_LOSS_REDETECT_SECS )) || exit 0
            log "[agent] Connectivity lost >5min, re-detecting IP..."
            auto_detect_hub_url || true
            persist_runtime_server_url
            printf '%s\n' "$locked_now" > "$HUB_LAST_SUCCESS_FILE"
        ) 200>"$HUB_REDETECT_LOCK_FILE"
        return 0
    fi

    log "[agent] Connectivity lost >5min, re-detecting IP..."
    auto_detect_hub_url || true
    persist_runtime_server_url
    printf '%s\n' "$now" > "$HUB_LAST_SUCCESS_FILE"
}

record_hub_contact_result() {
    local http_code="${1:-}" curl_exit="${2:-0}"
    if [[ "$http_code" =~ ^[0-9]{3}$ ]] && [[ "$http_code" != "000" ]]; then
        mark_hub_contact_success
        if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
            clear_spoke_failure
        fi
        return 0
    fi
    if [[ -z "$http_code" || "$http_code" == "000" || "$curl_exit" != "0" ]]; then
        record_spoke_failure
        maybe_redetect_hub_url
    fi
}

set_template_lock_status() {
    atomic_write_file "$TEMPLATE_LOCK_STATUS_FILE" "${1:-}"
}

clear_template_lock_status() {
    rm -f "$TEMPLATE_LOCK_STATUS_FILE" 2>/dev/null || true
}

probe_template_lock_status() {
    local -A _seen_templates=()
    local -a _messages=()
    local _template_id _lock
    for _template_id in "$IMAGE1_TEMPLATE_ID" "$IMAGE2_TEMPLATE_ID"; do
        [[ -n "${_template_id:-}" ]] || continue
        [[ -n "${_seen_templates[$_template_id]:-}" ]] && continue
        _seen_templates["$_template_id"]=1
        _lock=$(qm config "$_template_id" 2>/dev/null | awk '/^lock:/{print $2}' || true)
        [[ -n "$_lock" ]] && _messages+=("template ${_template_id}: ${_lock}")
    done
    if [[ ${#_messages[@]} -gt 0 ]]; then
        local _status
        _status=$(IFS='; '; printf '%s' "${_messages[*]}")
        set_template_lock_status "$_status"
        printf '%s' "$_status"
        return 0
    fi
    clear_template_lock_status
    return 1
}

is_truthy() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

sim_phy_accepts_type() {
    local actual_type="$1"
    if [[ "$SIM_PHY" == "any" || "$actual_type" == "$SIM_PHY" ]]; then
        return 0
    fi
    if is_truthy "$USE_ALL_DONGLES" && [[ "$SIM_PHY" == "wireless" || "$SIM_PHY" == "ethernet" ]]; then
        return 0
    fi
    return 1
}

valid_json_file() {
    local file="$1"
    [[ -f "$file" ]] || return 1
    python3 -c "import json,sys; json.load(sys.stdin)" < "$file" >/dev/null 2>&1
}

read_json_cache_or_default() {
    local file="$1" default_payload="${2:-[]}"
    if valid_json_file "$file"; then
        cat "$file"
        return 0
    fi
    if [[ -f "$file" ]]; then
        printf '[%s] WARNING: Ignoring malformed JSON cache %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$file" >&2
    fi
    printf '%s' "$default_payload"
}

write_reclone_state_cache() {
    local status="$1" vmids_json="${2:-[]}" phase="${3:-}"
    local phase_field="" payload
    [[ -n "$phase" ]] && phase_field=",\"phase\":\"${phase}\""
    payload=$(cat <<JSON
{"status":"${status}","active_vmids":${vmids_json}${phase_field},"updated_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON
)
    atomic_write_file "$RECLONE_STATE_CACHE" "$payload"
}

# ── Argument parsing ──────────────────────────────────────────────────────────
_server_arg_given=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version|-v) echo "client-sim-proxmox-agent v${AGENT_VERSION}"; exit 0 ;;
        --server=*) SERVER_URL="${1#--server=}"; _server_arg_given=1; shift ;;
        --server)   SERVER_URL="${2:-}"; _server_arg_given=1; shift 2 ;;
        *) shift ;;
    esac
done

# Persist --server to env file so systemd restarts don't need the arg each time
if [[ "$_server_arg_given" -eq 1 ]] && [[ -n "$SERVER_URL" ]]; then
    sed -i '/^CLIENT_SIM_SERVER_URL=/d' "$ENV_FILE" 2>/dev/null || true
    echo "CLIENT_SIM_SERVER_URL=${SERVER_URL}" >> "$ENV_FILE"
fi

# ── Auto-detect hub SERVER_URL from LXC 1001 ──────────────────────────────────
# Hub always runs in LXC container ID 1001. Read its IP from pct config.
auto_detect_hub_url() {
    command -v pct &>/dev/null || { log "ERROR: pct not found — not running on Proxmox?"; return 1; }
    if ! pct status 1001 &>/dev/null; then
        log "ERROR: LXC container 1001 does not exist on this host."
        return 1
    fi
    # Wait for container to reach running state (e.g. after a restore)
    local wait_secs=0 max_wait=120
    while [[ $wait_secs -lt $max_wait ]]; do
        local ct_status
        ct_status=$(pct status 1001 2>/dev/null | awk '{print $2}' || true)
        [[ "$ct_status" == "running" ]] && break
        log "LXC 1001 is '${ct_status:-unknown}' — waiting for it to come online... (${wait_secs}s/${max_wait}s)"
        sleep 5
        (( wait_secs += 5 ))
    done
    if [[ $wait_secs -ge $max_wait ]]; then
        log "ERROR: LXC 1001 did not reach running state within ${max_wait}s."
        return 1
    fi
    local ct_ip
    # Try RFC-1918 static IP from pct config (skip 169.x DHCP-server IPs and dhcp keyword)
    ct_ip=$(pct config 1001 2>/dev/null \
        | grep -oP 'ip=\K[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' \
        | grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' \
        | head -1 || true)
    # If DHCP (or no static IP), read the actual assigned IP from inside the container
    if [[ -z "$ct_ip" ]]; then
        log "LXC 1001 has no static IP — reading DHCP-assigned address from inside container..."
        ct_ip=$(pct exec 1001 -- bash -c "hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' | head -1" 2>/dev/null || true)
        # Fallback: any non-loopback, non-link-local IPv4
        if [[ -z "$ct_ip" ]]; then
            ct_ip=$(pct exec 1001 -- bash -c "hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|169\.|::)' | grep -E '^[0-9]+\.' | head -1" 2>/dev/null || true)
        fi
    fi
    if [[ -z "$ct_ip" ]]; then
        log "ERROR: Could not determine IP for LXC 1001 (tried pct config and hostname -I)."
        return 1
    fi
    SERVER_URL="http://${ct_ip}:8000"
    SERVER_URL_AUTO_DETECTED=1
    persist_runtime_server_url
    log "Auto-detected hub at ${SERVER_URL} (LXC 1001)"
    return 0
}

if [[ -n "$SERVER_URL" ]]; then
    log "Using server: ${SERVER_URL}"
    # Write the cache file so future restarts can fall back to this URL
    # even when LXC 1001 is unavailable (covers both --server and env-file cases).
    persist_runtime_server_url
else
    log "No --server specified — auto-detecting from LXC 1001..."
    # Remove any stale cached URL from env file (IP may have changed via DHCP)
    if [[ -f "$ENV_FILE" ]]; then
        sed -i '/^CLIENT_SIM_SERVER_URL=/d' "$ENV_FILE" 2>/dev/null || true
    fi
    if ! auto_detect_hub_url; then
        # LXC 1001 unavailable — fall back to last-known URL written by a previous
        # successful run.  This prevents crash-loops when the spoke container is
        # temporarily stopped or the host doesn't have LXC 1001 at all but was
        # previously pointed at a spoke via --server.
        _cached_url=""
        if [[ -f "$HUB_SERVER_URL_FILE" ]]; then
            _cached_url=$(tr -d '[:space:]' < "$HUB_SERVER_URL_FILE" 2>/dev/null || true)
        fi
        if [[ -n "$_cached_url" ]]; then
            log "WARNING: LXC 1001 unavailable — using last-known server URL: ${_cached_url}"
            SERVER_URL="$_cached_url"
            SERVER_URL_AUTO_DETECTED=1
        else
            log "ERROR: Hub URL could not be determined and no cached URL found."
            log "Usage: $0 --server https://<hub-ip>:8443"
            exit 1
        fi
    fi
fi

record_spoke_failure() {
    local state_dir="/var/lib/client-sim"
    local fail_since_file="${state_dir}/spoke-fail-since"
    local cooldown_file="${state_dir}/reinstall-last"
    local reinstall_cooldown_secs=7200
    mkdir -p "$state_dir"

    local now
    now=$(date +%s)

    if [[ ! -f "$fail_since_file" ]]; then
        echo "$now" > "$fail_since_file"
        log "AUTO-REPAIR: Spoke contact lost — starting offline timer (threshold: ${SPOKE_OFFLINE_REINSTALL_SECS}s)"
        return
    fi

    local fail_since elapsed
    fail_since=$(cat "$fail_since_file" 2>/dev/null || echo "$now")
    [[ "$fail_since" =~ ^[0-9]+$ ]] || fail_since="$now"
    elapsed=$(( now - fail_since ))

    if (( elapsed > 0 && elapsed % 600 < 30 )); then
        log "AUTO-REPAIR: Spoke still unreachable — offline for ${elapsed}s / ${SPOKE_OFFLINE_REINSTALL_SECS}s before reinstall"
    fi

    if (( elapsed < SPOKE_OFFLINE_REINSTALL_SECS )); then
        return
    fi

    if [[ -f "$cooldown_file" ]]; then
        local last_reinstall cooldown_elapsed
        last_reinstall=$(cat "$cooldown_file" 2>/dev/null || echo "0")
        [[ "$last_reinstall" =~ ^[0-9]+$ ]] || last_reinstall=0
        cooldown_elapsed=$(( now - last_reinstall ))
        if (( cooldown_elapsed < reinstall_cooldown_secs )); then
            log "AUTO-REPAIR: Threshold exceeded but cooldown active (last reinstall ${cooldown_elapsed}s ago — cooldown is ${reinstall_cooldown_secs}s)"
            return
        fi
    fi

    log "AUTO-REPAIR: Spoke unreachable for ${elapsed}s — triggering reinstall"
    trigger_spoke_reinstall
}

clear_spoke_failure() {
    local fail_since_file="/var/lib/client-sim/spoke-fail-since"
    if [[ -f "$fail_since_file" ]]; then
        log "AUTO-REPAIR: Spoke contact restored — clearing offline timer"
        rm -f "$fail_since_file"
    fi
}

trigger_spoke_reinstall() {
    local state_dir="/var/lib/client-sim"
    local installer_url="https://raw.githubusercontent.com/lbockenstedt/cs/main/proxmox/install-proxmox-agent.sh"
    local installer_file="${state_dir}/install-proxmox-agent-repair.sh"
    mkdir -p "$state_dir"

    log "AUTO-REPAIR: Downloading installer from ${installer_url}..."
    if ! curl -fsSL --connect-timeout 15 --max-time 60 "$installer_url" -o "$installer_file"; then
        log "AUTO-REPAIR: Failed to download installer — will retry next cycle"
        return 1
    fi
    if ! bash -n "$installer_file"; then
        log "AUTO-REPAIR: Installer failed syntax check — aborting"
        rm -f "$installer_file"
        return 1
    fi
    chmod +x "$installer_file"
    echo "$(date +%s)" > "${state_dir}/reinstall-last"
    log "AUTO-REPAIR: Launching reinstaller — agent will restart"
    nohup bash "$installer_file" >> /var/log/client-sim-proxmox-agent-repair.log 2>&1 &
    sleep 3
    log "AUTO-REPAIR: Exiting to allow installer to take over"
    kill -TERM "$AGENT_SHELL_PID" 2>/dev/null || true
    exit 0
}

curl_api() {
    local method="$1" path="$2" data="${3:-}"
    refresh_runtime_server_url
    local args=(-sS --max-time 15 -X "$method" "${SERVER_URL}${path}" -H "Content-Type: application/json")
    local response http_code body curl_exit=0
    [[ -n "$API_KEY" ]] && args+=(-H "X-API-Key: $API_KEY")
    [[ -n "$data" ]] && args+=(-d "$data")
    response=$(curl "${args[@]}" -w $'\n%{http_code}') || curl_exit=$?
    http_code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    record_hub_contact_result "$http_code" "$curl_exit" >&2
    if (( curl_exit != 0 )); then
        log "curl_api ERROR: ${method} ${path} curl exited ${curl_exit} (HTTP ${http_code})"
        return 1
    fi
    if [[ "$http_code" =~ ^[0-9]{3}$ ]] && (( http_code >= 400 )); then
        log "curl_api ERROR: ${method} ${path} returned HTTP ${http_code}"
        return 1
    fi
    printf '%s' "$body"
}

json_field() {
    local payload="$1" field="$2" default_value="${3:-}"
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$payload" | jq -r --arg f "$field" --arg d "$default_value" '
            if type == "object" and has($f) and .[$f] != null then .[$f] else $d end
        ' 2>/dev/null || true
        return 0
    fi
    python3 -c "import json,sys; data=json.loads(sys.argv[1] or '{}'); value=data.get(sys.argv[2], sys.argv[3]); print(str(value))" "$payload" "$field" "$default_value" 2>/dev/null || true
}

save_api_key() {
    local key="$1" escaped_key
    escaped_key=$(sed_escape "$key")
    if grep -q '^CLIENT_SIM_API_KEY=' "$ENV_FILE" 2>/dev/null; then
        sed -i "s/^CLIENT_SIM_API_KEY=.*/CLIENT_SIM_API_KEY=${escaped_key}/" "$ENV_FILE"
    else
        echo "CLIENT_SIM_API_KEY=${key}" >> "$ENV_FILE"
    fi
    API_KEY="$key"
}

save_repo_branch() {
    local branch="$1" escaped_branch
    escaped_branch=$(sed_escape "$branch")
    if grep -q '^CLIENT_SIM_REPO_BRANCH=' "$ENV_FILE" 2>/dev/null; then
        sed -i "s/^CLIENT_SIM_REPO_BRANCH=.*/CLIENT_SIM_REPO_BRANCH=${escaped_branch}/" "$ENV_FILE"
    else
        echo "CLIENT_SIM_REPO_BRANCH=${branch}" >> "$ENV_FILE"
    fi
}

normalize_repo_raw_for_branch() {
    local repo_raw="${1:-}" branch="${2:-main}"
    repo_raw="${repo_raw%/}"
    [[ -n "$repo_raw" ]] || return 1
    case "$repo_raw" in
        */"$branch") printf '%s\n' "$repo_raw" ;;
        *) printf '%s\n' "$repo_raw/$branch" ;;
    esac
}

schedule_agent_restart() {
    local service_name="client-sim-proxmox-agent"
    local systemctl_bin
    systemctl_bin=$(command -v systemctl || echo /bin/systemctl)
    if command -v systemd-run >/dev/null 2>&1; then
        local restart_unit="client-sim-proxmox-agent-restart-$(date +%s)"
        if systemd-run --quiet --collect --unit "$restart_unit" --on-active=2s "$systemctl_bin" restart "$service_name"; then
            log "Scheduled agent restart via ${restart_unit}"
            return 0
        fi
        log "WARNING: systemd-run restart scheduling failed; falling back to nohup"
    fi
    nohup bash -lc "sleep 2; exec \"$systemctl_bin\" restart \"$service_name\"" >/dev/null 2>&1 &
    return 0
}

clear_api_key() {
    if grep -q '^CLIENT_SIM_API_KEY=' "$ENV_FILE" 2>/dev/null; then
        sed -i 's/^CLIENT_SIM_API_KEY=.*/CLIENT_SIM_API_KEY=/' "$ENV_FILE"
    fi
    API_KEY=""
}

curl_api_status() {
    local method="$1" path="$2" data="${3:-}"
    refresh_runtime_server_url
    local args=(-sS --max-time 15 -X "$method" "${SERVER_URL}${path}" -H "Content-Type: application/json" -w $'\n%{http_code}')
    local response http_code curl_exit=0
    [[ -n "$API_KEY" ]] && args+=(-H "X-API-Key: $API_KEY")
    [[ -n "$data" ]] && args+=(-d "$data")
    response=$(curl "${args[@]}") || curl_exit=$?
    http_code="${response##*$'\n'}"
    record_hub_contact_result "$http_code" "$curl_exit" >&2
    printf '%s' "$response"
    (( curl_exit == 0 ))
}

post_progress_event() {
    local event_type="$1" payload_json="${2:-{}}"
    local event_json event_file
    mkdir -p "$PROGRESS_EVENT_QUEUE_DIR" || return 0
    event_json=$(python3 - "$event_type" "$payload_json" <<'PY' 2>/dev/null || true
import json, sys
message_type = sys.argv[1]
raw_payload = sys.argv[2] if len(sys.argv) > 2 else '{}'
try:
    payload = json.loads(raw_payload) if raw_payload else {}
except Exception:
    payload = {}
if not isinstance(payload, dict):
    payload = {}
print(json.dumps({"type": message_type, "payload": payload}))
PY
)
    [[ -n "$event_json" ]] || return 0
    event_file="${PROGRESS_EVENT_QUEUE_DIR}/$(date +%s%N)-$$-$RANDOM.json"
    atomic_write_file "$event_file" "$event_json" || return 0
}

emit_backup_progress() {
    local job_id="$1" vm_id="$2" status="$3" pct="$4" step="$5" error="${6:-}" spoke_id="${7:-}"
    local payload
    payload=$(python3 - "$job_id" "$vm_id" "$status" "$pct" "$step" "$error" "$spoke_id" <<'PY' 2>/dev/null || true
import json, sys
payload = {
    "job_id": sys.argv[1],
    "vm_id": int(sys.argv[2]) if str(sys.argv[2]).isdigit() else sys.argv[2],
    "status": sys.argv[3],
    "pct": max(0, min(100, int(float(sys.argv[4] or 0)))),
    "step": sys.argv[5],
}
if sys.argv[6]:
    payload["error"] = sys.argv[6]
if sys.argv[7]:
    payload["spoke_id"] = sys.argv[7]
print(json.dumps(payload))
PY
)
    [[ -n "$payload" ]] && post_progress_event "backup_progress" "$payload"
}

emit_reseed_progress() {
    local job_id="$1" status="$2" step="$3" error="${4:-}"
    local payload
    payload=$(python3 - "$job_id" "$status" "$step" "$error" <<'PY' 2>/dev/null || true
import json, sys
payload = {
    "job_id": sys.argv[1],
    "status": sys.argv[2],
    "step": sys.argv[3],
}
if sys.argv[4]:
    payload["error"] = sys.argv[4]
print(json.dumps(payload))
PY
)
    [[ -n "$payload" ]] && post_progress_event "reseed_progress" "$payload"
}

normalize_command_name() {
    printf '%s' "${1//-/_}"
}

json_payload() {
    python3 - "$HOSTNAME" "$@" <<'PY'
import json
import sys
print(json.dumps({
    "hostname": sys.argv[1],
    "id": sys.argv[2],
    "status": sys.argv[3],
    "message": sys.argv[4],
}))
PY
}

ack_inbox_command() {
    local cmd_id="$1" status="$2" message="${3:-}"
    local payload response_with_status http_status body attempt
    payload=$(json_payload "$cmd_id" "$status" "$message") || return 1
    for attempt in 1 2 3; do
        response_with_status=$(curl_api_status POST /api/inbox/ack "$payload" 2>/dev/null || true)
        http_status="${response_with_status##*$'\n'}"
        body="${response_with_status%$'\n'*}"
        case "$http_status" in
            200)
                log "ACK: ${cmd_id} status=${status}"
                return 0
                ;;
            404)
                log "ACK skipped: ${cmd_id} already gone from server queue"
                return 0
                ;;
            202|401|403)
                handle_auth_failure "$http_status" "/api/inbox/ack"
                ;;
            "")
                log "WARNING: ACK ${cmd_id} attempt ${attempt} — empty/no response (curl error?)"
                ;;
            *)
                log "WARNING: ACK ${cmd_id} attempt ${attempt} returned HTTP ${http_status} ${body:+body=${body:0:160}}"
                ;;
        esac
        sleep 2
    done
    log "ERROR: failed to ACK command ${cmd_id} after 3 attempts"
    return 1
}

handle_auth_failure() {
    local status="$1" endpoint="$2"
    case "$status" in
        202|401|403)
            log "Auth/reset required after ${endpoint} (HTTP ${status}) — re-registering agent"
            clear_api_key
            register_and_wait_for_key
            return 0
            ;;
    esac
    return 1
}

register_and_wait_for_key() {
    local my_hostname response response_with_status approved key status poll_response poll_with_status poll_approved poll_key poll_status
    my_hostname=$(hostname)
    log "No API key found. Registering with server..."

    while true; do
        refresh_runtime_server_url
        response_with_status=$(curl -sS --max-time 10 -X POST "${SERVER_URL}/api/proxmox/register" \
            -H "Content-Type: application/json" \
            -d "{\"hostname\":\"$my_hostname\"}" \
            -w $'\n%{http_code}' 2>/dev/null || true)
        status="${response_with_status##*$'\n'}"
        response="${response_with_status%$'\n'*}"
        [[ "$response" == "$response_with_status" ]] && response='{}'
        record_hub_contact_result "$status"

        approved=$(json_field "$response" approved)
        key=$(json_field "$response" key)

        if [[ "$approved" == "True" || "$approved" == "true" ]] && [[ -n "$key" ]]; then
            log "Approved! Saving API key."
            save_api_key "$key"
            return 0
        fi

        log "Pending approval... checking again in 30s"
        sleep 30

        refresh_runtime_server_url
        poll_with_status=$(curl -sS --max-time 10 \
            "${SERVER_URL}/api/proxmox/key?hostname=$my_hostname" \
            -w $'\n%{http_code}' 2>/dev/null || true)
        poll_status="${poll_with_status##*$'\n'}"
        poll_response="${poll_with_status%$'\n'*}"
        [[ "$poll_response" == "$poll_with_status" ]] && poll_response='{}'
        record_hub_contact_result "$poll_status"
        poll_approved=$(json_field "$poll_response" approved)
        poll_key=$(json_field "$poll_response" key)

        if [[ "$poll_approved" == "True" || "$poll_approved" == "true" ]] && [[ -n "$poll_key" ]]; then
            log "Approved! Saving API key."
            save_api_key "$poll_key"
            return 0
        fi
    done
}

ensure_state_file() {
    touch "$STATE_FILE"
}

json_from_records() {
    local kind="$1"
    shift || true
    python3 - "$kind" "$@" <<'PY'
import json
import sys

kind = sys.argv[1]
items = []
for raw in sys.argv[2:]:
    parts = raw.split("\t")
    if kind == "unknown":
        bus_path, vidpid, name = (parts + ["", "", ""])[:3]
        items.append({"bus_path": bus_path, "vidpid": vidpid, "name": name})
    else:
        vmid, bus_path, missing_since, name, vidpid, prov_status, fail_count, quarantined_at = (parts + ["", "", "", "", "", "", "", ""])[:8]
        items.append({
            "vmid": int(vmid) if vmid else None,
            "bus_path": bus_path,
            "missing_since": int(missing_since) if missing_since else None,
            "name": name,
            "vidpid": vidpid,
            "prov_status": prov_status or "active",
            "fail_count": int(fail_count) if fail_count else 0,
            "quarantined_at": int(quarantined_at) if quarantined_at else None,
        })
print(json.dumps(items))
PY
}

find_label_for_vidpid() {
    local vidpid="$1"
    if [[ -n "${CERTIFIED_LABELS[$vidpid]:-}" ]]; then
        printf '%s' "${CERTIFIED_LABELS[$vidpid]}"
    else
        printf '%s' "$vidpid"
    fi
}

CLIENT_SETUP_CONF="/etc/pve/scripts/client-setup.conf"

get_vm_name() {
    local vmid="$1" name=""
    if [[ -f "$CLIENT_SETUP_CONF" ]]; then
        name=$(awk -v sec="[c${vmid}]" '
            $0 == sec        { found=1; next }
            found && /^\[/   { exit }
            found && /^vm_name=/ { sub(/^vm_name=[ \t]*/, ""); print; exit }
        ' "$CLIENT_SETUP_CONF")
    fi
    printf '%s' "${name:-sim-client}"
}

# Read the full hostname (Proxmox VM name) directly from qm config so we never
# have to reconstruct it from vm_name + vmid — hostname no longer contains the VMID.
get_full_hostname() {
    local vmid="$1"
    qm config "$vmid" 2>/dev/null | awk '/^name:/{print $2; exit}'
}

device_name_from_sysfs() {
    local dev="$1" manufacturer="" product="" name
    [[ -f "$dev/manufacturer" ]] && manufacturer=$(tr -d '\n' < "$dev/manufacturer")
    [[ -f "$dev/product" ]] && product=$(tr -d '\n' < "$dev/product")
    name="$manufacturer $product"
    name=$(echo "$name" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')
    if [[ -z "$name" ]]; then
        name=$(basename "$dev")
    fi
    printf '%s' "$name"
}

expand_vmid_spec() {
    local spec="${1:-}" part start end vmid
    local -a entries=()
    [[ -n "$spec" ]] || return 0
    IFS=',' read -ra parts <<< "$spec"
    for part in "${parts[@]}"; do
        part="${part//[[:space:]]/}"
        [[ -n "$part" ]] || continue
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            start=$((10#${BASH_REMATCH[1]}))
            end=$((10#${BASH_REMATCH[2]}))
            (( start <= end )) || continue
            if (( end - start > 1000 )); then
                end=$((start + 1000))
            fi
            for ((vmid=start; vmid<=end; vmid++)); do
                entries+=("$vmid")
            done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
            entries+=("$((10#$part))")
        fi
    done
    [[ ${#entries[@]} -gt 0 ]] || return 0
    printf '%s\n' "${entries[@]}" | sort -nu
}

vmid_is_runnable_template() {
    local vmid="$1"
    qm status "$vmid" >/dev/null 2>&1 && qm config "$vmid" 2>/dev/null | grep -q '^template: 1'
}

find_template_vmid_from_spec() {
    local spec="${1:-}" candidate
    [[ -n "$spec" ]] || return 1
    while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        if vmid_is_runnable_template "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done < <(expand_vmid_spec "$spec")
    return 1
}

find_lowest_available_template_vmid() {
    local conf vmid
    while IFS= read -r conf; do
        [[ -n "$conf" ]] || continue
        vmid="$(basename "$conf" .conf)"
        [[ "$vmid" =~ ^[0-9]+$ ]] || continue
        if vmid_is_runnable_template "$vmid"; then
            printf '%s' "$vmid"
            return 0
        fi
    done < <(compgen -G '/etc/pve/qemu-server/*.conf' | sort -V)
    return 1
}

resolve_template_vmid() {
    local spec="${1:-}" spec_seen="${2:-0}" fallback_id="${3:-}" resolved=""
    if [[ -n "$spec" ]]; then
        resolved=$(find_template_vmid_from_spec "$spec" || true)
    elif [[ "$spec_seen" != "1" && "$fallback_id" =~ ^[0-9]+$ ]] && vmid_is_runnable_template "$fallback_id"; then
        resolved="$fallback_id"
    fi
    if [[ -z "$resolved" ]]; then
        resolved=$(find_lowest_available_template_vmid || true)
    fi
    [[ -n "$resolved" ]] && printf '%s' "$resolved"
}

refresh_usb_config() {
    local response parsed kind a b c
    response=$(curl_api GET "/api/proxmox/usb-config?hostname=$(hostname)" "" 2>/dev/null || echo '{}')
    parsed=$(python3 - "$response" <<'PY' 2>/dev/null || true
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else '{}'
try:
    data = json.loads(raw)
except Exception:
    data = {}

sim_phy = str(data.get("sim_phy", "wireless")).strip().lower() or "wireless"
if sim_phy not in {"wireless", "ethernet", "any"}:
    sim_phy = "wireless"
use_all_dongles = str(data.get("use_all_dongles", False)).strip().lower()
print("CFG\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
    str(data.get("auto_provision", "off")).lower(),
    int(data.get("missing_timeout", 60) or 60),
    int(data.get("image1_template_id", data.get("template_id", 100)) or 100),
    int(data.get("image2_template_id", 200) or 200),
    max(0, min(100, int(data.get("image1_pct", 50) or 50))),
    sim_phy,
    max(1, int(data.get("reclone_concurrency", 1) or 1)),
    max(1, min(4094, int(data.get("l1_vlan_start", 100) or 100))),
    max(1, min(4094, int(data.get("l1_vlan_end", 199) or 199))),
    max(1, min(256, int(data.get("max_slots", 24) or 24))),
    use_all_dongles,
    max(0, int(data.get("vmid_start", 0) or 0)),
    max(0, min(99, int(data.get("vm_set_override", 0) or 0))),
))
for idx, key in ((1, "image1_template_spec"), (2, "image2_template_spec")):
    spec = str(data.get(key, "") or "").replace("\t", " ").replace("\n", " ").strip()
    print(f"SPEC\t{idx}\t{spec}")
for item in data.get("vidpids", []) or []:
    if not isinstance(item, dict):
        continue
    vidpid = str(item.get("vidpid", "")).strip().lower()
    if not vidpid:
        continue
    dtype = str(item.get("type", "wireless")).strip().lower() or "wireless"
    label = str(item.get("label", "")).replace("\t", " ").strip()
    print(f"CERT\t{vidpid}\t{dtype}\t{label}")
for vidpid in data.get("ignored_vidpids", []) or []:
    value = str(vidpid).strip().lower()
    if value:
        print(f"IGN\t{value}")
# Agent watchdog settings (AGNT line — older agents silently ignore unknown line types)
agnt_enabled   = str(data.get("guest_agent_watchdog_enabled", "on")).lower()
agnt_grace     = int(data.get("guest_agent_grace_minutes", 20) or 20)
agnt_interval  = int(data.get("guest_agent_check_interval_minutes", 10) or 10)
agnt_reboot    = int(data.get("guest_agent_reboot_after_minutes", 10) or 10)
agnt_reclone   = int(data.get("guest_agent_reclone_after_minutes", 30) or 30)
agnt_reboot_en = str(data.get("watchdog_reboot_enabled", "on")).lower()
print(f"AGNT\t{agnt_enabled}\t{agnt_grace}\t{agnt_interval}\t{agnt_reboot}\t{agnt_reclone}\t{agnt_reboot_en}")
# Resource provision thresholds (RSRC line — older agents silently ignore unknown line types)
cpu_thr = max(0, min(100, int(data.get("cpu_provision_threshold", 80) or 80)))
mem_thr = max(0, min(100, int(data.get("mem_provision_threshold", 80) or 80)))
print(f"RSRC\t{cpu_thr}\t{mem_thr}")
PY
)

    CERTIFIED_TYPES=()
    CERTIFIED_LABELS=()
    IGNORED_VIDPIDS=()
    AUTO_PROVISION="off"
    MISSING_TIMEOUT=60
    IMAGE1_TEMPLATE_ID=100
    IMAGE1_TEMPLATE_SPEC=""
    IMAGE1_TEMPLATE_SPEC_SEEN=0
    IMAGE2_TEMPLATE_ID=200
    IMAGE2_TEMPLATE_SPEC=""
    IMAGE2_TEMPLATE_SPEC_SEEN=0
    IMAGE1_PCT=50
    VM_SET_OVERRIDE=0
    SIM_PHY="wireless"
    RECLONE_CONCURRENCY=1
    L1_VLAN_START=100
    L1_VLAN_END=199
    MAX_USB_SLOTS=24
    USE_ALL_DONGLES="false"
    CPU_PROVISION_THRESHOLD=80
    MEM_PROVISION_THRESHOLD=80
    CPU_RAMP_CEILING=90

    while IFS=$'\t' read -r kind a b c d e f g h i j k l m; do
        [[ -z "$kind" ]] && continue
        case "$kind" in
            CFG)
                AUTO_PROVISION="$a"
                MISSING_TIMEOUT="$b"
                IMAGE1_TEMPLATE_ID="$c"
                IMAGE2_TEMPLATE_ID="$d"
                IMAGE1_PCT="${e:-50}"
                SIM_PHY="${f:-wireless}"
                RECLONE_CONCURRENCY="${g:-1}"
                L1_VLAN_START="${h:-100}"
                L1_VLAN_END="${i:-199}"
                MAX_USB_SLOTS="${j:-24}"
                USE_ALL_DONGLES="${k:-false}"
                local _vmid_start_cfg="${l:-0}"
                VM_SET_OVERRIDE="${m:-0}"
                recompute_vmid_range "$_vmid_start_cfg"
                ;;
            SPEC)
                if [[ "$a" == "1" ]]; then
                    IMAGE1_TEMPLATE_SPEC="$b"
                    IMAGE1_TEMPLATE_SPEC_SEEN=1
                elif [[ "$a" == "2" ]]; then
                    IMAGE2_TEMPLATE_SPEC="$b"
                    IMAGE2_TEMPLATE_SPEC_SEEN=1
                fi
                ;;
            CERT)
                CERTIFIED_TYPES["$a"]="$b"
                CERTIFIED_LABELS["$a"]="$c"
                ;;
            IGN)
                IGNORED_VIDPIDS["$a"]=1
                ;;
            AGNT)
                # VM guest agent watchdog settings
                GUEST_AGENT_WATCHDOG_ENABLED="${a:-on}"
                GUEST_AGENT_GRACE_MINUTES="${b:-20}"
                GUEST_AGENT_CHECK_INTERVAL_MINUTES="${c:-10}"
                GUEST_AGENT_REBOOT_AFTER_MINUTES="${d:-10}"
                GUEST_AGENT_RECLONE_AFTER_MINUTES="${e:-30}"
                WATCHDOG_REBOOT_ENABLED="${f:-on}"
                ;;
            RSRC)
                # Resource-based provisioning thresholds
                CPU_PROVISION_THRESHOLD="${a:-80}"
                MEM_PROVISION_THRESHOLD="${b:-80}"
                CPU_RAMP_CEILING="${c:-90}"
                ;;
        esac
    done <<< "$parsed"

    local _resolved_template
    _resolved_template=$(resolve_template_vmid "$IMAGE1_TEMPLATE_SPEC" "$IMAGE1_TEMPLATE_SPEC_SEEN" "$IMAGE1_TEMPLATE_ID" || true)
    if [[ -n "$_resolved_template" ]]; then
        IMAGE1_TEMPLATE_ID="$_resolved_template"
    fi
    _resolved_template=$(resolve_template_vmid "$IMAGE2_TEMPLATE_SPEC" "$IMAGE2_TEMPLATE_SPEC_SEEN" "$IMAGE2_TEMPLATE_ID" || true)
    if [[ -n "$_resolved_template" ]]; then
        IMAGE2_TEMPLATE_ID="$_resolved_template"
    fi

}

load_state_file() {
    ensure_state_file
    STATE_BUS_TO_VMID=()
    STATE_VMID_TO_BUS=()
    STATE_MISSING_BY_BUS=()
    STATE_VMID_TO_IMAGE=()
    STATE_VIDPID_BY_BUS=()
    local vmid bus_path missing_since image_num vidpid line rest
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" ]] && continue

        # Preserve empty tab-delimited fields; `read` with tab IFS collapses them and
        # turns a blank missing_since into the image number on every reload.
        vmid="${line%%$'\t'*}"
        rest="${line#*$'\t'}"
        [[ "$rest" == "$line" ]] && continue
        bus_path="${rest%%$'\t'*}"
        rest="${rest#*$'\t'}"
        missing_since="${rest%%$'\t'*}"
        if [[ "$rest" == *$'\t'* ]]; then
            rest="${rest#*$'\t'}"
            image_num="${rest%%$'\t'*}"
            if [[ "$rest" == *$'\t'* ]]; then
                vidpid="${rest#*$'\t'}"
            else
                vidpid=""
            fi
        else
            image_num="1"
            vidpid=""
        fi

        [[ -z "$vmid" || -z "$bus_path" ]] && continue
        STATE_BUS_TO_VMID["$bus_path"]="$vmid"
        STATE_VMID_TO_BUS["$vmid"]="$bus_path"
        STATE_MISSING_BY_BUS["$bus_path"]="$missing_since"
        STATE_VMID_TO_IMAGE["$vmid"]="${image_num:-1}"
        [[ -n "$vidpid" ]] && STATE_VIDPID_BY_BUS["$bus_path"]="$vidpid"
    done < "$STATE_FILE"
    prune_stale_state_vmids
}

prune_stale_state_vmids() {
    local qm_vmids vmid bus_path stale_count=0
    local -A existing_vmids=()

    if ! qm_vmids=$({ qm list 2>/dev/null || true; pct list 2>/dev/null || true; } | awk '$1 ~ /^[0-9]+$/ { print $1 }'); then
        log "WARNING: failed to enumerate existing guests; skipping stale VM state cleanup"
        return
    fi

    while IFS= read -r vmid; do
        [[ -n "$vmid" ]] && existing_vmids["$vmid"]=1
    done <<< "$qm_vmids"

    for vmid in "${!STATE_VMID_TO_BUS[@]}"; do
        [[ -n "${existing_vmids[$vmid]:-}" ]] && continue
        bus_path="${STATE_VMID_TO_BUS[$vmid]:-}"
        unset "STATE_VMID_TO_BUS[$vmid]"
        unset "STATE_VMID_TO_IMAGE[$vmid]"
        if [[ -n "$bus_path" ]]; then
            unset "STATE_BUS_TO_VMID[$bus_path]"
            unset "STATE_MISSING_BY_BUS[$bus_path]"
        fi
        rm -f "${PROV_DIR}/${vmid}.post_prov_retry" \
              "${PROV_DIR}/${vmid}.provision_done" \
              "${PROV_DIR}/${vmid}.agent_unresponsive" 2>/dev/null || true
        ((stale_count++))
        log "Removed stale VM state for VM $vmid${bus_path:+ (bus $bus_path)}"
    done

    (( stale_count > 0 )) && save_state_file
}

save_state_file() {
    ensure_state_file
    (
        if command -v flock >/dev/null 2>&1; then
            flock -x 200
        fi
        {
            for vmid in "${!STATE_VMID_TO_BUS[@]}"; do
                local_bus="${STATE_VMID_TO_BUS[$vmid]}"
                printf '%s\t%s\t%s\t%s\t%s\n' "$vmid" "$local_bus" "${STATE_MISSING_BY_BUS[$local_bus]:-}" "${STATE_VMID_TO_IMAGE[$vmid]:-1}" "${STATE_VIDPID_BY_BUS[$local_bus]:-}"
            done | sort -n
        } > "$STATE_FILE"
    ) 200>"$STATE_LOCK_FILE"
}

load_excluded_buses() {
    STATE_EXCLUDED_BUS=()
    [[ -f "$EXCLUDED_BUS_FILE" ]] || return 0
    local _bus
    while IFS= read -r _bus || [[ -n "$_bus" ]]; do
        [[ -z "$_bus" || "$_bus" == '#'* ]] && continue
        STATE_EXCLUDED_BUS["$_bus"]="1"
    done < "$EXCLUDED_BUS_FILE"
}

save_excluded_buses() {
    {
        for _bus in "${!STATE_EXCLUDED_BUS[@]}"; do
            printf '%s\n' "$_bus"
        done | sort
    } > "$EXCLUDED_BUS_FILE"
}

# ── USB quarantine: track buses with repeated dongle-missing failures ──────────
# A bus is quarantined after USB_QUARANTINE_THRESHOLD consecutive missing-timeout
# teardowns (dongle absent for the full MISSING_TIMEOUT window). Quarantined buses
# are skipped during provisioning. Quarantine auto-clears when the bus has been
# physically absent for 2× MISSING_TIMEOUT since quarantine (dongle replaced).
# Manual clear via hub command: clear_usb_quarantine [bus_path].

load_usb_quarantine() {
    USB_FAIL_COUNT=()
    USB_QUARANTINED=()
    [[ -f "$USB_QUARANTINE_FILE" ]] || return 0
    local _bus _fc _qa _changed=0
    while IFS=$'\t' read -r _bus _fc _qa; do
        [[ -z "$_bus" ]] && continue
        USB_FAIL_COUNT["$_bus"]="${_fc:-0}"
        [[ -n "$_qa" ]] && USB_QUARANTINED["$_bus"]="$_qa"
    done < <(python3 -c "
import json, sys
try:
    d = json.load(open('$USB_QUARANTINE_FILE'))
    for bus, info in d.items():
        fc = info.get('fail_count', 0)
        qa = info.get('quarantined_at', '') or ''
        print(f'{bus}\t{fc}\t{qa}')
except Exception:
    pass
" 2>/dev/null)
    # Auto-clear quarantined buses physically absent for > 2× MISSING_TIMEOUT
    # (indicates dongle was replaced; allow the new dongle to provision)
    local _now _auto_clear_secs _qa
    _now=$(date +%s)
    _auto_clear_secs=$(( MISSING_TIMEOUT * 60 * 2 ))
    for _bus in "${!USB_QUARANTINED[@]}"; do
        _qa="${USB_QUARANTINED[$_bus]}"
        if [[ -z "${PRESENT_BUSES[$_bus]:-}" ]] && (( _now - _qa > _auto_clear_secs )); then
            log "USB bus $_bus quarantine auto-cleared (absent > $((MISSING_TIMEOUT * 2)) min since quarantine — dongle assumed replaced)"
            unset "USB_QUARANTINED[$_bus]"
            USB_FAIL_COUNT["$_bus"]=0
            _changed=1
        fi
    done
    (( _changed )) && save_usb_quarantine
}

save_usb_quarantine() {
    local _tmp_data="" _bus
    for _bus in "${!USB_FAIL_COUNT[@]}"; do
        local _fc="${USB_FAIL_COUNT[$_bus]:-0}"
        local _qa="${USB_QUARANTINED[$_bus]:-}"
        _tmp_data+="${_bus}"$'\t'"${_fc}"$'\t'"${_qa}"$'\n'
    done
    local _payload
    _payload=$(printf '%s' "$_tmp_data" | python3 -c "
import json, sys
data = {}
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line:
        continue
    parts = line.split('\t')
    bus = parts[0] if len(parts) > 0 else ''
    fc  = parts[1] if len(parts) > 1 else '0'
    qa  = parts[2] if len(parts) > 2 else ''
    if not bus:
        continue
    data[bus] = {
        'fail_count': int(fc) if fc.isdigit() else 0,
        'quarantined_at': int(qa) if qa and qa.isdigit() else None,
    }
print(json.dumps(data, indent=2))
" 2>/dev/null || echo '{}')
    mkdir -p "$(dirname "$USB_QUARANTINE_FILE")"
    atomic_write_file "$USB_QUARANTINE_FILE" "$_payload"
}

record_usb_failure() {
    local _bus="$1"
    local _fc=$(( ${USB_FAIL_COUNT[$_bus]:-0} + 1 ))
    USB_FAIL_COUNT["$_bus"]="$_fc"
    if (( _fc >= USB_QUARANTINE_THRESHOLD )) && [[ -z "${USB_QUARANTINED[$_bus]:-}" ]]; then
        USB_QUARANTINED["$_bus"]="$(date +%s)"
        log "USB bus $_bus QUARANTINED after $_fc missing-timeout failures — skipping provisioning"
    else
        log "USB bus $_bus failure count: $_fc / $USB_QUARANTINE_THRESHOLD"
    fi
    save_usb_quarantine
}

clear_usb_quarantine_state() {
    local _target_bus="${1:-}"  # empty = clear all
    local _changed=0
    if [[ -z "$_target_bus" ]]; then
        for _bus in "${!USB_QUARANTINED[@]}"; do
            log "clear_usb_quarantine: clearing quarantine for bus $_bus"
            unset "USB_QUARANTINED[$_bus]"
            USB_FAIL_COUNT["$_bus"]=0
            _changed=1
        done
    elif [[ -n "${USB_QUARANTINED[$_target_bus]:-}" || -n "${USB_FAIL_COUNT[$_target_bus]:-}" ]]; then
        log "clear_usb_quarantine: clearing quarantine for bus $_target_bus"
        unset "USB_QUARANTINED[$_target_bus]"
        USB_FAIL_COUNT["$_target_bus"]=0
        _changed=1
    else
        log "clear_usb_quarantine: bus ${_target_bus} is not quarantined"
    fi
    (( _changed )) && save_usb_quarantine
    return 0
}

# ── VM orphan tracking helpers ─────────────────────────────────────────────────
# When destroy_vm fails repeatedly (DESTROY_MAX_FAILS times) the VM is declared
# an orphan: the USB bus is force-released for re-provisioning and the VMID is
# written to the orphan registry so operators can inspect/clean up manually.

_destroy_fail_count_file() { echo "${PROV_DIR}/${1}.destroy_fails"; }

increment_destroy_fail_count() {
    local _vmid="$1" _bus="$2"
    local _f; _f="$(_destroy_fail_count_file "$_vmid")"
    local _fc=$(( $(cat "$_f" 2>/dev/null || echo 0) + 1 ))
    echo "$_fc" > "$_f"
    if (( _fc >= DESTROY_MAX_FAILS )); then
        log "ERROR: VM $_vmid destroy failed $_fc times — declaring orphan; releasing bus $_bus for re-provisioning"
        # Force-release state so the bus can be re-provisioned
        unset "STATE_BUS_TO_VMID[$_bus]"
        unset "STATE_MISSING_BY_BUS[$_bus]"
        unset "STATE_VIDPID_BY_BUS[$_bus]"
        unset "STATE_VMID_TO_BUS[$_vmid]"
        unset "STATE_VMID_TO_IMAGE[$_vmid]"
        rm -f "$_f" "${PROV_DIR}/${_vmid}" "${PROV_DIR}/${_vmid}.tearing_down"
        # Append to orphan registry (create/merge JSON array)
        local _ts; _ts="$(date +%s)"
        local _entry="{\"vmid\":${_vmid},\"bus\":\"${_bus}\",\"ts\":${_ts}}"
        local _current_json; _current_json="$(cat "$ORPHAN_VMS_FILE" 2>/dev/null || echo '[]')"
        echo "$_current_json" | python3 -c "
import sys, json
arr = json.load(sys.stdin)
arr = [e for e in arr if e.get('vmid') != ${_vmid}]
arr.append(json.loads('${_entry}'))
print(json.dumps(arr))
" > "$ORPHAN_VMS_FILE" 2>/dev/null || echo "[${_entry}]" > "$ORPHAN_VMS_FILE"
        cp "$ORPHAN_VMS_FILE" "$ORPHAN_VMS_CACHE" 2>/dev/null || true
        _state_changed=1
    else
        log "VM $_vmid destroy failed (attempt $_fc / $DESTROY_MAX_FAILS); will retry next cycle"
    fi
}

reset_destroy_fail_count() {
    local _vmid="$1"
    rm -f "$(_destroy_fail_count_file "$_vmid")"
}

remove_orphan_vm_entry() {
    local _vmid="$1"
    [[ -f "$ORPHAN_VMS_FILE" ]] || return 0
    python3 -c "
import sys, json
try:
    arr = json.load(open('$ORPHAN_VMS_FILE'))
    arr = [e for e in arr if e.get('vmid') != ${_vmid}]
    json.dump(arr, open('$ORPHAN_VMS_FILE','w'))
except Exception:
    pass
" 2>/dev/null || true
    cp "$ORPHAN_VMS_FILE" "$ORPHAN_VMS_CACHE" 2>/dev/null || true
}

scan_usb_devices() {
    USB_NAME_BY_BUS=()
    USB_VIDPID_BY_BUS=()
    PRESENT_BUSES=()
    UNKNOWN_USB_LINES=()

    local dev bus_path vid pid vidpid name
    for dev in /sys/bus/usb/devices/*; do
        [[ -d "$dev" ]] || continue
        [[ -f "$dev/idVendor" && -f "$dev/idProduct" ]] || continue
        [[ "$dev" == *:* ]] && continue

        bus_path=$(basename "$dev")
        vid=$(tr '[:upper:]' '[:lower:]' < "$dev/idVendor")
        pid=$(tr '[:upper:]' '[:lower:]' < "$dev/idProduct")
        vidpid="$vid:$pid"
        name=$(device_name_from_sysfs "$dev")

        USB_NAME_BY_BUS["$bus_path"]="$name"
        USB_VIDPID_BY_BUS["$bus_path"]="$vidpid"

        if [[ -n "${IGNORED_VIDPIDS[$vidpid]:-}" ]]; then
            continue
        fi
        if [[ -n "${CERTIFIED_TYPES[$vidpid]:-}" || -n "${CERTIFIED_LABELS[$vidpid]:-}" ]]; then
            PRESENT_BUSES["$bus_path"]="$vidpid"
        else
            UNKNOWN_USB_LINES+=("${bus_path}"$'\t'"${vidpid}"$'\t'"${name}")
        fi
    done
}

usb_missing_timeout_seconds() {
    printf '%s' "$(( MISSING_TIMEOUT * 60 ))"
}

# ── Driver blacklisting ────────────────────────────────────────────────────────
# Enumerates all certified USB dongles present in sysfs, resolves their bound
# kernel driver via interface symlinks (e.g. 1-5:1.0/driver), and writes a
# modprobe blacklist so the Proxmox host never claims the device.  Also attempts
# an immediate rmmod if the driver is already loaded (non-fatal on failure).
# Idempotent: only rewrites the conf file when the set of drivers changes.
blacklist_dongle_drivers() {
    local -A drivers_to_bl=()
    local dev bus_path vid pid vidpid iface driver_path driver_name

    for dev in /sys/bus/usb/devices/*; do
        [[ -d "$dev" ]] || continue
        [[ -f "$dev/idVendor" && -f "$dev/idProduct" ]] || continue
        [[ "$dev" == *:* ]] && continue  # skip interface entries (e.g. 1-5:1.0)

        bus_path=$(basename "$dev")
        vid=$(tr '[:upper:]' '[:lower:]' < "$dev/idVendor" 2>/dev/null) || continue
        pid=$(tr '[:upper:]' '[:lower:]' < "$dev/idProduct" 2>/dev/null) || continue
        vidpid="$vid:$pid"

        # Only act on VID:PIDs we are configured to manage as dongles.
        [[ -n "${CERTIFIED_TYPES[$vidpid]:-}" || -n "${CERTIFIED_LABELS[$vidpid]:-}" ]] || continue

        # The WiFi driver binds to a USB interface child (e.g. 1-5:1.0), not the
        # device root itself (which shows "usb" or "hub" as its driver).
        for iface in /sys/bus/usb/devices/"${bus_path}":*/driver; do
            [[ -L "$iface" ]] || continue
            driver_path=$(readlink -f "$iface" 2>/dev/null) || continue
            driver_name=$(basename "$driver_path")
            [[ -n "$driver_name" && "$driver_name" != "." ]] || continue
            drivers_to_bl["$driver_name"]=1
        done
    done

    if [[ ${#drivers_to_bl[@]} -eq 0 ]]; then
        return 0
    fi

    # Build conf content sorted for stable diffs.
    local rendered
    rendered=$(
        printf "# Auto-generated by client-sim proxmox agent — do not edit manually\n"
        printf "# Prevents host from binding to USB WiFi dongles used for VM passthrough\n"
        for drv in $(printf '%s\n' "${!drivers_to_bl[@]}" | sort); do
            printf "blacklist %s\n" "$drv"
        done
    )

    # Only write + depmod when the file actually changes (idempotent).
    local existing_conf=""
    [[ -f "$DONGLE_BLACKLIST_CONF" ]] && existing_conf=$(cat "$DONGLE_BLACKLIST_CONF" 2>/dev/null || true)
    if [[ "$rendered" != "$existing_conf" ]]; then
        printf '%s\n' "$rendered" > "$DONGLE_BLACKLIST_CONF" 2>/dev/null || {
            log "WARNING: Could not write $DONGLE_BLACKLIST_CONF (running as non-root?)"
            return 0
        }
        depmod -a 2>/dev/null || true
        log "Driver blacklist updated (${DONGLE_BLACKLIST_CONF}): $(printf '%s\n' "${!drivers_to_bl[@]}" | sort | tr '\n' ' ')"
    fi

    # Attempt to unload each driver if currently loaded; failure is non-fatal.
    for drv in "${!drivers_to_bl[@]}"; do
        if lsmod 2>/dev/null | grep -q "^${drv} "; then
            if rmmod "$drv" 2>/dev/null; then
                log "Unloaded driver: $drv"
            else
                log "WARNING: Could not unload driver $drv (may be in use) — blacklist will take effect on next boot"
            fi
        fi
    done

    # Expose to telemetry.
    BLACKLISTED_DRIVERS_JSON=$(python3 -c \
        'import json,sys; print(json.dumps(sorted(sys.argv[1:])))' \
        "${!drivers_to_bl[@]}" 2>/dev/null) || BLACKLISTED_DRIVERS_JSON="[]"
}

find_present_bus_for_vidpid() {
    local vidpid="$1" bus_path
    for bus_path in "${!PRESENT_BUSES[@]}"; do
        [[ "${PRESENT_BUSES[$bus_path]}" == "$vidpid" ]] || continue
        printf '%s' "$bus_path"
        return 0
    done
    return 1
}

guest_is_template() {
    local vmid="$1" guest_type="${2:-}" conf=""
    if [[ -z "$guest_type" ]]; then
        guest_type=$(get_guest_type "$vmid" 2>/dev/null || true)
    fi
    case "$guest_type" in
        qemu) conf="/etc/pve/qemu-server/${vmid}.conf" ;;
        lxc)  conf="/etc/pve/lxc/${vmid}.conf" ;;
        *)    return 1 ;;
    esac
    [[ -f "$conf" ]] && grep -Eq '^template:\s*1\s*$' "$conf"
}

reconcile_present_usb_state() {
    local _current_bus vmid missing_since _present_vidpid _state_vidpid _reconnected_bus _assigned_vmid
    local _changed=0

    for _current_bus in "${!STATE_BUS_TO_VMID[@]}"; do
        vmid="${STATE_BUS_TO_VMID[$_current_bus]}"
        missing_since="${STATE_MISSING_BY_BUS[$_current_bus]:-}"
        _present_vidpid="${PRESENT_BUSES[$_current_bus]:-}"
        _state_vidpid="${_present_vidpid:-${USB_VIDPID_BY_BUS[$_current_bus]:-${STATE_VIDPID_BY_BUS[$_current_bus]:-}}}"

        if [[ -n "$_state_vidpid" && "${STATE_VIDPID_BY_BUS[$_current_bus]:-}" != "$_state_vidpid" ]]; then
            STATE_VIDPID_BY_BUS["$_current_bus"]="$_state_vidpid"
            _changed=1
        fi

        if [[ -n "$_present_vidpid" ]]; then
            if [[ -n "$missing_since" ]]; then
                unset "STATE_MISSING_BY_BUS[$_current_bus]"
                _changed=1
                log "USB $_current_bus present again, clearing missing state for VM $vmid"
                # Clear any accumulated destroy-fail counter now that the dongle returned
                reset_destroy_fail_count "$vmid"
            fi
            continue
        fi

        [[ -n "$_state_vidpid" ]] || continue
        _reconnected_bus=$(find_present_bus_for_vidpid "$_state_vidpid" 2>/dev/null || true)
        [[ -n "$_reconnected_bus" && "$_reconnected_bus" != "$_current_bus" ]] || continue

        _assigned_vmid="${STATE_BUS_TO_VMID[$_reconnected_bus]:-}"
        if [[ -n "$_assigned_vmid" && "$_assigned_vmid" != "$vmid" ]]; then
            continue
        fi

        unset "STATE_BUS_TO_VMID[$_current_bus]"
        unset "STATE_MISSING_BY_BUS[$_current_bus]"
        unset "STATE_VIDPID_BY_BUS[$_current_bus]"
        STATE_VMID_TO_BUS["$vmid"]="$_reconnected_bus"
        STATE_BUS_TO_VMID["$_reconnected_bus"]="$vmid"
        STATE_VIDPID_BY_BUS["$_reconnected_bus"]="$_state_vidpid"
        unset "STATE_MISSING_BY_BUS[$_reconnected_bus]"
        _changed=1
        log "USB dongle vidpid $_state_vidpid moved from $_current_bus to $_reconnected_bus, clearing missing state for VM $vmid"
    done

    return $_changed
}

build_usb_state_json() {
    USB_STATE_LINES=()
    local vmid bus_path missing_since name vidpid prov_status _now_ts
    _now_ts=$(date +%s)
    for vmid in "${!STATE_VMID_TO_BUS[@]}"; do
        bus_path="${STATE_VMID_TO_BUS[$vmid]}"
        missing_since="${STATE_MISSING_BY_BUS[$bus_path]:-}"
        # Use live-scanned vidpid if device is present; update stored value so it
        # persists in the state file even after the dongle goes physically missing.
        if [[ -n "${USB_VIDPID_BY_BUS[$bus_path]:-}" ]]; then
            vidpid="${USB_VIDPID_BY_BUS[$bus_path]}"
            STATE_VIDPID_BY_BUS["$bus_path"]="$vidpid"
        else
            vidpid="${STATE_VIDPID_BY_BUS[$bus_path]:-}"
        fi
        name="${USB_NAME_BY_BUS[$bus_path]:-$(find_label_for_vidpid "$vidpid")}"
        # Determine provisioning status for UI display
        if [[ -f "${PROV_DIR}/${vmid}.deleting" ]]; then
            prov_status="tearing_down"
        elif [[ -f "${PROV_DIR}/${vmid}.post_prov_retry" ]]; then
            prov_status="post_prov_retry"
        elif [[ -f "${PROV_DIR}/${vmid}.agent_unresponsive" ]]; then
            local _au_rebooted_at
            IFS='|' read -r _ _ _au_rebooted_at _ < "${PROV_DIR}/${vmid}.agent_unresponsive" 2>/dev/null || _au_rebooted_at=0
            if [[ "${_au_rebooted_at:-0}" != "0" ]]; then
                prov_status="agent_rebooting"
            else
                prov_status="agent_unresponsive"
            fi
        elif [[ -f "${PROV_DIR}/${vmid}" ]]; then
            prov_status="provisioning"
        elif [[ -n "$missing_since" ]]; then
            if (( _now_ts - missing_since > MISSING_TIMEOUT * 60 )); then
                prov_status="tearing_down"
            else
                prov_status="missing"
            fi
        else
            prov_status="active"
        fi
        USB_STATE_LINES+=("${vmid}"$'\t'"${bus_path}"$'\t'"${missing_since}"$'\t'"${name}"$'\t'"${vidpid}"$'\t'"${prov_status}"$'\t'"${USB_FAIL_COUNT[$bus_path]:-0}"$'\t'"${USB_QUARANTINED[$bus_path]:-}")
    done
    if (( ${#UNKNOWN_USB_LINES[@]} )); then
        UNKNOWN_USB_JSON=$(json_from_records unknown "${UNKNOWN_USB_LINES[@]}")
    else
        UNKNOWN_USB_JSON="[]"
    fi
    if (( ${#USB_STATE_LINES[@]} )); then
        USB_STATE_JSON=$(json_from_records state "${USB_STATE_LINES[@]}")
    else
        USB_STATE_JSON="[]"
    fi
    # Build present_usb: all certified dongles physically detected right now
    local present_lines=()
    for bus_path in "${!PRESENT_BUSES[@]}"; do
        vidpid="${PRESENT_BUSES[$bus_path]}"
        name="${USB_NAME_BY_BUS[$bus_path]:-}"
        present_lines+=("${bus_path}"$'\t'"${vidpid}"$'\t'"${name}")
    done
    if (( ${#present_lines[@]} )); then
        PRESENT_USB_JSON=$(json_from_records unknown "${present_lines[@]}")
    else
        PRESENT_USB_JSON="[]"
    fi
    # Persist to cache files so the background telemetry sender can read them
    atomic_write_file "$USB_STATE_CACHE" "$USB_STATE_JSON"
    atomic_write_file "$USB_PRESENT_CACHE" "$PRESENT_USB_JSON"
    atomic_write_file "$USB_UNKNOWN_CACHE" "$UNKNOWN_USB_JSON"
    # Build and persist quarantine list: quarantined buses that have no active VM
    local _assigned_buses=()
    for _qvmid in "${!STATE_VMID_TO_BUS[@]}"; do
        _assigned_buses+=("${STATE_VMID_TO_BUS[$_qvmid]}")
    done
    local _quarantine_lines=()
    for _qbus in "${!USB_QUARANTINED[@]}"; do
        local _qbus_has_vm=0
        for _ab in "${_assigned_buses[@]}"; do
            [[ "$_ab" == "$_qbus" ]] && { _qbus_has_vm=1; break; }
        done
        if (( ! _qbus_has_vm )); then
            _quarantine_lines+=("${_qbus}"$'\t'"${USB_FAIL_COUNT[$_qbus]:-0}"$'\t'"${USB_QUARANTINED[$_qbus]}")
        fi
    done
    local USB_QUARANTINE_JSON
    if (( ${#_quarantine_lines[@]} )); then
        USB_QUARANTINE_JSON=$(printf '%s\n' "${_quarantine_lines[@]}" | python3 -c "
import json, sys
items = []
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line:
        continue
    parts = line.split('\t')
    bp = parts[0] if len(parts) > 0 else ''
    fc = parts[1] if len(parts) > 1 else '0'
    qa = parts[2] if len(parts) > 2 else ''
    items.append({'bus_path': bp, 'fail_count': int(fc) if fc.isdigit() else 0, 'quarantined_at': int(qa) if qa and qa.isdigit() else None})
print(json.dumps(items))
" 2>/dev/null || echo '[]')
    else
        USB_QUARANTINE_JSON="[]"
    fi
    atomic_write_file "$USB_QUARANTINE_CACHE" "$USB_QUARANTINE_JSON"
}

# Wait until a VM is fully stopped, with a timeout.
_wait_vm_stopped() {
    local vmid="$1" max_wait="${2:-90}"
    local elapsed=0
    while [[ $elapsed -lt $max_wait ]]; do
        local state
        state=$(timeout 10 qm status "$vmid" 2>/dev/null | awk '{print $2}')
        [[ "$state" == "stopped" ]] && return 0
        sleep 3
        elapsed=$(( elapsed + 3 ))
    done
    log "WARNING: VM $vmid did not stop within ${max_wait}s (state=$(timeout 5 qm status "$vmid" 2>/dev/null))"
    return 1
}

# Wait until a VMID no longer appears in qm list, with a timeout.
_wait_vmid_gone() {
    local vmid="$1" max_wait="${2:-90}"
    local elapsed=0
    while [[ $elapsed -lt $max_wait ]]; do
        timeout 10 qm status "$vmid" 2>/dev/null || return 0
        sleep 3
        elapsed=$(( elapsed + 3 ))
    done
    log "WARNING: VMID $vmid still exists after ${max_wait}s"
    return 1
}

get_guest_type() {
    local vmid="$1"
    # All checks use timeout — /etc/pve/ is a FUSE mount (pmxcfs) and qm/pct status
    # queries the QEMU monitor socket; both can hang indefinitely on stuck/zombie VMs.
    if timeout 5 test -f "/etc/pve/qemu-server/${vmid}.conf" 2>/dev/null; then
        printf 'qemu'; return 0
    fi
    if timeout 5 test -f "/etc/pve/lxc/${vmid}.conf" 2>/dev/null; then
        printf 'lxc'; return 0
    fi
    if timeout 10 qm status "$vmid" >/dev/null 2>&1; then
        printf 'qemu'; return 0
    fi
    if timeout 10 pct status "$vmid" >/dev/null 2>&1; then
        printf 'lxc'; return 0
    fi
    return 1
}

_wait_guest_stopped() {
    local guest_type="$1" vmid="$2" max_wait="${3:-90}"
    local elapsed=0 cmd="qm"
    [[ "$guest_type" == "lxc" ]] && cmd="pct"
    while [[ $elapsed -lt $max_wait ]]; do
        local state
        state=$(timeout 10 $cmd status "$vmid" 2>/dev/null | awk '{print $2}')
        [[ "$state" == "stopped" ]] && return 0
        sleep 3
        elapsed=$(( elapsed + 3 ))
    done
    log "WARNING: ${guest_type^^} $vmid did not stop within ${max_wait}s"
    return 1
}

_wait_guest_gone() {
    local guest_type="$1" vmid="$2" max_wait="${3:-90}"
    local elapsed=0 cmd="qm"
    [[ "$guest_type" == "lxc" ]] && cmd="pct"
    while [[ $elapsed -lt $max_wait ]]; do
        local _rc=0
        timeout 10 $cmd status "$vmid" 2>/dev/null || _rc=$?
        [[ $_rc -ne 0 ]] && return 0
        sleep 3
        elapsed=$(( elapsed + 3 ))
    done
    log "WARNING: ${guest_type^^} $vmid still exists after ${max_wait}s"
    return 1
}

_destroy_guest_only() {
    local vmid="$1" guest_type="${2:-}" force="${3:-0}"
    if [[ -z "$guest_type" ]]; then
        guest_type=$(get_guest_type "$vmid" 2>/dev/null || true)
    fi
    if [[ -z "$guest_type" ]]; then
        log "ERROR: Unable to determine guest type for VMID $vmid"
        return 1
    fi

    log "Stopping ${guest_type^^} $vmid before destroy (force=$force)"
    if [[ "$guest_type" == "lxc" ]]; then
        timeout 120 pct stop "$vmid" --force 2>/dev/null || \
            timeout 120 pct stop "$vmid" --skiplock 2>/dev/null || \
            timeout 120 pct stop "$vmid" 2>/dev/null || true
        _wait_guest_stopped "$guest_type" "$vmid" 90 || true
        log "Destroying LXC $vmid"
        timeout 300 pct destroy "$vmid" --skiplock --purge --force 2>/dev/null || \
            timeout 300 pct destroy "$vmid" --purge --force 2>/dev/null || \
            timeout 300 pct destroy "$vmid" --skiplock --purge 2>/dev/null || \
            timeout 300 pct destroy "$vmid" --skiplock 2>/dev/null || \
            timeout 300 pct destroy "$vmid" 2>/dev/null || true
    else
        if [[ "$force" == "1" ]]; then
            # Force-stop immediately (hub-initiated delete — no graceful shutdown needed).
            # Try qm stop with short timeout first.
            timeout 30 qm stop "$vmid" --skiplock --timeout 5 >>"$AGENT_LOG" 2>&1 || \
                timeout 30 qm stop "$vmid" --skiplock --forceStop 1 >>"$AGENT_LOG" 2>&1 || \
                timeout 30 qm stop "$vmid" --skiplock --timeout 1 >>"$AGENT_LOG" 2>&1 || true
            _wait_guest_stopped "$guest_type" "$vmid" 15 || {
                # qm stop didn't fully stop the VM — kill QEMU process directly.
                # Try PID file (PVE path), then /var/run fallback, then pgrep.
                local _qemu_pid=""
                _qemu_pid=$(cat "/run/qemu-server/${vmid}.pid" 2>/dev/null || \
                            cat "/var/run/qemu-server/${vmid}.pid" 2>/dev/null || true)
                if [[ -z "$_qemu_pid" ]] || ! kill -0 "$_qemu_pid" 2>/dev/null; then
                    _qemu_pid=$(pgrep -f "[[:space:]]${vmid}[[:space:]]" 2>/dev/null | head -1 || \
                                pgrep -f "qemu.*${vmid}" 2>/dev/null | head -1 || true)
                fi
                if [[ -n "$_qemu_pid" ]] && kill -0 "$_qemu_pid" 2>/dev/null; then
                    log "Force-killing QEMU PID $_qemu_pid for VM $vmid"
                    kill -9 "$_qemu_pid" 2>/dev/null || true
                    sleep 3
                else
                    # Last resort: kill via systemd unit (PVE 9+ cgroup)
                    systemctl kill --signal=SIGKILL "qemu-server@${vmid}.service" 2>/dev/null || true
                    sleep 3
                fi
            }
            _wait_guest_stopped "$guest_type" "$vmid" 20 || true
        else
            timeout 150 qm stop "$vmid" --skiplock --timeout 120 >>"$AGENT_LOG" 2>&1 || \
                timeout 30 qm stop "$vmid" --skiplock --timeout 5 >>"$AGENT_LOG" 2>&1 || true
            _wait_guest_stopped "$guest_type" "$vmid" 150 || true
        fi
        log "Destroying VM $vmid"
        # Try destroy; if it fails (e.g. VM still flagged running), kill QEMU again and retry once.
        if ! timeout 300 qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks >>"$AGENT_LOG" 2>&1; then
            log "qm destroy $vmid failed — attempting emergency QEMU kill and retry"
            local _retry_pid=""
            _retry_pid=$(cat "/run/qemu-server/${vmid}.pid" 2>/dev/null || \
                         cat "/var/run/qemu-server/${vmid}.pid" 2>/dev/null || \
                         pgrep -f "[[:space:]]${vmid}[[:space:]]" 2>/dev/null | head -1 || true)
            [[ -n "$_retry_pid" ]] && kill -9 "$_retry_pid" 2>/dev/null || true
            systemctl kill --signal=SIGKILL "qemu-server@${vmid}.service" 2>/dev/null || true
            sleep 5
            timeout 300 qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks >>"$AGENT_LOG" 2>&1 || true
        fi
    fi

    # Allow generous time for disk cleanup — large/thin images can take minutes.
    _wait_guest_gone "$guest_type" "$vmid" 360 || {
        log "ERROR: VM/CT $vmid still visible after 360s — destroy may have failed; returning failure"
        return 1
    }
    return 0
}

destroy_lxc() {
    _destroy_guest_only "$1" "lxc"
}

clone_lxc_instance() {
    local vmid="$1" source_vmid="$2"
    if [[ -z "$source_vmid" ]]; then
        log "ERROR: No LXC template/source VMID provided for CT $vmid"
        return 1
    fi

    local ct_name
    ct_name=$(pct config "$vmid" 2>/dev/null | awk -F': ' '$1=="hostname" {print $2; exit}')
    [[ -z "$ct_name" ]] && ct_name="ct-${vmid}"

    local -a reapply_args=()
    local line key value
    while IFS= read -r line; do
        case "$line" in
            onboot:*|startup:*|cores:*|memory:*|swap:*|features:*|protection:*|tags:*|description:*|nameserver:*|searchdomain:*|unprivileged:*|net[0-9]*:*)
                key="${line%%:*}"
                value="${line#*: }"
                [[ -n "$value" ]] && reapply_args+=("--${key}" "$value")
                ;;
        esac
    done < <(pct config "$vmid" 2>/dev/null || true)

    if ! _destroy_guest_only "$vmid" "lxc"; then
        log "ERROR: Failed to destroy CT $vmid before reclone"
        return 1
    fi

    if ! timeout 600 pct clone "$source_vmid" "$vmid" --hostname "$ct_name" 2>/dev/null; then
        log "ERROR: pct clone failed for CT $vmid from source $source_vmid"
        return 1
    fi

    if [[ ${#reapply_args[@]} -gt 0 ]]; then
        timeout 120 pct set "$vmid" "${reapply_args[@]}" 2>/dev/null || \
            log "WARNING: Failed to reapply one or more settings to CT $vmid"
    fi

    if ! timeout 60 pct start "$vmid" 2>/dev/null; then
        log "ERROR: pct start failed for CT $vmid"
        return 1
    fi

    log "Recloned LXC $vmid from source/template $source_vmid"
}

clone_vm_for_usb() {
    local vmid="$1" bus_path="$2" product_name="$3" image_num="${4:-1}" device_type="${5:-wireless}" save_state_on_failure="${6:-true}"
    local guest_ready=0
    local template_id="$IMAGE1_TEMPLATE_ID"
    [[ "$image_num" == "2" ]] && template_id="$IMAGE2_TEMPLATE_ID"

    local vm_name
    vm_name=$(get_vm_name "$vmid")
    # Hostname is the assigned name only — VMID is intentionally excluded.
    # Every VM gets a unique name from the name list, so no suffix is needed.
    local full_name="${vm_name}"

    # Helper: destroy this VM and free its slot so the next loop retries
    _teardown() {
        local reason="$1"
        rm -f "${PROV_DIR}/${vmid}" "${PROV_DIR}/${vmid}.post_prov_retry" \
              "${PROV_DIR}/${vmid}.provision_done" "${PROV_DIR}/${vmid}.agent_unresponsive" 2>/dev/null || true
        log "ERROR: VM $vmid provisioning failed — ${reason}. Tearing down and releasing USB $bus_path for retry."
        timeout 30 qm stop "$vmid" --skiplock 2>/dev/null || true
        _wait_vm_stopped "$vmid" 60 || true
        timeout 120 qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks 2>/dev/null || \
            timeout 120 qm destroy "$vmid" --skiplock --purge 2>/dev/null || true
        # Last resort: if qm destroy can't remove a partial/zombie config (e.g.
        # the clone was aborted before the config was fully written), delete the
        # config file directly so the VMID is not stuck as a zombie.
        if qm status "$vmid" >/dev/null 2>&1; then
            local _conf_path="/etc/pve/local/qemu-server/${vmid}.conf"
            if [[ -f "$_conf_path" ]]; then
                log "WARN: qm destroy $vmid failed — removing zombie config directly: $_conf_path"
                rm -f "$_conf_path" 2>/dev/null || true
            fi
        fi
        _wait_vmid_gone "$vmid" 60 || true
        unset "STATE_VMID_TO_BUS[$vmid]"
        unset "STATE_VMID_TO_IMAGE[$vmid]"
        unset "STATE_BUS_TO_VMID[$bus_path]"
        unset "STATE_MISSING_BY_BUS[$bus_path]"
        is_truthy "$save_state_on_failure" && save_state_file
    }

    # Mark this VMID as actively provisioning so the UI can show "Spinning up"
    echo "$(date +%s)" > "${PROV_DIR}/${vmid}"

    # Fail fast when a template is locked so the UI can report the problem and
    # offer a manual qm unlock instead of waiting/retrying in the background.
    local _lock
    _lock=$(qm config "$template_id" 2>/dev/null | awk '/^lock:/{print $2}' || true)
    if [[ -n "$_lock" ]]; then
        set_template_lock_status "template ${template_id}: ${_lock}"
        log "ERROR: template $template_id is locked ($_lock) — run qm unlock $template_id or use the UI to clear"
        _teardown "template $template_id locked ($_lock)"
        return 1
    fi
    probe_template_lock_status >/dev/null 2>&1 || true

    # Pre-clone zombie cleanup: if this VMID already exists in Proxmox (e.g. a
    # partial clone from a prior failed run that _teardown couldn't destroy),
    # force-destroy it now before cloning.  Without this, qm clone fails with
    # "VMID in use" and _teardown's stop attempt shows "unable to find config",
    # causing the VMID to be silently skipped and the next VMID used instead —
    # producing the alternating-skip pattern visible in the task log.
    if qm status "$vmid" >/dev/null 2>&1; then
        log "WARN: VMID $vmid already exists in Proxmox before clone (zombie from prior failed run) — force-destroying"
        timeout 30  qm stop    "$vmid" --skiplock --timeout 0 2>/dev/null || true
        _wait_vm_stopped "$vmid" 30 || true
        timeout 120 qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks 2>/dev/null || true
        _wait_vmid_gone  "$vmid" 30 || true
        if qm status "$vmid" >/dev/null 2>&1; then
            log "ERROR: could not destroy zombie VMID $vmid — skipping clone to avoid VMID conflict"
            _teardown "zombie VMID $vmid could not be destroyed"
            return 1
        fi
        log "Zombie VMID $vmid destroyed; proceeding with clone"
    fi

    # Clone — capture stderr so the real Proxmox error appears in the agent log
    local _clone_err
    _clone_err=$(timeout 600 qm clone "$template_id" "$vmid" --name "$full_name" 2>&1 >/dev/null)
    if [[ $? -ne 0 ]]; then
        log "ERROR: qm clone $template_id → $vmid failed: ${_clone_err:-<no output>}"
        _teardown "qm clone failed (template $template_id missing or VMID $vmid conflict)"
        return 1
    fi

    timeout 30 qm set "$vmid" --onboot 1 --startup "order=2,up=60" 2>/dev/null || true
    timeout 30 qm set "$vmid" -usb0 "host=$bus_path" 2>/dev/null || true

    # L1 VLAN NIC: check if simulation.conf has l1=yes for this VM's bucket.
    # Bucket is determined by a cksum hash of the full hostname, matching the
    # hash-based bucket assignment logic in simulation.sh.
    _check_l1_vlan() {
        local bucket_digit
        bucket_digit=$(( $(printf '%s' "$full_name" | cksum | cut -d' ' -f1) % 10 ))
        local sim_conf
        sim_conf=$(curl_api GET "/api/config" "" 2>/dev/null || true)
        [[ -z "$sim_conf" ]] && return
        local l1_val
        l1_val=$(python3 - "$sim_conf" "$bucket_digit" <<'PY' 2>/dev/null || true
import sys, configparser
text, bucket_idx = sys.argv[1], sys.argv[2]
p = configparser.ConfigParser()
p.read_string(text)
section = f"s{bucket_idx}"
print(p.get(section, "l1", fallback="no").strip().lower())
PY
)
        if [[ "$l1_val" == "yes" ]]; then
            local slot_index=$(( vmid - start_vmid ))
            local vlan_id=$(( L1_VLAN_START + slot_index ))
            if (( vlan_id >= L1_VLAN_START && vlan_id <= L1_VLAN_END )); then
                log "Attaching L1 VLAN NIC on vmbr254 tag=$vlan_id to VM $vmid"
                timeout 30 qm set "$vmid" --net1 "virtio,bridge=vmbr254,tag=${vlan_id}" 2>/dev/null || \
                    log "WARNING: Failed to set net1 VLAN for VM $vmid"
            else
                log "WARNING: Computed VLAN $vlan_id out of range [$L1_VLAN_START-$L1_VLAN_END] for VM $vmid — skipping L1 NIC"
            fi
        fi
    }
    _check_l1_vlan

    # Start
    if ! timeout 60 qm start "$vmid" 2>/dev/null; then
        _teardown "qm start failed"
        return 1
    fi

    # Wait for guest agent (up to 10 minutes, 5s intervals).
    # Each iteration: timeout 10 ping + sleep 5 = ~15s max → 40 iters ≈ 10 min.
    # Keeping this tight is critical for sequential provisioning (CONCURRENCY=1):
    # a single stuck VM previously blocked the entire batch for up to 30 minutes.
    local _ping_attempt=0
    for _ in $(seq 1 40); do
        (( _ping_attempt++ )) || true
        local _ping_out _ping_rc
        _ping_out=$(timeout 10 qm agent "$vmid" ping 2>&1); _ping_rc=$?
        log "PROVISION: qm agent $vmid ping attempt=${_ping_attempt} rc=${_ping_rc}${_ping_out:+ out=${_ping_out}}"
        if (( _ping_rc == 0 )); then
            guest_ready=1
            break
        fi
        sleep 5
    done

    if [[ "$guest_ready" -eq 0 ]]; then
        log "WARNING: Guest agent not ready after ~10 min for VM $vmid — tearing down (will retry via post-prov queue)"
        _teardown "guest agent unreachable after 10 min — tear down and retry"
        return 1
    fi

    # Set hostname — write /etc/hostname + suppress cloud-init from overriding it.
    # IMPORTANT: qm guest exec defaults to --timeout 0 (async/fire-and-forget).
    # We must pass --timeout explicitly so PVE waits for the commands to finish
    # before returning. Without this the reboot fires before the write completes.
    # Also avoid hostnamectl: it communicates via D-Bus which may not be ready
    # right after boot and can hang the entire bash script.
    local hostname_set=0
    for _ in $(seq 1 3); do
        if timeout 90 qm guest exec "$vmid" --timeout 60 -- bash -c "
            echo '${full_name}' > /etc/hostname
            sed -i 's/^127\.0\.1\.1.*/127.0.1.1\t${full_name}/' /etc/hosts 2>/dev/null || true
            mkdir -p /etc/cloud/cloud.cfg.d
            echo 'preserve_hostname: true' > /etc/cloud/cloud.cfg.d/99_preserve_hostname.cfg
            rm -f /var/lib/cloud/sem/config_set_hostname 2>/dev/null || true
        " >/dev/null 2>&1; then
            hostname_set=1
            break
        fi
        sleep 5
    done

    if [[ "$hostname_set" -eq 0 ]]; then
        _teardown "hostname set failed despite guest agent responding"
        return 1
    fi

    log "Set hostname to $full_name on VM $vmid"

    # Write USB device type for startup.sh (also synchronous)
    timeout 90 qm guest exec "$vmid" --timeout 60 -- bash -c "echo 'sim_phy=${device_type}' > /usr/local/scripts/usb-phy-override.conf" >/dev/null 2>&1 \
        && log "Wrote sim_phy=${device_type} to usb-phy-override.conf on VM $vmid" \
        || log "WARNING: Could not write usb-phy-override.conf on VM $vmid"

    timeout 30 qm guest exec "$vmid" --timeout 10 -- reboot >/dev/null 2>&1 || true

    # Wait for the VM to come back up after reboot, then run update.sh
    # so it has the latest scripts before startup.sh runs for the first time.
    # Use a deadline-based loop so the `timeout 10` ping doesn't silently extend
    # the wall-clock wait beyond the intended 5-minute cap.
    local _reboot_deadline=$(( $(date +%s) + 300 ))
    local came_back=0
    while (( $(date +%s) < _reboot_deadline )); do
        sleep 5
        local _pb_out _pb_rc
        _pb_out=$(timeout 10 qm agent "$vmid" ping 2>&1); _pb_rc=$?
        log "PROVISION: qm agent $vmid ping post-reboot rc=${_pb_rc}${_pb_out:+ out=${_pb_out}}"
        if (( _pb_rc == 0 )); then
            came_back=1
            break
        fi
    done

    if [[ "$came_back" -eq 1 ]]; then
        timeout 360 qm guest exec "$vmid" --timeout 300 -- bash /usr/local/scripts/update.sh >/dev/null 2>&1 \
            && log "update.sh completed on VM $vmid" \
            || log "WARNING: update.sh exec failed on VM $vmid — will retry on next boot"

        # Mark provisioning complete so the agent watchdog knows when to start monitoring
        echo "$(date +%s)|${bus_path}" > "${PROV_DIR}/${vmid}.provision_done"

        # Trigger hub self-update so the hub pulls latest scripts too
        if [[ -n "$SERVER_URL" ]]; then
            local _hub_http
            _hub_http=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
                -X POST "${SERVER_URL}/api/self-update" 2>/dev/null || true)
            [[ "$_hub_http" == "200" ]] \
                && log "Hub self-update triggered at ${SERVER_URL}" \
                || log "WARNING: Hub self-update returned HTTP ${_hub_http:-000} (non-fatal)"
        fi
    else
        log "WARNING: VM $vmid did not come back after reboot — skipping update.sh"
        # Queue for post-provisioning retry: check every 10 min, reclone after 1 hour
        local _retry_ts
        _retry_ts=$(date +%s)
        # Sanitise product_name — strip pipe chars used as field delimiter
        local _safe_product
        _safe_product="${product_name//|/}"
        echo "${_retry_ts}|${_retry_ts}|${bus_path}|${image_num}|${device_type}|${_safe_product}" \
            > "${PROV_DIR}/${vmid}.post_prov_retry"
        log "POST-PROV RETRY: VM $vmid queued for retry (10-min interval, reclone after 1h)"
    fi

    rm -f "${PROV_DIR}/${vmid}" 2>/dev/null || true
    log "Provisioned VM $vmid ($full_name) for USB $bus_path (${product_name}) type=${device_type}"
}

provision_vm() {
    local bus_path="$1" vidpid="$2" product_name="$3"
    local free_vmid=""
    local vmid

    for vmid in $(seq "$start_vmid" "$end_vmid"); do
        if [[ -z "${STATE_VMID_TO_BUS[$vmid]:-}" ]]; then
            free_vmid="$vmid"
            break
        fi
    done

    if [[ -z "$free_vmid" ]]; then
        log "No free slots available in VM range $start_vmid-$end_vmid"
        return 1
    fi

    # Choose image based on current distribution vs target %
    local img1_count=0 img2_count=0 total_vms image_num=1
    for vmid in "${!STATE_VMID_TO_IMAGE[@]}"; do
        [[ "${STATE_VMID_TO_IMAGE[$vmid]}" == "2" ]] && ((img2_count++)) || ((img1_count++))
    done
    total_vms=$(( img1_count + img2_count + 1 ))
    local target_img1=$(( (IMAGE1_PCT * total_vms + 99) / 100 ))  # ceiling
    [[ "$img1_count" -ge "$target_img1" ]] && image_num=2

    local device_type="${CERTIFIED_TYPES[$vidpid]:-wireless}"
    clone_vm_for_usb "$free_vmid" "$bus_path" "$product_name" "$image_num" "$device_type"
    STATE_BUS_TO_VMID["$bus_path"]="$free_vmid"
    STATE_VMID_TO_BUS["$free_vmid"]="$bus_path"
    STATE_MISSING_BY_BUS["$bus_path"]=""
    STATE_VMID_TO_IMAGE["$free_vmid"]="$image_num"
    log "Provisioned VM $free_vmid for USB $bus_path ($vidpid) type=$device_type image=$image_num (${IMAGE1_PCT}% img1 target, ${img1_count}/${total_vms} currently img1)"
}

_expire_vm_pending_commands() {
    local vmid="$1"
    local _destroy_hostname
    _destroy_hostname=$(get_full_hostname "$vmid" 2>/dev/null || true)
    if [[ -n "$_destroy_hostname" ]]; then
        curl_api DELETE "/api/commands/pending?target=${_destroy_hostname}" "" >/dev/null 2>&1 || true
    fi
}

destroy_vm() {
    local vmid="$1" guest_type="${2:-}" exclude_bus="${3:-0}" force="${4:-0}"
    local bus_path="${STATE_VMID_TO_BUS[$vmid]:-}"
    if [[ -z "$guest_type" ]]; then
        guest_type=$(get_guest_type "$vmid" 2>/dev/null || true)
    fi

    # Expire any pending client inbox commands for this VM's hostname BEFORE destroying.
    # Without this, stale commands (e.g. reboot) remain in the queue and are delivered
    # to the replacement VM when the same VMID slot is re-used, causing an immediate reboot.
    _expire_vm_pending_commands "$vmid"

    # Save bus exclusion BEFORE destroying so that even if destruction is slow or takes
    # longer than the command timeout, the auto-provision loop never recreates this VM.
    if [[ "$exclude_bus" == "1" && -n "$bus_path" ]]; then
        load_excluded_buses
        STATE_EXCLUDED_BUS["$bus_path"]="1"
        save_excluded_buses
        log "Bus $bus_path pre-excluded from auto-provisioning before hub-initiated delete of VM $vmid"
    fi

    if ! _destroy_guest_only "$vmid" "$guest_type" "$force"; then
        log "ERROR: Failed to destroy VMID $vmid"
        return 1
    fi
    if [[ -n "$bus_path" ]]; then
        unset "STATE_MISSING_BY_BUS[$bus_path]"
        unset "STATE_BUS_TO_VMID[$bus_path]"
    fi
    unset "STATE_VMID_TO_BUS[$vmid]"
    unset "STATE_VMID_TO_IMAGE[$vmid]"
    rm -f "${PROV_DIR}/${vmid}.post_prov_retry" \
          "${PROV_DIR}/${vmid}.provision_done" \
          "${PROV_DIR}/${vmid}.agent_unresponsive" 2>/dev/null || true
    save_state_file
    log "Destroyed ${guest_type^^} $vmid"
}

# Retry VMs that completed provisioning but whose post-reboot step (update.sh) was
# skipped because the guest agent timed out. Checks every 10 minutes; after 1 hour
# without the guest responding, destroys and lets the normal provision loop reclone.
_run_post_prov_retry_queue() {
    local _now _retry_file _vmid _start_ts _last_ts _bus _img _dtype _prodname
    _now=$(date +%s)
    local _state_mutated=0

    for _retry_file in "${PROV_DIR}"/*.post_prov_retry; do
        [[ -f "$_retry_file" ]] || continue

        _vmid="${_retry_file##*/}"
        _vmid="${_vmid%.post_prov_retry}"

        # Parse stored fields
        IFS='|' read -r _start_ts _last_ts _bus _img _dtype _prodname < "$_retry_file" || {
            log "POST-PROV RETRY: corrupt retry file $_retry_file — removing"
            rm -f "$_retry_file"
            continue
        }

        # Honour 10-minute minimum between retries
        if (( _now - _last_ts < 600 )); then continue; fi

        # Skip if this VM is being torn down by another path
        if [[ -f "${PROV_DIR}/${_vmid}.deleting" ]]; then
            log "POST-PROV RETRY: VM $_vmid is being deleted — removing retry entry"
            rm -f "$_retry_file"
            continue
        fi

        # Guard against VMID reuse: the VM must still map to the same bus path
        local _current_bus="${STATE_VMID_TO_BUS[$_vmid]:-}"
        if [[ "$_current_bus" != "$_bus" ]]; then
            log "POST-PROV RETRY: VM $_vmid bus mismatch (expected $_bus, got ${_current_bus:-none}) — stale entry, removing"
            rm -f "$_retry_file"
            continue
        fi

        # Verify VM still exists in Proxmox
        if ! qm status "$_vmid" >/dev/null 2>&1; then
            log "POST-PROV RETRY: VM $_vmid no longer exists — removing retry entry"
            rm -f "$_retry_file"
            continue
        fi

        local _elapsed=$(( _now - _start_ts ))
        local _rq_out _rq_rc
        _rq_out=$(timeout 10 qm agent "$_vmid" ping 2>&1); _rq_rc=$?
        log "POST-PROV RETRY: qm agent $_vmid ping rc=${_rq_rc} elapsed=${_elapsed}s${_rq_out:+ out=${_rq_out}}"

        if (( _rq_rc == 0 )); then
            log "POST-PROV RETRY: VM $_vmid guest agent responded (after ${_elapsed}s) — running update.sh"
            timeout 360 qm guest exec "$_vmid" --timeout 300 -- bash /usr/local/scripts/update.sh >/dev/null 2>&1 \
                && log "POST-PROV RETRY: update.sh completed on VM $_vmid — retry resolved" \
                || log "WARNING: POST-PROV RETRY: update.sh exec failed on VM $_vmid — VM is live, will update on next boot"
            # Write provision_done now that the VM is confirmed responsive
            echo "$(date +%s)|${_bus}" > "${PROV_DIR}/${_vmid}.provision_done"
            # Mirror hub self-update from the normal provision success path
            if [[ -n "$SERVER_URL" ]]; then
                local _hub_http
                _hub_http=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
                    -X POST "${SERVER_URL}/api/self-update" 2>/dev/null || true)
                [[ "$_hub_http" == "200" ]] \
                    && log "POST-PROV RETRY: Hub self-update triggered" \
                    || true
            fi
            rm -f "$_retry_file"
            _state_mutated=1
        else
            if (( _elapsed > 3600 )); then
                log "POST-PROV RETRY: VM $_vmid unresponsive for >1 hour — deleting; provision loop will reclone"
                rm -f "$_retry_file"
                destroy_vm "$_vmid"
                _state_mutated=1
            else
                local _remaining=$(( 3600 - _elapsed ))
                log "POST-PROV RETRY: VM $_vmid still not responding — ${_remaining}s until reclone threshold"
                echo "${_start_ts}|${_now}|${_bus}|${_img}|${_dtype}|${_prodname}" > "$_retry_file"
            fi
        fi
    done

    if (( _state_mutated )); then
        build_usb_state_json
        post_telemetry || true
    fi
}

# Monitor VM guest agents and escalate: warn → soft reboot → reclone.
# .provision_done (timestamp|bus_path) defines the grace period start.
# .agent_unresponsive (first_fail|last_check|rebooted_at) tracks escalation state.
# NOTE: the post-reboot reclone check runs even if the VM is stopped/paused
# (rubber-duck fix: do NOT gate on running state for the reclone deadline).
_run_vm_agent_watchdog() {
    [[ "${GUEST_AGENT_WATCHDOG_ENABLED:-on}" != "on" ]] && return 0

    local _now _grace_s _reboot_s _reclone_s _state_mutated=0
    _now=$(date +%s)
    _grace_s=$(( ${GUEST_AGENT_GRACE_MINUTES:-20} * 60 ))
    _reboot_s=$(( ${GUEST_AGENT_REBOOT_AFTER_MINUTES:-10} * 60 ))
    _reclone_s=$(( ${GUEST_AGENT_RECLONE_AFTER_MINUTES:-30} * 60 ))

    local vmid
    for vmid in "${!STATE_VMID_TO_BUS[@]}"; do
        # Skip VMs that are in mid-flight lifecycle states
        [[ -f "${PROV_DIR}/${vmid}" ]] && continue
        [[ -f "${PROV_DIR}/${vmid}.deleting" ]] && continue
        [[ -f "${PROV_DIR}/${vmid}.post_prov_retry" ]] && continue

        local _unresp_file="${PROV_DIR}/${vmid}.agent_unresponsive"
        local _first_fail=0 _last_check=0 _rebooted_at=0

        if [[ -f "$_unresp_file" ]]; then
            IFS='|' read -r _first_fail _last_check _rebooted_at < "$_unresp_file" 2>/dev/null || true

            # Post-reboot reclone deadline: runs REGARDLESS of VM running state
            # (VM may be stuck stopped/paused after reboot attempt)
            if [[ "${_rebooted_at:-0}" != "0" ]]; then
                local _since_reboot=$(( _now - _rebooted_at ))
                if (( _since_reboot >= _reclone_s )); then
                    log "VM AGENT WATCHDOG: VM $vmid unresponsive ${_since_reboot}s after reboot — recloning"
                    rm -f "$_unresp_file" "${PROV_DIR}/${vmid}.provision_done" 2>/dev/null || true
                    destroy_vm "$vmid"
                    _state_mutated=1
                    continue
                fi
            fi
        fi

        # Grace period: skip VMs that haven't been marked provision_done yet
        local _done_file="${PROV_DIR}/${vmid}.provision_done"
        [[ ! -f "$_done_file" ]] && continue

        local _done_ts _done_bus
        IFS='|' read -r _done_ts _done_bus < "$_done_file" 2>/dev/null || { _done_ts=0; _done_bus=""; }
        (( _done_ts == 0 )) && continue

        # VMID reuse guard: stored bus must match current assignment
        local _current_bus="${STATE_VMID_TO_BUS[$vmid]:-}"
        if [[ -n "$_done_bus" && "$_done_bus" != "$_current_bus" ]]; then
            log "VM AGENT WATCHDOG: VM $vmid bus mismatch (expected $_done_bus, got ${_current_bus:-none}) — removing stale watchdog files"
            rm -f "$_done_file" "$_unresp_file" 2>/dev/null || true
            continue
        fi

        # Still within grace period
        (( _now - _done_ts < _grace_s )) && continue

        # Only ping running VMs (stopped/paused ones are handled by the reclone path above)
        local _vm_status
        _vm_status=$(qm status "$vmid" 2>/dev/null | awk '{print $2}' || true)
        [[ "$_vm_status" != "running" ]] && continue

        # Bounded ping: 5s max so a batch of stuck VMs doesn't stall the main loop
        local _wd_out _wd_rc
        _wd_out=$(timeout 5 qm agent "$vmid" ping 2>&1); _wd_rc=$?
        log "VM AGENT WATCHDOG: qm agent $vmid ping rc=${_wd_rc}${_wd_out:+ out=${_wd_out}}"
        if (( _wd_rc == 0 )); then
            if [[ -f "$_unresp_file" ]]; then
                log "VM AGENT WATCHDOG: VM $vmid guest agent recovered — clearing unresponsive state"
                rm -f "$_unresp_file"
                _state_mutated=1
            fi
            continue
        fi

        # Agent not responding
        if [[ ! -f "$_unresp_file" ]]; then
            echo "${_now}|${_now}|0" > "$_unresp_file"
            log "VM AGENT WATCHDOG: VM $vmid agent not responding — monitoring started (reboot_after=${GUEST_AGENT_REBOOT_AFTER_MINUTES}m, reclone_after=${GUEST_AGENT_RECLONE_AFTER_MINUTES}m)"
            _state_mutated=1
            continue
        fi

        local _unresponsive_s=$(( _now - _first_fail ))
        # Update last-check timestamp
        echo "${_first_fail}|${_now}|${_rebooted_at}" > "$_unresp_file"

        if [[ "${_rebooted_at:-0}" == "0" ]] && (( _unresponsive_s >= _reboot_s )); then
            if [[ "${WATCHDOG_REBOOT_ENABLED:-on}" == "on" ]]; then
                log "VM AGENT WATCHDOG: VM $vmid unresponsive ${_unresponsive_s}s — issuing soft reboot"
                qm reboot "$vmid" --timeout 30 2>/dev/null || qm reset "$vmid" 2>/dev/null || true
                echo "${_first_fail}|${_now}|${_now}" > "$_unresp_file"
                log "VM AGENT WATCHDOG: VM $vmid soft reboot issued — will reclone if still unresponsive after ${GUEST_AGENT_RECLONE_AFTER_MINUTES}m"
            else
                log "VM AGENT WATCHDOG: VM $vmid unresponsive ${_unresponsive_s}s — auto-reboot disabled, reporting only"
            fi
            _state_mutated=1
        fi
    done

    if (( _state_mutated )); then
        build_usb_state_json
        post_telemetry || true
    fi
}

# Destroy VM via qm only — does NOT update in-memory state or write the state file.
# Used by parallel reclone jobs where state is managed by the parent process.
_destroy_vm_qm_only() {
    local vmid="$1"
    log "Stopping VM $vmid before destroy"
    # Use qm's own --timeout so Proxmox manages graceful→force shutdown internally.
    # timeout 30 + external kill was cutting the shutdown short, leaving the VM running
    # and causing qm destroy to fail silently.
    qm stop "$vmid" --skiplock --timeout 120 2>/dev/null || \
        qm stop "$vmid" --skiplock --timeout 0 2>/dev/null || true
    # Wait up to 150s (120s qm stop + 30s buffer) for the VM to reach stopped state
    _wait_vm_stopped "$vmid" 150 || true
    log "Destroying VM $vmid"
    timeout 300 qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks 2>/dev/null || true
    _wait_vmid_gone "$vmid" 90 || true
    log "VM $vmid destroyed"
}

# Run one reclone in a background subshell. All needed values are passed as arguments
# because bash associative arrays are NOT inherited by background subshells.
# State file updates are handled by the parent after all jobs complete.
_reclone_parallel_job() {
    local vmid="$1" bus_path="$2" product_name="$3" saved_image="$4" device_type="$5"
    local _vm_hostname
    _vm_hostname=$(get_full_hostname "$vmid" 2>/dev/null || true)
    # Expire stale client inbox commands before destroying so the new VM doesn't inherit them
    [[ -n "$_vm_hostname" ]] && curl_api DELETE "/api/commands/pending?target=${_vm_hostname}" "" >/dev/null 2>&1 || true
    write_reclone_state_cache "running" "[${vmid}]" "stopping"
    _destroy_vm_qm_only "$vmid"
    write_reclone_state_cache "running" "[${vmid}]" "cloning"
    if clone_vm_for_usb "$vmid" "$bus_path" "$product_name" "$saved_image" "$device_type"; then
        log "Parallel reclone done: VM $vmid bus=$bus_path type=$device_type image=$saved_image"
    else
        return 1
    fi
}

reclone_vm_instance() {
    local vmid="$1"
    local bus_path vidpid product_name

    refresh_usb_config
    load_state_file
    scan_usb_devices

    bus_path="${STATE_VMID_TO_BUS[$vmid]:-}"
    if [[ -z "$bus_path" ]]; then
        # Fallback: recover bus_path from qm config (handles VMs created outside the agent)
        local usb_line
        usb_line=$(qm config "$vmid" 2>/dev/null | grep -m1 '^usb[0-9]*: ')
        if [[ "$usb_line" =~ host=([^,[:space:]]+) ]]; then
            bus_path="${BASH_REMATCH[1]}"
            log "Recovered USB bus_path=$bus_path for VM $vmid from qm config"
            STATE_VMID_TO_BUS[$vmid]="$bus_path"
            STATE_BUS_TO_VMID[$bus_path]="$vmid"
            save_state_file
        else
            log "WARNING: No USB mapping found for VM $vmid (state file and qm config both empty)"
            return 1
        fi
    fi
    if [[ ! -d "/sys/bus/usb/devices/$bus_path" ]]; then
        log "WARNING: USB device $bus_path is not present; cannot reclone VM $vmid"
        return 1
    fi

    vidpid="${USB_VIDPID_BY_BUS[$bus_path]:-}"
    local stored_vidpid="${STATE_VIDPID_BY_BUS[$bus_path]:-$vidpid}"
    product_name="${USB_NAME_BY_BUS[$bus_path]:-$(find_label_for_vidpid "$vidpid")}"
    # sim_phy is always derived from the certified USB device table so the correct
    # wired/wireless type is applied regardless of what simulation.conf says globally.
    local device_type="${CERTIFIED_TYPES[$vidpid]:-wireless}"
    local stored_device_type="${CERTIFIED_TYPES[$stored_vidpid]:-wireless}"
    # Guard: skip reclone when the attached dongle's current type no longer matches
    # the VM's stored assignment, unless overflow mode is explicitly enabled.
    if [[ "$stored_device_type" != "$device_type" ]] && ! is_truthy "$USE_ALL_DONGLES"; then
        log "WARNING: VM $vmid USB $bus_path current type=$device_type does not match stored type=$stored_device_type — skipping reclone"
        return 1
    fi
    if [[ "$SIM_PHY" != "any" ]] && ! is_truthy "$USE_ALL_DONGLES" && ! sim_phy_accepts_type "$stored_device_type"; then
        log "WARNING: VM $vmid USB $bus_path stored type=$stored_device_type is not allowed by sim_phy=$SIM_PHY use_all_dongles=$USE_ALL_DONGLES — skipping reclone"
        return 1
    fi
    # Save image number BEFORE destroy_vm — destroy_vm unsets STATE_VMID_TO_IMAGE[$vmid].
    local saved_image="${STATE_VMID_TO_IMAGE[$vmid]:-1}"

    # Expire any pending client inbox commands for this VM's hostname BEFORE destroying
    # it. Without this, commands (e.g. reboot) that the old VM never ACK'd remain as
    # "pending" and will be delivered to the replacement VM with the same hostname,
    # causing it to reboot immediately after calling home.
    local _client_hostname
    _client_hostname=$(get_full_hostname "$vmid" 2>/dev/null || true)
    [[ -n "$_client_hostname" ]] && curl_api DELETE "/api/commands/pending?target=${_client_hostname}" "" >/dev/null 2>&1 || true
    log "Expired pending client commands for ${_client_hostname:-vmid $vmid} before reclone"

    destroy_vm "$vmid"
    clone_vm_for_usb "$vmid" "$bus_path" "$product_name" "$saved_image" "$device_type"
    STATE_BUS_TO_VMID["$bus_path"]="$vmid"
    STATE_VMID_TO_BUS["$vmid"]="$bus_path"
    STATE_MISSING_BY_BUS["$bus_path"]=""
    save_state_file
    log "Recloned VM $vmid for USB $bus_path ($vidpid) type=$device_type image=$saved_image"
}

# ── Continuous resource gate check ──────────────────────────────────────────
# Called every telemetry cycle to keep PROVISION_HALT_CACHE current regardless
# of whether a provisioning event is in progress. Without this, the cache is
# only written during active provision attempts, so the UI shows no throttle
# indicator when "Idle" even though CPU/memory are above threshold.
check_resource_halt() {
    [[ "$AUTO_PROVISION" != "on" ]] && return 0
    local _cpu _s1 _s2 _mem_total _mem_avail _mem_pct
    _s1=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    sleep 1
    _s2=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    _cpu=$(awk -v s1="$_s1" -v s2="$_s2" 'BEGIN {
        n = split(s1, a); split(s2, b)
        t1 = 0; t2 = 0
        for (i = 2; i <= n; i++) { t1 += a[i]; t2 += b[i] }
        dt = t2 - t1; di = b[5] - a[5]
        printf "%.0f\n", (dt > 0) ? (1 - di/dt) * 100 : 0
    }' 2>/dev/null) || _cpu=0
    _mem_total=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
    _mem_avail=$(grep MemAvailable /proc/meminfo 2>/dev/null | awk '{print $2}')
    if [[ -n "$_mem_total" && "$_mem_total" -gt 0 ]]; then
        _mem_pct=$(awk -v t="$_mem_total" -v a="$_mem_avail" \
            'BEGIN { printf "%.0f\n", (1 - a/t) * 100 }' 2>/dev/null) || _mem_pct=0
    else
        _mem_pct=0
    fi
    if (( _cpu >= CPU_PROVISION_THRESHOLD )); then
        printf '{"halted":true,"reason":"cpu","cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
            "$_cpu" "$CPU_PROVISION_THRESHOLD" "$_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
            > "$PROVISION_HALT_CACHE"
        return 0
    fi
    if (( _mem_pct >= MEM_PROVISION_THRESHOLD )); then
        printf '{"halted":true,"reason":"mem","cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
            "$_cpu" "$CPU_PROVISION_THRESHOLD" "$_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
            > "$PROVISION_HALT_CACHE"
        return 0
    fi
    printf '{"halted":false,"reason":null,"cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
        "$_cpu" "$CPU_PROVISION_THRESHOLD" "$_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
        > "$PROVISION_HALT_CACHE"
}

_usb_provision_loop_impl() {
    local now bus_path vidpid product_name vmid missing_since
    local timeout_seconds missing_age _current_bus _state_vidpid _guest_type

    refresh_usb_config
    scan_usb_devices
    load_state_file
    load_excluded_buses
    load_usb_quarantine
    # Once a dongle is physically removed, its exclusion is cleared so that
    # re-plugging the dongle provisions a fresh VM as expected.
    local _excl_changed=0
    for _excl_bus in "${!STATE_EXCLUDED_BUS[@]}"; do
        if [[ -z "${PRESENT_BUSES[$_excl_bus]:-}" ]]; then
            unset "STATE_EXCLUDED_BUS[$_excl_bus]"
            _excl_changed=1
            log "Bus $_excl_bus no longer present — cleared exclusion; will reprovision when re-plugged"
        fi
    done
    (( _excl_changed )) && save_excluded_buses

    # ── Stale state cleanup: remove entries for VMIDs that no longer exist ────
    # Prevents dongles from being "stuck" assigned to a manually-deleted VM.
    # Use a timeout so a hung qm/pct command doesn't stall the entire provision loop.
    local -A _existing_vmids=()
    local -A _reconnected_vidpids=()
    local _qm_list_raw
    _qm_list_raw=$(timeout 60 bash -c '{ qm list 2>/dev/null || true; pct list 2>/dev/null || true; }' 2>/dev/null)
    local _qm_exit=$?
    if [[ $_qm_exit -eq 124 ]]; then
        log "WARNING: qm/pct list timed out (Proxmox may have a stuck task); skipping provision cycle"
        printf '{"halted":false,"reason":"qm_busy","warning":"qm list timed out","ts":%s}\n' "$(date +%s)" \
            > "$PROVISION_HALT_CACHE"
        return 0
    fi
    while IFS= read -r _vid; do
        [[ -n "$_vid" ]] && _existing_vmids["$_vid"]="1"
    done < <(printf '%s\n' "$_qm_list_raw" | awk '$1 ~ /^[0-9]+$/ { print $1 }')
    local _state_changed=0
    for vmid in "${!STATE_VMID_TO_BUS[@]}"; do
        if [[ -z "${_existing_vmids[$vmid]:-}" ]]; then
            local _stale_bus="${STATE_VMID_TO_BUS[$vmid]:-}"
            log "State cleanup: VM $vmid no longer exists — releasing bus ${_stale_bus:-unknown} for re-provision"
            [[ -n "$_stale_bus" ]] && {
                unset "STATE_BUS_TO_VMID[$_stale_bus]"
                unset "STATE_MISSING_BY_BUS[$_stale_bus]"
                unset "STATE_VIDPID_BY_BUS[$_stale_bus]"
            }
            unset "STATE_VMID_TO_BUS[$vmid]"
            unset "STATE_VMID_TO_IMAGE[$vmid]"
            _state_changed=1
        fi
    done

    now=$(date +%s)
    timeout_seconds=$(usb_missing_timeout_seconds)
    if ! reconcile_present_usb_state; then
        _state_changed=1
    fi
    for _current_bus in "${!STATE_BUS_TO_VMID[@]}"; do
        vmid="${STATE_BUS_TO_VMID[$_current_bus]}"
        missing_since="${STATE_MISSING_BY_BUS[$_current_bus]:-}"
        _state_vidpid="${USB_VIDPID_BY_BUS[$_current_bus]:-${STATE_VIDPID_BY_BUS[$_current_bus]:-}}"
        [[ -n "$_state_vidpid" ]] && STATE_VIDPID_BY_BUS["$_current_bus"]="$_state_vidpid"

        if [[ -n "${PRESENT_BUSES[$_current_bus]:-}" ]]; then
            continue
        fi

        if [[ -z "$missing_since" ]]; then
            STATE_MISSING_BY_BUS["$_current_bus"]="$now"
            _state_changed=1
            log "USB $_current_bus missing for VM $vmid; grace timer started"
        else
            missing_age=$(( now - missing_since ))
            if (( missing_age > timeout_seconds )); then
                _guest_type=$(get_guest_type "$vmid" 2>/dev/null || true)
                if guest_is_template "$vmid" "$_guest_type"; then
                    log "USB dongle missing for ${missing_age}s but VM $vmid is a template; skipping teardown"
                    continue
                fi
                log "USB dongle missing for ${missing_age}s — tearing down VM $vmid"
                [[ -n "$_state_vidpid" && -n "$(find_present_bus_for_vidpid "$_state_vidpid" 2>/dev/null || true)" ]] && _reconnected_vidpids["$_state_vidpid"]=1
                if destroy_vm "$vmid" "$_guest_type"; then
                    reset_destroy_fail_count "$vmid"
                    record_usb_failure "$_current_bus"
                    _state_changed=1
                else
                    # destroy_vm returned non-zero: timed out or failed.  Track consecutive
                    # failures; after DESTROY_MAX_FAILS give up and force-release the bus so
                    # a fresh VM can be provisioned on this dongle.
                    increment_destroy_fail_count "$vmid" "$_current_bus"
                fi
            fi
        fi
    done

    # ── Resource gate: skip provisioning if CPU or memory is above threshold ──
    local _rsrc_cpu _rsrc_mem_pct _s1 _s2 _mem_total _mem_avail
    _s1=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    sleep 1
    _s2=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    _rsrc_cpu=$(awk -v s1="$_s1" -v s2="$_s2" 'BEGIN {
        n = split(s1, a); split(s2, b)
        t1 = 0; t2 = 0
        for (i = 2; i <= n; i++) { t1 += a[i]; t2 += b[i] }
        dt = t2 - t1; di = b[5] - a[5]
        val = (dt > 0) ? (1 - di/dt) * 100 : 0
        printf "%.0f\n", val
    }' 2>/dev/null) || _rsrc_cpu=0
    _mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    _mem_avail=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
    if [[ -n "$_mem_total" && "$_mem_total" -gt 0 ]]; then
        _rsrc_mem_pct=$(awk -v t="$_mem_total" -v a="$_mem_avail" \
            'BEGIN { printf "%.0f\n", (1 - a/t) * 100 }' 2>/dev/null) || _rsrc_mem_pct=0
    else
        _rsrc_mem_pct=0
    fi
    if (( _rsrc_cpu >= CPU_PROVISION_THRESHOLD )); then
        printf '{"halted":true,"reason":"cpu","cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
            "$_rsrc_cpu" "$CPU_PROVISION_THRESHOLD" "$_rsrc_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
            > "$PROVISION_HALT_CACHE"
        log "Auto-provision paused: CPU ${_rsrc_cpu}% >= threshold ${CPU_PROVISION_THRESHOLD}%"
        build_usb_state_json
        return 0
    fi
    if (( _rsrc_mem_pct >= MEM_PROVISION_THRESHOLD )); then
        printf '{"halted":true,"reason":"mem","cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
            "$_rsrc_cpu" "$CPU_PROVISION_THRESHOLD" "$_rsrc_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
            > "$PROVISION_HALT_CACHE"
        log "Auto-provision paused: memory ${_rsrc_mem_pct}% >= threshold ${MEM_PROVISION_THRESHOLD}%"
        build_usb_state_json
        return 0
    fi
    printf '{"halted":false,"reason":null,"cpu_pct":%s,"cpu_threshold":%s,"mem_pct":%s,"mem_threshold":%s,"ts":%s}\n' \
        "$_rsrc_cpu" "$CPU_PROVISION_THRESHOLD" "$_rsrc_mem_pct" "$MEM_PROVISION_THRESHOLD" "$(date +%s)" \
        > "$PROVISION_HALT_CACHE"

    # ── Parallel provision: new USB dongles not yet assigned a VM ─────────────
    # Pre-assign VMIDs in the parent before forking so parallel subshells
    # cannot race and pick the same slot. Associative arrays (STATE_*, CERTIFIED_TYPES)
    # are NOT inherited by background subshells — capture all needed values here.
    local -a _prov_buses=() _prov_vmids=() _prov_products=() _prov_images=() _prov_types=()
    local -a _preferred_buses=() _overflow_buses=() _ordered_buses=()
    local _next_free_vmid="$start_vmid"
    local _img1_count=0 _img2_count=0 _preferred_available=0 _overflow_available=0

    for vmid in "${!STATE_VMID_TO_IMAGE[@]}"; do
        [[ "${STATE_VMID_TO_IMAGE[$vmid]}" == "2" ]] && ((_img2_count++)) || ((_img1_count++))
    done

    for bus_path in "${!PRESENT_BUSES[@]}"; do
        [[ -n "${STATE_BUS_TO_VMID[$bus_path]:-}" ]] && continue
        [[ -n "${STATE_EXCLUDED_BUS[$bus_path]:-}" ]] && { log "Bus $bus_path is excluded from provisioning (hub-deleted); skipping"; continue; }
        [[ -n "${USB_QUARANTINED[$bus_path]:-}" ]] && { log "Bus $bus_path is quarantined (${USB_FAIL_COUNT[$bus_path]:-0} failures); skipping provisioning"; continue; }
        vidpid="${PRESENT_BUSES[$bus_path]}"
        local _dtype="${CERTIFIED_TYPES[$vidpid]:-wireless}"
        if [[ "$SIM_PHY" == "any" || "$_dtype" == "$SIM_PHY" ]]; then
            _preferred_buses+=("$bus_path")
            [[ "$SIM_PHY" != "any" ]] && ((_preferred_available++))
            continue
        fi
        if is_truthy "$USE_ALL_DONGLES" && [[ "$SIM_PHY" == "wireless" || "$SIM_PHY" == "ethernet" ]]; then
            _overflow_buses+=("$bus_path")
            ((_overflow_available++))
            continue
        fi
        log "Skipping USB $bus_path ($vidpid) — type=$_dtype, sim_phy=$SIM_PHY"
    done

    _ordered_buses=("${_preferred_buses[@]}")
    if [[ ${#_overflow_buses[@]} -gt 0 ]]; then
        _ordered_buses+=("${_overflow_buses[@]}")
        log "use_all_dongles enabled — provisioning ${_preferred_available} preferred $SIM_PHY dongles first, then ${_overflow_available} overflow dongles"
    fi

    for bus_path in "${_ordered_buses[@]}"; do
        vidpid="${PRESENT_BUSES[$bus_path]}"
        local _dtype="${CERTIFIED_TYPES[$vidpid]:-wireless}"
        while (( _next_free_vmid <= end_vmid )); do
            [[ -z "${STATE_VMID_TO_BUS[$_next_free_vmid]:-}" && -z "${_existing_vmids[$_next_free_vmid]:-}" ]] && break
            ((_next_free_vmid++))
        done
        if (( _next_free_vmid > end_vmid )); then
            log "No free VM slots available — stopping provisioning"
            break
        fi
        local _free="$_next_free_vmid"
        ((_next_free_vmid++))

        product_name="${USB_NAME_BY_BUS[$bus_path]:-$(find_label_for_vidpid "$vidpid")}"

        local _total_vms=$(( _img1_count + _img2_count + 1 ))
        local _target_img1=$(( (IMAGE1_PCT * _total_vms + 99) / 100 ))
        local _img_num=1
        [[ "$_img1_count" -ge "$_target_img1" ]] && _img_num=2
        [[ "$_img_num" == "1" ]] && ((_img1_count++)) || ((_img2_count++))

        STATE_VMID_TO_BUS["$_free"]="$bus_path"
        STATE_BUS_TO_VMID["$bus_path"]="$_free"
        STATE_VMID_TO_IMAGE["$_free"]="$_img_num"

        _prov_buses+=("$bus_path")
        _prov_vmids+=("$_free")
        _prov_products+=("$product_name")
        _prov_images+=("$_img_num")
        _prov_types+=("$_dtype")

        if [[ -n "${_reconnected_vidpids[$vidpid]:-}" ]]; then
            log "USB dongle vidpid $vidpid reconnected — auto-provisioning new VM"
        elif [[ "$SIM_PHY" != "any" && "$_dtype" != "$SIM_PHY" ]]; then
            log "Provisioning overflow USB $bus_path ($vidpid) — type=$_dtype, preferred=$SIM_PHY"
        fi
    done

    if [[ ${#_prov_buses[@]} -gt 0 ]]; then
        local _active_pids=() _all_pids=()
        local _slot_deadline=0 _last_cfg_refresh=0
        for _i in "${!_prov_buses[@]}"; do
            # Wait for a concurrency slot, but enforce a D-state timeout so a hung
            # qm clone descendant cannot hold the provision flock indefinitely.
            _slot_deadline=0
            while [[ ${#_active_pids[@]} -ge ${RECLONE_CONCURRENCY:-1} ]]; do
                local _live_pids=()
                for _p in "${_active_pids[@]}"; do
                    kill -0 "$_p" 2>/dev/null && _live_pids+=("$_p")
                done
                _active_pids=("${_live_pids[@]}")
                if [[ ${#_active_pids[@]} -ge ${RECLONE_CONCURRENCY:-1} ]]; then
                    local _now_slot=$(date +%s)
                    if (( _slot_deadline == 0 )); then
                        _slot_deadline=$(( _now_slot + ${DSTATE_TIMEOUT_SECONDS:-120} ))
                    fi
                    if (( _now_slot >= _slot_deadline )); then
                        log "WARNING: Clone slot occupied for >${DSTATE_TIMEOUT_SECONDS:-120}s (likely D-state descendant) — killing ${#_active_pids[@]} stuck clone(s)"
                        for _p in "${_active_pids[@]}"; do
                            kill -KILL "$_p" 2>/dev/null || true
                        done
                        _active_pids=()
                        break
                    fi
                    # Refresh config every 30s while waiting so a change to
                    # RECLONE_CONCURRENCY takes effect mid-cycle.
                    if (( _now_slot - _last_cfg_refresh >= 30 )); then
                        refresh_usb_config 2>/dev/null || true
                        _last_cfg_refresh=$_now_slot
                    fi
                    sleep 3
                fi
            done
            # Create sentinel in the PARENT right before forking — this means only
            # RECLONE_CONCURRENCY sentinels exist at once, so the UI shows exactly
            # as many "provisioning" VMs as are actively cloning (not the full queue).
            echo "$(date +%s)" > "${PROV_DIR}/${_prov_vmids[$_i]}"
            build_usb_state_json
            post_telemetry || true
            # Stagger clone starts and re-check CPU between each clone (ramp-up pacing).
            # The 1-second measurement slot replaces one second of the 15-second stagger.
            if (( _i > 0 )); then
                sleep 14
                local _pace_s1 _pace_s2 _pace_cpu
                _pace_s1=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
                sleep 1
                _pace_s2=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
                _pace_cpu=$(awk -v s1="$_pace_s1" -v s2="$_pace_s2" 'BEGIN {
                    n = split(s1, a); split(s2, b)
                    t1 = 0; t2 = 0
                    for (i = 2; i <= n; i++) { t1 += a[i]; t2 += b[i] }
                    dt = t2 - t1; di = b[5] - a[5]
                    printf "%.0f\n", (dt > 0) ? (1 - di/dt) * 100 : 0
                }' 2>/dev/null) || _pace_cpu=0
                if (( _pace_cpu >= CPU_RAMP_CEILING )); then
                    log "Auto-provision pacing: CPU ${_pace_cpu}% >= ceiling ${CPU_RAMP_CEILING}% — stopping batch after ${_i} clone(s)"
                    printf '{"halted":true,"reason":"pacing","cpu_pct":%s,"cpu_threshold":%s,"mem_pct":0,"mem_threshold":0,"ts":%s}\n' \
                        "$_pace_cpu" "$CPU_RAMP_CEILING" "$(date +%s)" > "$PROVISION_HALT_CACHE"
                    # Remove the sentinel just created for this VMID and revert state
                    # for this and all remaining unstarted clones so they are re-evaluated
                    # on the next provision cycle once CPU drops below the threshold.
                    for _j in "${!_prov_buses[@]}"; do
                        (( _j < _i )) && continue
                        rm -f "${PROV_DIR}/${_prov_vmids[$_j]}" 2>/dev/null || true
                        unset "STATE_VMID_TO_BUS[${_prov_vmids[$_j]}]"  2>/dev/null || true
                        unset "STATE_VMID_TO_IMAGE[${_prov_vmids[$_j]}]" 2>/dev/null || true
                        unset "STATE_BUS_TO_VMID[${_prov_buses[$_j]}]"   2>/dev/null || true
                    done
                    build_usb_state_json
                    post_telemetry || true
                    break
                fi
            fi
            (
                if clone_vm_for_usb "${_prov_vmids[$_i]}" "${_prov_buses[$_i]}" \
                    "${_prov_products[$_i]}" "${_prov_images[$_i]}" "${_prov_types[$_i]}" false; then
                    log "Provisioned VM ${_prov_vmids[$_i]} for USB ${_prov_buses[$_i]} type=${_prov_types[$_i]} image=${_prov_images[$_i]} (parallel)"
                else
                    exit 1
                fi
            ) &
            _pid=$!
            _active_pids+=("$_pid")
            _all_pids+=("$_pid")
        done
        local _prov_ok=0 _prov_fail=0
        # Wait for all clone jobs concurrently with two deadlines:
        #   _clone_deadline  — hard per-batch ceiling (CLONE_TIMEOUT_SECONDS, default 30 min)
        #   _stuck_deadline  — early exit when any PID has been alive too long, which
        #                      indicates a descendant stuck in D-state (uninterruptible
        #                      kernel sleep).  The subshell (_cpid) itself stays in S-state
        #                      while waiting for the D-state child and is therefore killable,
        #                      so we kill all surviving PIDs together instead of waiting
        #                      DSTATE_TIMEOUT_SECONDS per PID sequentially.
        local _clone_deadline=$(( $(date +%s) + ${CLONE_TIMEOUT_SECONDS:-1800} ))
        local _stuck_deadline=$(( $(date +%s) + ${DSTATE_TIMEOUT_SECONDS:-120} ))
        # Single polling loop monitoring ALL PIDs at once
        local _live_pids=("${_all_pids[@]}")
        while (( ${#_live_pids[@]} > 0 )); do
            local _now=$(date +%s)
            # Check which PIDs are still alive
            local _still_live=()
            for _p in "${_live_pids[@]}"; do
                kill -0 "$_p" 2>/dev/null && _still_live+=("$_p")
            done
            _live_pids=("${_still_live[@]}")
            (( ${#_live_pids[@]} == 0 )) && break

            if (( _now >= _clone_deadline )); then
                log "WARNING: Batch clone deadline reached — killing ${#_live_pids[@]} remaining clone(s)"
                for _p in "${_live_pids[@]}"; do
                    kill -TERM "$_p" 2>/dev/null || true
                done
                sleep 3
                for _p in "${_live_pids[@]}"; do
                    kill -KILL "$_p" 2>/dev/null || true
                done
                sleep 2
                break
            fi

            if (( _now >= _stuck_deadline )); then
                log "WARNING: ${#_live_pids[@]} clone(s) still alive after ${DSTATE_TIMEOUT_SECONDS:-120}s — likely D-state descendant(s); killing subshells"
                for _p in "${_live_pids[@]}"; do
                    kill -KILL "$_p" 2>/dev/null || true
                done
                sleep 2
                break
            fi
            sleep 5
        done

        # Collect results for every spawned PID.
        # Guard: if a PID survived SIGKILL it is itself in D-state — skip wait().
        for _i in "${!_all_pids[@]}"; do
            local _cpid="${_all_pids[$_i]}"
            if kill -0 "$_cpid" 2>/dev/null; then
                log "WARNING: VM ${_prov_vmids[$_i]} clone PID $_cpid survived SIGKILL (D-state) — skipping wait, treating as failure"
                unset "STATE_VMID_TO_BUS[${_prov_vmids[$_i]}]"
                unset "STATE_VMID_TO_IMAGE[${_prov_vmids[$_i]}]"
                unset "STATE_BUS_TO_VMID[${_prov_buses[$_i]}]"
                unset "STATE_MISSING_BY_BUS[${_prov_buses[$_i]}]"
                (( _prov_fail++ )) || true
            elif ! wait "$_cpid" 2>/dev/null; then
                log "WARNING: A parallel provision job failed for VM ${_prov_vmids[$_i]}"
                unset "STATE_VMID_TO_BUS[${_prov_vmids[$_i]}]"
                unset "STATE_VMID_TO_IMAGE[${_prov_vmids[$_i]}]"
                unset "STATE_BUS_TO_VMID[${_prov_buses[$_i]}]"
                unset "STATE_MISSING_BY_BUS[${_prov_buses[$_i]}]"
                (( _prov_fail++ )) || true
            else
                (( _prov_ok++ )) || true
            fi
        done
        _state_changed=1
        save_state_file
        build_usb_state_json
        post_telemetry || true
        # Signal caller to apply backoff when every spawned job failed.
        if (( _prov_fail > 0 && _prov_ok == 0 )); then
            return 2
        fi
    fi

    [[ "$_state_changed" -eq 1 ]] && save_state_file
    build_usb_state_json
}

usb_provision_loop() {
    if command -v flock >/dev/null 2>&1; then
        (
            if ! flock -n 200; then
                log "Provision loop already running in another process — skipping duplicate trigger"
                exit 0
            fi
            _usb_provision_loop_impl
        ) 200>"$USB_PROVISION_LOCK_FILE"
        return $?
    fi

    _usb_provision_loop_impl
}

refresh_usb_telemetry_only() {
    refresh_usb_config
    scan_usb_devices
    load_state_file
    if ! reconcile_present_usb_state; then
        save_state_file
    fi
    build_usb_state_json
}

collect_log_lines() {
    # Read new log lines since last send, return as JSON array of strings.
    # Uses tail -c +N for fast byte-offset seeking (avoids slow dd bs=1).
    # flock ensures the WS subshell and main-loop subshell never race on the
    # same offset — only one reader advances the pointer at a time.
    [[ -f "$AGENT_LOG" ]] || { echo "[]"; return; }

    local _lock_fd=9 _lock_file="${AGENT_LOG_OFFSET_FILE}.lock"
    (
        flock -x "$_lock_fd"

        local offset=0 current_size new_content
        [[ -f "$AGENT_LOG_OFFSET_FILE" ]] && offset=$(<"$AGENT_LOG_OFFSET_FILE" 2>/dev/null || echo 0)
        current_size=$(wc -c < "$AGENT_LOG" 2>/dev/null || echo 0)

        # If log was rotated (shrunk), reset offset
        (( current_size < offset )) && offset=0

        if (( current_size <= offset )); then echo "[]"; return; fi

        # On first call (offset=0) only send last 100 lines to avoid flooding
        if (( offset == 0 )); then
            new_content=$(tail -n 100 "$AGENT_LOG" 2>/dev/null || true)
        else
            # tail -c +N starts at byte N (1-based)
            new_content=$(tail -c +$(( offset + 1 )) "$AGENT_LOG" 2>/dev/null || true)
        fi

        echo "$current_size" > "$AGENT_LOG_OFFSET_FILE"

        [[ -z "$new_content" ]] && { echo "[]"; return; }

        echo "$new_content" | python3 -c "
import sys, json
lines = [l.rstrip() for l in sys.stdin if l.strip()]
print(json.dumps(lines[-200:]))
" 2>/dev/null || echo "[]"
    ) 9>"$_lock_file"
}


# Find the vhclient binary in common install locations.
_find_vhclient() {
    local found
    while IFS= read -r found; do
        [[ -x "$found" ]] && echo "$found" && return 0
    done < <(find /root/.local /opt /home /usr/local/lib /usr/lib -maxdepth 6 -name 'vhclient*' -type f 2>/dev/null)
    local c
    for c in /usr/sbin/vhclient /usr/bin/vhclient /usr/local/bin/vhclient /opt/virtualhere/vhclient; do
        [[ -x "$c" ]] && echo "$c" && return 0
    done
    return 1
}

# ── T3 PCI device collection ───────────────────────────────────────────────────
# Scans the host PCI bus for devices that qualify as "T3" IoT adapters.
# Currently targets VID:PID 168c:0034 (Qualcomm Atheros AR9462 802.11ac adapter).
# Returns a JSON array of matching devices: [{id, vidpid, name}, ...].
# An empty array means no T3 devices are present on this Proxmox node.
collect_t3_pci_devices() {
    # Target VID:PID for T3 classification — Qualcomm Atheros 802.11ac wireless adapter.
    # This constant will expand to a configurable list in a future release.
    local T3_VIDPID="168c:0034"

    # lspci must be available; if not, report no devices (graceful degradation).
    if ! command -v lspci &>/dev/null; then
        echo "[]"
        return
    fi

    # Use Python for reliable JSON construction and string escaping.
    # lspci -n lists all PCI devices with numeric vendor:device IDs:
    #   0000:01:00.0 0280: 168c:0034 (rev 01)
    # lspci (without -n) gives the human-readable name for the same address.
    python3 - "$T3_VIDPID" <<'PY'
import subprocess, re, json, sys

t3_vidpid = sys.argv[1] if len(sys.argv) > 1 else "168c:0034"

try:
    # -n: show numeric vendor:device IDs so we can do exact matching
    raw = subprocess.check_output(["lspci", "-n"], text=True, timeout=10).splitlines()
except Exception:
    raw = []

devices = []
for line in raw:
    # Parse: "0000:01:00.0 0280: 168c:0034 (rev 01)"
    m = re.match(r'^(\S+)\s+\S+:\s+(\S+)', line)
    if not m:
        continue
    addr = m.group(1)
    # Strip any trailing revision/subdevice info from the VID:DID field
    vidpid = m.group(2).lower().split()[0]
    if vidpid != t3_vidpid.lower():
        continue
    # Fetch human-readable name for this specific address
    try:
        name_line = subprocess.check_output(["lspci", "-s", addr], text=True, timeout=5).strip()
        # Remove the address prefix: "0000:01:00.0 Network controller: Qualcomm..."
        name = re.sub(r'^\S+\s+', '', name_line, count=1)
    except Exception:
        name = ""
    devices.append({"id": addr, "vidpid": vidpid, "name": name})

print(json.dumps(devices))
PY
}

collect_vh_devices() {
    local vhbin
    vhbin=$(_find_vhclient 2>/dev/null) || vhbin=""

    python3 - "$vhbin" <<'PY'
import subprocess, re, json, socket, sys

vhbin = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else ""

# ── Service / process status ───────────────────────────────────────────────
# vhclient may not be a systemd service — check systemctl first, then pgrep.
svc_active = False
for svc_name in ("virtualhereclient", "vhclient", "vhclientd", "virtualhere"):
    try:
        r = subprocess.run(["systemctl", "is-active", svc_name],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip() in ("active", "activating"):
            svc_active = True
            break
    except Exception:
        pass

if not svc_active:
    # Fall back to pgrep — handles "not running as a service" case
    for proc in ("vhclient", "vhclientx86_64", "vhclientarm"):
        try:
            r = subprocess.run(["pgrep", "-x", proc],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                svc_active = True
                break
        except Exception:
            pass

# ── Fetch device list: TCP API first, binary fallback ──────────────────────
vh_out = ""
vh_ok  = False

try:
    with socket.create_connection(("127.0.0.1", 7575), timeout=5) as s:
        s.sendall(b"LIST\n")
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", errors="replace")
        if "-->" in text or "Servers" in text or "VirtualHere" in text:
            vh_out = text
            vh_ok  = True
except Exception:
    pass

if not vh_ok and vhbin:
    try:
        r = subprocess.run([vhbin, "-t", "list"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            vh_out = r.stdout
            vh_ok  = True
    except Exception:
        pass

# ── Parse ──────────────────────────────────────────────────────────────────
# Example output:
#   QNAP Hub (QNAP:7575)
#     *--> 802.11ac NIC (QNAP.5134) (In-use by you)
#   Auto-Use All currently on
devices        = []
current_server = None
auto_use_all   = bool(re.search(r'auto.?use.?all\s+currently\s+on', vh_out, re.IGNORECASE))

# Server line: "Some Name (host:port)" — no "-->"
# Device line: "*--> Name (address)" optionally followed by "(In-use by you)" etc.
srv_re = re.compile(r'^\s*(.+?)\s+\((\S+:\d+)\)\s*$')
dev_re = re.compile(r'^\s*(\*?)\s*-->\s+(.+?)\s+\(([^)]+)\)(?:\s+\([^)]*\))?\s*$')

for line in vh_out.splitlines():
    if '-->' in line:
        dev_m = dev_re.match(line)
        if dev_m:
            in_use = bool(dev_m.group(1)) or auto_use_all
            devices.append({
                "name":     dev_m.group(2).strip(),
                "address":  dev_m.group(3).strip(),
                "server":   current_server,
                "auto_use": in_use,
            })
    else:
        srv_m = srv_re.match(line)
        if srv_m:
            current_server = srv_m.group(2)

# ── Enrich each device with DEVICE INFO ────────────────────────────────────
def vh_command(cmd, vhbin, timeout=5):
    """Send a VH IPC command, try TCP first then binary."""
    try:
        with socket.create_connection(("127.0.0.1", 7575), timeout=timeout) as s:
            s.sendall((cmd + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return buf.decode("utf-8", errors="replace")
    except Exception:
        pass
    if vhbin:
        try:
            r = subprocess.run([vhbin, "-t", cmd],
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout
        except Exception:
            pass
    return ""

info_re = re.compile(r'^([A-Z ]+):\s*(.+)$')

for dev in devices:
    addr = dev.get("address", "")
    if not addr:
        continue
    out = vh_command(f"DEVICE INFO,{addr}", vhbin)
    info = {}
    for line in out.splitlines():
        m = info_re.match(line.strip())
        if m:
            info[m.group(1).strip()] = m.group(2).strip()
    if info:
        dev["vendor"]     = info.get("VENDOR", "")
        dev["vendor_id"]  = info.get("VENDOR ID", "")
        dev["product_id"] = info.get("PRODUCT ID", "")
        dev["serial"]     = info.get("SERIAL", "")
        dev["in_use_by"]  = info.get("IN USE BY", "")

print(json.dumps({
    "vh_service_active": svc_active,
    "vh_connected":      vh_ok,
    "auto_use_all":      auto_use_all,
    "count":             len(devices),
    "devices":           devices,
}))
PY
}

collect_telemetry() {
    local cpu_line mem_total mem_free mem_used storage_json vms_json template_lock template_lock_json provision_halt_json
    # Two-sample /proc/stat diff: captures user+nice+system+iowait+irq+softirq (total - idle).
    # More accurate than top's "us" field which only shows user-space CPU.
    # Also snapshot per-VM QEMU process ticks before the sleep so we can compute
    # accurate per-VM CPU% over the same 1-second window (pvesh returns stale 0.0
    # for lightly-loaded VMs because its internal poller only runs every 3-5s).
    local _s1 _s2
    _s1=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    declare -A _vm_ticks_t1
    for _pf in /var/run/qemu-server/*.pid; do
        [[ -f "$_pf" ]] || continue
        _vid="${_pf##*/}"; _vid="${_vid%.pid}"
        _pid=$(cat "$_pf" 2>/dev/null) || continue
        [[ -d "/proc/$_pid" ]] || continue
        _tk=$(awk '{print $14+$15}' "/proc/$_pid/stat" 2>/dev/null) && \
            _vm_ticks_t1[$_vid]="$_tk"
    done
    sleep 1
    _s2=$(grep '^cpu ' /proc/stat 2>/dev/null || echo "cpu 0 0 0 1 0 0 0 0")
    # Compute per-VM CPU percentages normalised the same way as pvesh:
    # pct = (delta_ticks / hz) / num_host_cpus * 100  → fraction of total host CPU (0-100)
    local _hz _ncpus _vm_cpu_json
    _hz=$(getconf CLK_TCK 2>/dev/null || echo "100")
    _ncpus=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo "1")
    _vm_cpu_json="{"
    for _vid in "${!_vm_ticks_t1[@]}"; do
        _pf2="/var/run/qemu-server/${_vid}.pid"
        [[ -f "$_pf2" ]] || continue
        _pid2=$(cat "$_pf2" 2>/dev/null) || continue
        [[ -d "/proc/$_pid2" ]] || continue
        _tk2=$(awk '{print $14+$15}' "/proc/$_pid2/stat" 2>/dev/null) || continue
        _delta=$(( _tk2 - _vm_ticks_t1[$_vid] ))
        _pct=$(awk -v d="$_delta" -v hz="$_hz" -v n="$_ncpus" \
               'BEGIN { printf "%.2f", (d / hz / n) * 100 }' 2>/dev/null) || _pct="0"
        _vm_cpu_json+="\"${_vid}\":${_pct},"
    done
    _vm_cpu_json="${_vm_cpu_json%,}}"
    cpu_line=$(awk -v s1="$_s1" -v s2="$_s2" 'BEGIN {
        n = split(s1, a); split(s2, b)
        t1 = 0; t2 = 0
        for (i = 2; i <= n; i++) { t1 += a[i]; t2 += b[i] }
        dt = t2 - t1; di = b[5] - a[5]
        val = (dt > 0) ? (1 - di/dt) * 100 : 0
        printf "%.1f\n", val
    }' 2>/dev/null) || cpu_line=0
    mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    mem_free=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
    mem_used=$(( mem_total - mem_free ))

    storage_json="[]"
    if command -v pvesm &>/dev/null; then
        storage_json=$(pvesm status 2>/dev/null | awk 'NR>1 {
            printf "{\"name\":\"%s\",\"type\":\"%s\",\"used\":%s,\"total\":%s},",
            $1,$2,$5,$4
        }' | sed 's/,$//' | awk 'BEGIN{print "["}{print}END{print "]"}' | tr -d '\n')
    fi

    local pve_version=""
    if command -v pveversion &>/dev/null; then
        pve_version=$(pveversion 2>/dev/null | awk -F'/' 'NR==1{print $2}' || true)
    fi

    vms_json="[]"
    if command -v pvesh &>/dev/null; then
        # pvesh returns: cpu (0.0-1.0 fraction), mem (bytes), maxmem (bytes)
        # Normalise: cpu → percent, mem/maxmem → MB. Merge QEMU VMs + LXC containers.
        # VM_PROC_CPU carries pre-computed per-VM CPU% from /proc/{pid}/stat (same 1s window
        # as host CPU), which is more accurate than pvesh's stale internal poll value.
        vms_json=$(VM_PROC_CPU="$_vm_cpu_json" python3 -c "
import json, subprocess, sys, re, os
from pathlib import Path

proc_cpu = json.loads(os.environ.get('VM_PROC_CPU', '{}'))

META_RE = re.compile(r'(?:reclone[-_ ](?:source|template)|template[-_ ]source)\\s*[:=]\\s*(\\d+)', re.I)


def fetch(path):
    try:
        r = subprocess.run(['pvesh','get',path,'--output-format','json'],
                           capture_output=True, text=True, timeout=15)
        return json.loads(r.stdout) if r.returncode == 0 else []
    except Exception:
        return []


def config_text(kind, vmid):
    cfg_path = Path('/etc/pve/qemu-server' if kind == 'qemu' else '/etc/pve/lxc') / f'{vmid}.conf'
    try:
        return cfg_path.read_text(encoding='utf-8')
    except Exception:
        return ''


PROTECTED_VMIDS = {1001}

def reclone_info(kind, vmid):
    # Hard failsafe: these VMIDs can never be recloned regardless of config
    if vmid in PROTECTED_VMIDS:
        return None, False, [], None, False, 'Protected system VM — cannot be managed from this UI', False
    text = config_text(kind, vmid)
    source_vmid = None
    for line in text.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        if key.strip() in {'description', 'tags', 'notes', 'comment'}:
            m = META_RE.search(value)
            if m:
                source_vmid = int(m.group(1))
                break
    if kind == 'qemu':
        m = re.search(r'^usb\\d+:\\s.*?host=([^,\\s]+)', text, re.M)
        bus_path = m.group(1) if m else None
        # has_usb_config: True if any USB passthrough line exists (host= or mapping= or any format)
        has_usb_config = bool(re.search(r'^usb\\d+:', text, re.M))
        # pci_passthrough_addrs: list of PCI bus addresses passed through to this VM (hostpciN: lines)
        pci_addrs = re.findall(r'^hostpci\\d+:\\s*([0-9a-fA-F:.]+)', text, re.M)
        supported = bool(bus_path) or (source_vmid is not None)
        reason = None if supported else 'No USB passthrough mapping or reclone-source metadata found'
        is_template = bool(re.search(r'^template:\\s*1\\s*$', text, re.M))
        return bus_path, has_usb_config, pci_addrs, source_vmid, supported, reason, is_template
    supported = source_vmid is not None
    reason = None if supported else 'Set tags/description with reclone-source=<template CTID>'
    is_template = bool(re.search(r'^template:\\s*1\\s*$', text, re.M))
    return None, False, [], source_vmid, supported, reason, is_template


node = subprocess.run(['hostname', '-s'], capture_output=True, text=True).stdout.strip()
qemu = fetch(f'/nodes/{node}/qemu')
lxc  = fetch(f'/nodes/{node}/lxc')

out = []
for v in qemu:
    vmid = v.get('vmid')
    bus_path, has_usb_config, pci_addrs, source_vmid, supported, reason, is_template = reclone_info('qemu', vmid)
    raw_cpu = v.get('cpu')
    # Prefer proc-based CPU (accurate 1s sample) over pvesh's stale internal value.
    proc_pct = proc_cpu.get(str(vmid))
    cpu_val = proc_pct if proc_pct is not None else (round(float(raw_cpu) * 100, 2) if raw_cpu is not None else None)
    out.append({
        'vmid':                vmid,
        'name':                v.get('name', ''),
        'status':              v.get('status', 'unknown'),
        'cpu':                 cpu_val,
        'mem':                 round(int(v.get('mem') or 0) / 1024 / 1024),
        'maxmem':              round(int(v.get('maxmem') or 0) / 1024 / 1024),
        'is_template':         bool(v.get('template', 0)) or is_template,
        'type':                'qemu',
        'has_usb_config':      has_usb_config,
        'pci_passthrough_addrs': pci_addrs,
        'reclone_bus_path':    bus_path,
        'reclone_source_vmid': source_vmid,
        'reclone_supported':   supported,
        'reclone_reason':      reason,
        'tags':                str(v.get('tags') or ''),
    })
for v in lxc:
    vmid = v.get('vmid')
    _bus_path, _has_usb, _pci_addrs, source_vmid, supported, reason, is_template = reclone_info('lxc', vmid)
    raw_cpu = v.get('cpu')
    out.append({
        'vmid':                vmid,
        'name':                v.get('name', ''),
        'status':              v.get('status', 'unknown'),
        'cpu':                 round(float(raw_cpu) * 100, 2) if raw_cpu is not None else None,
        'mem':                 round(int(v.get('mem') or 0) / 1024 / 1024),
        'maxmem':              round(int(v.get('maxmem') or 0) / 1024 / 1024),
        'is_template':         is_template,
        'type':                'lxc',
        'pci_passthrough_addrs': [],
        'reclone_source_vmid': source_vmid,
        'reclone_supported':   supported,
        'reclone_reason':      reason,
    })
print(json.dumps(out))
" 2>/dev/null || echo "[]")
    fi

    # Fallback: qm list + pct list (no real-time CPU/mem usage; maxmem from qm list col 4)
    # qm list columns: VMID  NAME  STATUS  MEM(MB)  BOOTDISK(GB)  PID
    if [[ "$vms_json" == "[]" ]] && command -v qm &>/dev/null; then
        local tmpl_ids=""
        for conf in /etc/pve/qemu-server/*.conf; do
            [[ -f "$conf" ]] || continue
            grep -q "^template: 1" "$conf" && tmpl_ids+="$(basename "$conf" .conf),"
        done
        tmpl_ids="${tmpl_ids%,}"
        local qemu_part lxc_part
        qemu_part=$(qm list 2>/dev/null | awk -v tmpls="$tmpl_ids" 'BEGIN {
            n=split(tmpls, t, ","); for(i=1;i<=n;i++) tmpl_set[t[i]]=1
        }
        NR>1 {
            is_tmpl = ($1 in tmpl_set) ? "true" : "false"
            printf "{\"vmid\":%s,\"name\":\"%s\",\"status\":\"%s\",\"cpu\":null,\"mem\":0,\"maxmem\":%s,\"is_template\":%s,\"type\":\"qemu\"},",
            $1,$2,$3,$4,is_tmpl
        }')
        lxc_part=$(pct list 2>/dev/null | awk 'NR>1 {
            printf "{\"vmid\":%s,\"name\":\"%s\",\"status\":\"%s\",\"cpu\":null,\"mem\":0,\"maxmem\":0,\"is_template\":false,\"type\":\"lxc\"},",
            $1,$3,$2
        }')
        local combined="${qemu_part}${lxc_part}"
        combined="${combined%,}"
        vms_json="[${combined}]"
    fi

    # Collect hardware watchdog fault log
    local hw_faults_json='{"faults":[]}'
    if [[ -f "$HW_FAULT_LOG" ]]; then
        hw_faults_json=$(cat "$HW_FAULT_LOG" 2>/dev/null || printf '{"faults":[]}')
    fi
    local hw_last_reset_json='null'
    if [[ -f "$HW_RESET_RECORD" ]]; then
        hw_last_reset_json=$(cat "$HW_RESET_RECORD" 2>/dev/null || printf 'null')
    fi

    template_lock=$(probe_template_lock_status 2>/dev/null || true)
    template_lock_json=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$template_lock" 2>/dev/null || printf '""')
    provision_halt_json='null'
    if [[ "$AUTO_PROVISION" == "on" ]]; then
        provision_halt_json=$(read_json_cache_or_default "$PROVISION_HALT_CACHE" 'null')
    elif [[ -f "$PROVISION_HALT_CACHE" ]]; then
        rm -f "$PROVISION_HALT_CACHE" 2>/dev/null || true
    fi

    cat <<JSON
{
  "node": {
    "hostname": "$(hostname)",
    "cpu_percent": ${cpu_line:-0},
    "mem_used_kb": ${mem_used:-0},
    "mem_total_kb": ${mem_total:-0},
    "storage": ${storage_json:-[]}
  },
  "agent_version": "${AGENT_VERSION}",
  "vmid_range": {"start": ${start_vmid}, "end": ${end_vmid}},
  "vm_set_override": ${VM_SET_OVERRIDE:-0},
  "effective_vm_set": ${id_num},
  "reseed_in_progress": $([ -f "$RESEED_LOCK_FILE" ] && echo true || echo false),
  "pve_version": "${pve_version}",
  "template_lock": ${template_lock_json},
  "missing_timeout_mins": ${MISSING_TIMEOUT},
  "vms": ${vms_json:-[]},
  "reclone_state": $(read_json_cache_or_default "$RECLONE_STATE_CACHE" '{"status":"idle","active_vmids":[]}'),
  "unknown_usb": $(read_json_cache_or_default "$USB_UNKNOWN_CACHE" "${UNKNOWN_USB_JSON:-[]}"),
  "usb_state": $(read_json_cache_or_default "$USB_STATE_CACHE" "${USB_STATE_JSON:-[]}"),
  "usb_quarantine": $(read_json_cache_or_default "$USB_QUARANTINE_CACHE" "[]"),
  "orphan_vms": $(read_json_cache_or_default "$ORPHAN_VMS_CACHE" "[]"),
  "present_usb": $(read_json_cache_or_default "$USB_PRESENT_CACHE" "${PRESENT_USB_JSON:-[]}"),
  "provision_halt": ${provision_halt_json},
  "blacklisted_drivers": ${BLACKLISTED_DRIVERS_JSON},
  "vh_devices": $(collect_vh_devices 2>/dev/null || echo '{"vh_connected":false,"vh_service_active":false,"count":0,"devices":[]}'),
  "t3_pci_devices": $(collect_t3_pci_devices 2>/dev/null || echo '[]'),
  "hw_faults": ${hw_faults_json},
  "hw_last_reset": ${hw_last_reset_json},
  "log_lines": $(collect_log_lines)
}
JSON
}

# ── Self-update ────────────────────────────────────────────────────────────────
# Downloads the latest agent script from GitHub, validates it, and replaces the
# running binary if the SHA256 hash differs. Called both from the inbox handler
# (update_agent command) and from the main loop's periodic self-check.
self_update_agent() {
    local requested_branch="${1:-}"
    local requested_repo_raw="${2:-}"
    local agent_script="/usr/local/bin/client-sim-proxmox-agent"
    local configured_branch configured_repo_raw branch repo_raw download_dir tmp_file selected_repo_raw
    local -a repo_candidates=()
    configured_branch=$(grep -oP '(?<=CLIENT_SIM_REPO_BRANCH=).*' "$ENV_FILE" 2>/dev/null | tr -d '[:space:]')
    configured_repo_raw=$(grep -oP '(?<=CLIENT_SIM_REPO_RAW=).*' "$ENV_FILE" 2>/dev/null | tr -d '[:space:]')
    branch="${requested_branch:-$configured_branch}"
    branch="${branch:-main}"
    download_dir="/var/lib/client-sim/update"
    tmp_file="${download_dir}/proxmox-agent.sh.download"
    mkdir -p "$download_dir"

    local candidate normalized existing already_added
    for candidate in \
        "$requested_repo_raw" \
        "${CLIENT_SIM_REPO_RAW:-}" \
        "$configured_repo_raw" \
        "https://raw.githubusercontent.com/lbockenstedt/cs" \
        "https://github.com/lbockenstedt/cs/raw"
    do
        [[ -n "$candidate" ]] || continue
        normalized=$(normalize_repo_raw_for_branch "$candidate" "$branch") || continue
        already_added=0
        for existing in "${repo_candidates[@]}"; do
            if [[ "$existing" == "$normalized" ]]; then
                already_added=1
                break
            fi
        done
        if (( already_added == 0 )); then
            repo_candidates+=("$normalized")
        fi
    done

    for repo_raw in "${repo_candidates[@]}"; do
        log "Checking for agent update from GitHub (branch: ${branch}, current: v${AGENT_VERSION}, source: ${repo_raw})..."
        if curl -fsSL --connect-timeout 10 --retry 2 --retry-delay 2 --max-time 60 "${repo_raw}/proxmox/proxmox-agent.sh" -o "$tmp_file"; then
            selected_repo_raw="$repo_raw"
            break
        fi
        log "WARNING: Failed to download agent update from ${repo_raw}"
    done
    if [[ -z "$selected_repo_raw" ]]; then
        rm -f "$tmp_file"
        log "ERROR: Failed to download agent update from all configured GitHub sources"
        return 1
    fi
    repo_raw="$selected_repo_raw"
    if ! bash -n "$tmp_file" 2>/dev/null; then
        rm -f "$tmp_file"
        log "ERROR: Downloaded agent script failed syntax check — aborting update"
        return 1
    fi
    local current_hash new_hash new_version
    current_hash=$(sha256sum "$agent_script" 2>/dev/null | awk '{print $1}')
    new_hash=$(sha256sum "$tmp_file" | awk '{print $1}')
    new_version=$(grep '^AGENT_VERSION=' "$tmp_file" | cut -d'"' -f2)
    if [[ "$current_hash" == "$new_hash" ]]; then
        rm -f "$tmp_file"
        if [[ -n "$requested_branch" && "$requested_branch" != "$configured_branch" ]]; then
            save_repo_branch "$branch"
        fi
        # Even if the file on disk is already up to date, the RUNNING process may still
        # be on an older version (disk was updated but service never restarted).
        # If the on-disk version differs from our running AGENT_VERSION, force a restart.
        if [[ -n "$new_version" && "$new_version" != "$AGENT_VERSION" ]]; then
            log "Disk is already v${new_version} but running v${AGENT_VERSION} — forcing restart..."
            if ! schedule_agent_restart; then
                log "ERROR: Failed to schedule agent restart"
                return 1
            fi
            return 0
        fi
        log "Agent is already up to date (v${AGENT_VERSION})"
        sync_pve_scripts "$repo_raw" "$branch"
        return 0
    fi
    install -m 0755 "$tmp_file" "$agent_script"
    rm -f "$tmp_file"
    save_repo_branch "$branch"
    log "Agent updated v${AGENT_VERSION} → v${new_version} from ${repo_raw} — scheduling restart..."
    # Also sync /etc/pve/scripts/ so clone.sh, ini-parser.sh, client-setup.conf etc stay current
    sync_pve_scripts "$repo_raw" "$branch"
    if ! schedule_agent_restart; then
        log "ERROR: Failed to schedule agent restart"
        return 1
    fi
}

# ── PVE scripts sync ───────────────────────────────────────────────────────────
# Deploys proxmox helper scripts (clone.sh, ini-parser.sh, check_guest.sh,
# client-setup.conf, sync-scripts.sh) from GitHub to /etc/pve/scripts/.
# Called on install and whenever self_update_agent() applies an update so the
# pve scripts always match the running agent version.
sync_pve_scripts() {
    local repo_raw="${1:-}" branch="${2:-main}"
    local pve_dir="/etc/pve/scripts"
    local -a pve_files=("clone.sh" "ini-parser.sh" "check_guest.sh" "client-setup.conf" "sync-scripts.sh")

    [[ -d /etc/pve ]] || { log "sync_pve_scripts: /etc/pve not found — skipping"; return 0; }
    mkdir -p "$pve_dir" || { log "sync_pve_scripts: cannot create ${pve_dir} — skipping"; return 0; }

    # Fall back to the canonical raw URL if not supplied
    if [[ -z "$repo_raw" ]]; then
        repo_raw=$(grep -oP '(?<=CLIENT_SIM_REPO_RAW=).*' "$ENV_FILE" 2>/dev/null | tr -d '[:space:]')
        repo_raw="${repo_raw:-https://raw.githubusercontent.com/lbockenstedt/cs}"
        repo_raw=$(normalize_repo_raw_for_branch "$repo_raw" "$branch" 2>/dev/null || echo "${repo_raw}/${branch}")
    fi

    local _ok=0 _skip=0 _fail=0
    for _f in "${pve_files[@]}"; do
        local _dest="${pve_dir}/${_f}" _tmp="${pve_dir}/.${_f}.tmp"
        if curl -fsSL --connect-timeout 15 --max-time 60 "${repo_raw}/proxmox/${_f}" -o "$_tmp" 2>/dev/null; then
            if ! cmp -s "$_tmp" "$_dest" 2>/dev/null; then
                mv "$_tmp" "$_dest"
                log "sync_pve_scripts: updated ${_dest}"
                (( _ok++ )) || true
            else
                rm -f "$_tmp"
                (( _skip++ )) || true
            fi
        else
            rm -f "$_tmp"
            log "sync_pve_scripts: WARNING — failed to download ${_f}"
            (( _fail++ )) || true
        fi
    done
    log "sync_pve_scripts: ${_ok} updated, ${_skip} unchanged, ${_fail} failed in ${pve_dir}"
}

# ── Hardware Watchdog ──────────────────────────────────────────────────────────
# Tier 1 — Immediate reboot (unrecoverable, system cannot self-heal):
#   Kernel panic, BUG, Oops        — kernel integrity lost
#   NVMe controller down/failed    — storage completely gone
#   ATA hard reset failed          — drive unresponsive after recovery attempts
#   EXT4/XFS journal abort         — filesystem will not recover without reboot
#   PCIe Fatal AER                 — PCIe device permanently faulted
#   EDAC uncorrected (UE)          — uncorrectable memory error
#
# Tier 2 — Reboot after N hits (recoverable individually, storm = sick system):
#   NVMe I/O timeout               — may recover, but N = stuck
#   blk_update_request I/O error   — block layer errors
#   ata timeout / soft reset       — ATA retries, N = recurring
#   hung task                      — kernel task stuck, may cascade
#   EDAC corrected errors          — single-bit ECC, many = hardware degrading
#   xhci_hcd/ehci_hcd died         — USB controller crashed
#   OOM kills                      — memory pressure storm

_record_hw_fault() {
    local tier="$1" pattern="$2" detail="$3"
    python3 - "$tier" "$pattern" "$detail" "$HW_FAULT_LOG" <<'PY' 2>/dev/null || true
import json, sys, time
from pathlib import Path

tier, pattern, detail, path = sys.argv[1:5]
f = Path(path)
try:
    data = json.loads(f.read_text()) if f.exists() else {'faults': []}
except Exception:
    data = {'faults': []}
data['faults'].append({
    'ts': time.time(),
    'tier': tier,
    'pattern': pattern,
    'detail': detail,
})
data['faults'] = data['faults'][-100:]
data['last_updated'] = time.time()
f.write_text(json.dumps(data))
PY
}

_hw_reboot_cooled_down() {
    if [[ ! -f "$HW_RESET_RECORD" ]]; then
        return 0
    fi
    local last_ts
    last_ts=$(python3 - "$HW_RESET_RECORD" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    data = json.loads(open(sys.argv[1]).read())
    print(int(data.get('ts', 0)))
except Exception:
    print(0)
PY
)
    local now
    now=$(date +%s)
    if (( now - last_ts >= HW_REBOOT_COOLDOWN )); then
        return 0
    fi
    return 1
}

# Issue an immediate hard reset. Tries IPMI first, then sysrq, then reboot -f.
hard_reset() {
    local reason="${1:-unknown}"
    log "WATCHDOG: Hard reset initiated — ${reason}"

    # Write reset record so next boot can report why we rebooted
    python3 - "$reason" "$HW_RESET_RECORD" "$AGENT_VERSION" <<'PY' 2>/dev/null || true
import json, sys, time
from pathlib import Path

reason, path, version = sys.argv[1:4]
Path(path).write_text(json.dumps({
    'ts': time.time(),
    'reason': reason,
    'agent_version': version,
}))
PY

    # Notify the spoke immediately — best-effort, don't block the reset on failure
    # The spoke stores this and relays to the hub so the event is recorded even
    # if the agent never sends another telemetry post after rebooting.
    local _hw_hostname _hw_payload
    _hw_hostname=$(hostname 2>/dev/null || echo "unknown")
    _hw_payload=$(python3 -c "
import json, sys, time
reason, hostname, version = sys.argv[1:4]
print(json.dumps({'hostname': hostname, 'reason': reason, 'tier': 'watchdog',
                  'ts': time.time(), 'agent_version': version}))
" "$reason" "$_hw_hostname" "$AGENT_VERSION" 2>/dev/null || echo "{}")
    if [[ -n "$_hw_payload" && "$_hw_payload" != "{}" ]]; then
        curl -sk --max-time 5 -X POST "${SERVER_URL}/api/proxmox/hw_reset_event" \
            -H "Content-Type: application/json" \
            -H "X-API-Key: ${API_KEY}" \
            -d "$_hw_payload" >/dev/null 2>&1 || true
    fi

    # 1. IPMI chassis hard reset (best option — equivalent to pressing reset button)
    if command -v ipmitool &>/dev/null; then
        log "WATCHDOG: Attempting IPMI chassis power reset"
        if ipmitool chassis power reset 2>/dev/null; then
            sleep 30
        fi
    fi

    # 2. Linux sysrq immediate reboot (no sync, no unmount — truly hard)
    log "WATCHDOG: Falling back to sysrq-b"
    echo 1 > /proc/sys/kernel/sysrq 2>/dev/null || true
    sync 2>/dev/null || true
    echo b > /proc/sysrq-trigger 2>/dev/null || true
    sleep 5

    # 3. Last resort
    log "WATCHDOG: Final fallback — reboot -f"
    reboot -f 2>/dev/null || true
}

hw_watchdog_check() {
    command -v journalctl &>/dev/null || return 0

    local -a TIER1_PATTERNS=(
        "Kernel panic"
        "kernel BUG at"
        "BUG: unable to handle kernel"
        "Oops: general protection"
        "RIP:.*Oops"
        "double fault"
        "machine check exception"
        "nvme.*controller is down"
        "nvme.*failed state"
        "nvme.*Abort status.*DNR"
        "nvme.*reset: controller failed"
        "ata.*SRST failed.*error=-19"
        "ata.*hard reset failed"
        "ata.*failed to recover some devices"
        "EXT4-fs error.*aborting journal"
        "EXT4-fs.*remounting filesystem read-only"
        "XFS.*log I/O error.*shutting down filesystem"
        "XFS.*metadata I/O error.*shutting down"
        "BTRFS.*error.*transaction abort"
        "pcieport.*PCIe Bus Error.*severity=Fatal"
        "AER.*Uncorrected.*Fatal"
        "EDAC.*UE.*uncorrected error"
        "Hardware Error.*severity.*Fatal"
        "MCE.*Hardware Error.*fatal"
    )

    local -a TIER2_PATTERNS=(
        "nvme.*I/O.*timeout"
        "nvme.*Abort command"
        "ata.*exception Emask"
        "ata.*timeout waiting for"
        "blk_update_request.*I/O error"
        "I/O error.*dev.*sector"
        "scsi.*timing out command"
        "sd.*Result: hostbyte=DID_TIMEOUT"
        "SCSI error.*sense key.*HARDWARE ERROR"
        "SCSI error.*sense key.*MEDIUM ERROR"
        "ata.*soft resetting link"
        "ata.*hard resetting link"
        "task.*blocked for more than.*seconds"
        "hung_task.*blocked"
        "EDAC.*CE.*memory error"
        "MCE.*corrected error"
        "xhci_hcd.*died"
        "ehci_hcd.*died"
        "usb.*hub.*unable to enumerate"
        "pcieport.*PCIe Bus Error.*severity=Corrected"
        "Out of memory.*Kill process"
        "oom.*killed process"
    )

    local cursor_args=()
    if [[ -f "$HW_WATCHDOG_CURSOR" ]]; then
        local saved_cursor
        saved_cursor=$(cat "$HW_WATCHDOG_CURSOR" 2>/dev/null || true)
        [[ -n "$saved_cursor" ]] && cursor_args=("--cursor=${saved_cursor}")
    fi

    local new_cursor
    new_cursor=$(journalctl -k -n 0 --show-cursor 2>/dev/null | grep -oP '(?<=-- cursor: ).*' || true)
    [[ -n "$new_cursor" ]] && printf '%s' "$new_cursor" > "$HW_WATCHDOG_CURSOR" 2>/dev/null || true

    local new_msgs
    new_msgs=$(journalctl -k --no-pager -o short-monotonic "${cursor_args[@]}" 2>/dev/null || true)
    if [[ -z "$new_msgs" ]]; then
        return 0
    fi

    local t1_matched=""
    for pat in "${TIER1_PATTERNS[@]}"; do
        local hit
        hit=$(printf '%s\n' "$new_msgs" | grep -iE "$pat" | head -1 || true)
        if [[ -n "$hit" ]]; then
            t1_matched="$pat"
            log "WATCHDOG: Tier-1 fault detected — pattern='${pat}'"
            log "WATCHDOG: Matched line: ${hit:0:200}"
            _record_hw_fault "tier1" "$pat" "$hit"
            break
        fi
    done

    if [[ -n "$t1_matched" ]]; then
        if _hw_reboot_cooled_down; then
            post_telemetry 2>/dev/null || true
            if [[ "${WATCHDOG_REBOOT_ENABLED:-on}" == "on" ]]; then
                hard_reset "Tier-1 hardware fault: ${t1_matched}"
            else
                log "WATCHDOG: Tier-1 fault detected — auto-reboot disabled, reporting only"
            fi
        else
            log "WATCHDOG: Tier-1 fault detected but reboot cooldown active — skipping reset"
        fi
        return 0
    fi

    local t2_count=0
    local -a t2_reasons=()
    for pat in "${TIER2_PATTERNS[@]}"; do
        local count=0
        count=$(printf '%s\n' "$new_msgs" | grep -icE "$pat" 2>/dev/null) || true
        [[ "$count" =~ ^[0-9]+$ ]] || count=0
        if [[ "$count" -gt 0 ]]; then
            t2_count=$(( t2_count + count ))
            t2_reasons+=("${pat}(${count})")
            _record_hw_fault "tier2" "$pat" "count=${count}"
        fi
    done

    if [[ "$t2_count" -ge "$HW_TIER2_REBOOT_THRESHOLD" ]]; then
        log "WATCHDOG: Tier-2 fault threshold reached — ${t2_count} hits: ${t2_reasons[*]}"
        if _hw_reboot_cooled_down; then
            post_telemetry 2>/dev/null || true
            if [[ "${WATCHDOG_REBOOT_ENABLED:-on}" == "on" ]]; then
                hard_reset "Tier-2 hardware faults (${t2_count} hits): ${t2_reasons[*]}"
            else
                log "WATCHDOG: Tier-2 threshold reached — auto-reboot disabled, reporting only"
            fi
        else
            log "WATCHDOG: Tier-2 threshold reached but cooldown active — skipping reset"
        fi
    fi
}

hw_watchdog_loop() {
    log "Hardware watchdog started (interval=${HW_WATCHDOG_INTERVAL}s, tier2_threshold=${HW_TIER2_REBOOT_THRESHOLD})"
    if [[ ! -f "$HW_WATCHDOG_CURSOR" ]]; then
        local init_cursor
        init_cursor=$(journalctl -k -n 0 --show-cursor 2>/dev/null | grep -oP '(?<=-- cursor: ).*' || true)
        [[ -n "$init_cursor" ]] && printf '%s' "$init_cursor" > "$HW_WATCHDOG_CURSOR" 2>/dev/null || true
        log "WATCHDOG: Initialized journal cursor"
    fi
    while true; do
        sleep "$HW_WATCHDOG_INTERVAL"
        hw_watchdog_check || log "WATCHDOG: check failed (non-fatal)"
    done
}

run_backup_command() {
    local vm_ids_json="${1:-[]}" job_id="${2:-}" azure_account="${3:-}" azure_container="${4:-}" azure_key="${5:-}"
    local retention="${6:-3}" spoke_id="${7:-}"
    local backup_root="/tmp/cs-backup" overall_status=0
    local -a vm_ids=()
    mapfile -t vm_ids < <(python3 - "$vm_ids_json" <<'PY' 2>/dev/null || true
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else '[]'
try:
    data = json.loads(raw)
except Exception:
    data = []
if not isinstance(data, list):
    data = [data] if data not in (None, '') else []
for item in data:
    print(item)
PY
)
    if [[ ${#vm_ids[@]} -eq 0 ]]; then
        log "WARNING: backup command received without vm_ids"
        return 1
    fi
    mkdir -p "$backup_root"
    [[ -n "$azure_account" ]] && export AZCOPY_ACCOUNT_NAME="$azure_account"
    [[ -n "$azure_key" ]] && export AZCOPY_ACCOUNT_KEY="$azure_key"
    for vmid in "${vm_ids[@]}"; do
        local vm_backup_dir="$backup_root/$vmid"
        local backup_file="" destination_url=""
        rm -rf "$vm_backup_dir"
        mkdir -p "$vm_backup_dir"
        log "Starting backup job ${job_id:-n/a} for VM $vmid (retention=${retention})"
        [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "starting" 0 "starting" "" "$spoke_id"
        [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "running" 15 "vzdump" "" "$spoke_id"
        if ! vzdump "$vmid" --compress zstd --mode snapshot --dumpdir "$vm_backup_dir" >>"$AGENT_LOG" 2>&1; then
            log "ERROR: vzdump failed for VM $vmid"
            [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "failed" 100 "vzdump" "vzdump failed — check $AGENT_LOG" "$spoke_id"
            overall_status=1
            rm -rf "$vm_backup_dir"
            continue
        fi
        backup_file=$(find "$vm_backup_dir" -maxdepth 1 -type f | sort | tail -n 1)
        if [[ -z "$backup_file" ]]; then
            log "ERROR: unable to locate backup artifact for VM $vmid"
            [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "failed" 100 "locate_backup" "backup artifact not found" "$spoke_id"
            overall_status=1
            rm -rf "$vm_backup_dir"
            continue
        fi
        if ! command -v azcopy >/dev/null 2>&1; then
            log "WARNING: azcopy not installed; skipping upload for VM $vmid"
            [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "completed" 100 "upload_skipped" "azcopy not installed" "$spoke_id"
            rm -rf "$vm_backup_dir"
            continue
        fi
        if [[ -z "$azure_account" || -z "$azure_container" ]]; then
            log "ERROR: missing Azure destination for VM $vmid backup upload"
            [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "failed" 100 "upload" "missing Azure destination" "$spoke_id"
            overall_status=1
            rm -rf "$vm_backup_dir"
            continue
        fi
        destination_url="https://${azure_account}.blob.core.windows.net/${azure_container}/${spoke_id}/${vmid}/$(basename "$backup_file")"
        [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "running" 80 "uploading" "" "$spoke_id"
        if ! azcopy copy "$backup_file" "$destination_url" --overwrite=true >>"$AGENT_LOG" 2>&1; then
            log "ERROR: azcopy upload failed for VM $vmid"
            [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "failed" 100 "uploading" "upload failed — check $AGENT_LOG" "$spoke_id"
            overall_status=1
            rm -rf "$vm_backup_dir"
            continue
        fi
        log "Backup job ${job_id:-n/a} completed for VM $vmid"
        [[ -n "$job_id" ]] && emit_backup_progress "$job_id" "$vmid" "completed" 100 "completed" "" "$spoke_id"
        rm -rf "$vm_backup_dir"
    done
    return "$overall_status"
}

run_reseed_command() {
    local blob_url="${1:-}" vm_id="${2:-100}" job_id="${3:-}"
    local download_path="/tmp/reseed-vm-${vm_id}.vma.zst"
    local status=0
    touch "$RESEED_LOCK_FILE"
    [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "starting" "starting"
    if [[ -n "$blob_url" ]]; then
        log "Starting reseed job ${job_id:-n/a} for VM $vm_id from $blob_url"
        [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "running" "downloading"
        if ! curl -L --progress-bar -o "$download_path" "$blob_url" >>"$AGENT_LOG" 2>&1; then
            log "ERROR: reseed download failed for VM $vm_id"
            [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "failed" "downloading" "download failed — check $AGENT_LOG"
            status=1
        elif [[ -n "$job_id" ]]; then
            emit_reseed_progress "$job_id" "running" "restoring"
        fi
        if [[ "$status" -eq 0 ]] && ! qmrestore "$download_path" "$vm_id" --force >>"$AGENT_LOG" 2>&1; then
            log "ERROR: qmrestore failed for VM $vm_id"
            [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "failed" "restoring" "qmrestore failed — check $AGENT_LOG"
            status=1
        elif [[ "$status" -eq 0 ]] && [[ -n "$job_id" ]]; then
            emit_reseed_progress "$job_id" "running" "templating"
        fi
        if [[ "$status" -eq 0 ]] && ! qm template "$vm_id" >>"$AGENT_LOG" 2>&1; then
            log "ERROR: qm template failed for VM $vm_id"
            [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "failed" "templating" "qm template failed — check $AGENT_LOG"
            status=1
        fi
    else
        log "No blob_url supplied for reseed; running clone.sh only"
    fi
    if [[ "$status" -eq 0 ]] && [[ -f /opt/client-sim-repo/proxmox/clone.sh ]]; then
        [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "running" "cloning"
        if ! bash /opt/client-sim-repo/proxmox/clone.sh >>"$AGENT_LOG" 2>&1; then
            log "ERROR: clone.sh failed during reseed"
            [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "failed" "cloning" "clone.sh failed — check $AGENT_LOG"
            status=1
        fi
    elif [[ "$status" -eq 0 ]]; then
        log "WARNING: clone.sh not found; reseed restore completed without clone step"
    fi
    if [[ "$status" -eq 0 ]]; then
        log "Reseed job ${job_id:-n/a} completed for VM $vm_id"
        [[ -n "$job_id" ]] && emit_reseed_progress "$job_id" "completed" "completed"
    fi
    rm -f "$download_path" "$RESEED_LOCK_FILE"
    return "$status"
}

process_backup_ws_command() {
    local raw="${1:-{}}"
    local parsed
    local -a fields=()
    parsed=$(python3 - "$raw" <<'PY' 2>/dev/null || printf '[]\n\n\n\n\n3\n\n'
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else '{}'
try:
    data = json.loads(raw)
except Exception:
    data = {}
payload = data.get('payload', data) if isinstance(data, dict) else {}
if not isinstance(payload, dict):
    payload = {}
vm_ids = payload.get('vm_ids', [])
if not isinstance(vm_ids, list):
    vm_ids = [vm_ids] if vm_ids not in (None, '') else []
print(json.dumps(vm_ids))
print(str(payload.get('job_id', '')))
print(str(payload.get('azure_account', '')))
print(str(payload.get('azure_container', '')))
print(str(payload.get('azure_key', '')))
print(str(payload.get('retention', '3')))
print(str(payload.get('spoke_id', '')))
PY
)
    mapfile -t fields <<< "$parsed"
    run_backup_command "${fields[0]:-[]}" "${fields[1]:-}" "${fields[2]:-}" "${fields[3]:-}" "${fields[4]:-}" "${fields[5]:-3}" "${fields[6]:-}"
}

process_reseed_ws_command() {
    local raw="${1:-{}}"
    local parsed
    local -a fields=()
    parsed=$(python3 - "$raw" <<'PY' 2>/dev/null || printf '\n100\n\n'
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else '{}'
try:
    data = json.loads(raw)
except Exception:
    data = {}
payload = data.get('payload', data) if isinstance(data, dict) else {}
if not isinstance(payload, dict):
    payload = {}
print(str(payload.get('blob_url', '')))
print(str(payload.get('vm_id', '100')))
print(str(payload.get('job_id', '')))
PY
)
    mapfile -t fields <<< "$parsed"
    run_reseed_command "${fields[0]:-}" "${fields[1]:-100}" "${fields[2]:-}"
}

execute_vm_command() {
    local action="$1" vmid="${2:-}" _type="${3:-qemu}" _source_vmid="${4:-}" _branch="${5:-}" _repo_raw="${6:-}" args_bus_path="${7:-}"
    local guest_type="${_type:-qemu}"
    if [[ -n "$vmid" && "$guest_type" != "lxc" ]]; then
        if pct status "$vmid" >/dev/null 2>&1 && ! qm status "$vmid" >/dev/null 2>&1; then
            guest_type="lxc"
        fi
    fi
    action=$(normalize_command_name "$action")
    case "$action" in
        start_vm)
            if [[ "$guest_type" == "lxc" ]]; then timeout 60 pct start "$vmid"; else timeout 60 qm start "$vmid"; fi
            ;;
        stop_vm)
            if [[ "$guest_type" == "lxc" ]]; then timeout 60 pct stop "$vmid"; else timeout 60 qm stop "$vmid"; fi
            ;;
        reboot_vm)
            if [[ "$guest_type" == "lxc" ]]; then pct reboot "$vmid" 2>/dev/null || true; else qm reboot "$vmid" 2>/dev/null || true; fi
            ;;
        snapshot_vm)
            if [[ "$guest_type" == "lxc" ]]; then
                pct snapshot "$vmid" "auto-$(date +%Y%m%d%H%M)" --description "client-sim"
            else
                qm snapshot "$vmid" "auto-$(date +%Y%m%d%H%M)" --description "client-sim"
            fi
            ;;
        reclone_vm)
            if [[ "$guest_type" == "lxc" ]]; then
                clone_lxc_instance "$vmid" "$_source_vmid"
            else
                reclone_vm_instance "$vmid"
            fi
            ;;
        delete_vm|delete-vm)
            if [[ "$guest_type" == "lxc" ]]; then
                destroy_lxc "$vmid"
            else
                load_state_file
                # force=1: hub-initiated deletes use immediate force-stop (simulation VMs only).
                destroy_vm "$vmid" "" "1" "1"
            fi
            ;;
        reclone_vms|reseed)
            run_reseed_command
            ;;
        provision_unassigned)
            log "provision_unassigned: clearing all bus exclusions and running provision loop"
            # scan_usb_devices must run before checking PRESENT_BUSES; simplest correct
            # approach is to unconditionally clear all exclusions — that is the intent.
            scan_usb_devices
            load_excluded_buses
            local _excl_count="${#STATE_EXCLUDED_BUS[@]}"
            STATE_EXCLUDED_BUS=()
            save_excluded_buses
            log "provision_unassigned: cleared ${_excl_count} bus exclusion(s)"
            usb_provision_loop || log "WARNING: provision_unassigned loop failed"
            ;;
        clear_usb_exclusions|clear-usb-exclusions)
            log "clear_usb_exclusions: clearing all bus exclusions and running provision loop"
            scan_usb_devices
            load_excluded_buses
            local _excl_count="${#STATE_EXCLUDED_BUS[@]}"
            STATE_EXCLUDED_BUS=()
            save_excluded_buses
            log "clear_usb_exclusions: cleared ${_excl_count} bus exclusion(s)"
            usb_provision_loop || log "WARNING: clear_usb_exclusions loop failed"
            ;;
        unlock_template)
            local _unlock_failed=0
            local -A _unlocked_templates=()
            local _template_id
            for _template_id in "$IMAGE1_TEMPLATE_ID" "$IMAGE2_TEMPLATE_ID"; do
                [[ -n "${_template_id:-}" ]] || continue
                [[ -n "${_unlocked_templates[$_template_id]:-}" ]] && continue
                _unlocked_templates["$_template_id"]=1
                if qm unlock "$_template_id" >>"$AGENT_LOG" 2>&1; then
                    log "unlock_template: qm unlock $_template_id succeeded"
                else
                    log "ERROR: unlock_template: qm unlock $_template_id failed"
                    _unlock_failed=1
                fi
            done
            if probe_template_lock_status >/dev/null 2>&1; then
                log "WARNING: unlock_template: one or more template locks remain"
            else
                log "unlock_template: cleared template lock status"
            fi
            [[ $_unlock_failed -eq 0 ]]
            ;;
        snapshot_vms)
            for vid in $(qm list | awk 'NR>1{print $1}'); do
                qm snapshot "$vid" "auto-$(date +%Y%m%d%H%M)" --description "client-sim" || true
            done
            ;;
        start_vms)  for vid in $(qm list | awk 'NR>1{print $1}'); do timeout 60 qm start "$vid" || true; done ;;
        stop_vms)   for vid in $(qm list | awk 'NR>1{print $1}'); do timeout 60 qm stop  "$vid" || true; done ;;
        update_agent|update-agent|proxmox_agent_update|proxmox-agent-update)
            self_update_agent "$_branch" "$_repo_raw"
            ;;
        restart_agent)
            log "restart_agent: scheduling immediate agent service restart"
            schedule_agent_restart
            ;;
        clear_provision_lock)
            log "clear_provision_lock: killing stuck qm processes and clearing provision flock"
            # Kill any hung qm clone/list processes so Proxmox locks are freed
            local _killed=0
            while IFS= read -r _qpid; do
                [[ -n "$_qpid" ]] || continue
                log "  Sending SIGTERM to stuck qm process PID $_qpid"
                kill -TERM "$_qpid" 2>/dev/null && (( _killed++ )) || true
            done < <(pgrep -f '^qm (clone|list)' 2>/dev/null || true)
            (( _killed > 0 )) && { sleep 3; pgrep -f '^qm (clone|list)' 2>/dev/null | xargs -r kill -KILL 2>/dev/null || true; }
            # Unlock any VMs stuck in locked state
            qm list 2>/dev/null | awk 'NR>1 && $3=="locked" {print $1}' | while IFS= read -r _lvm; do
                log "  Unlocking stuck VM $_lvm"
                qm unlock "$_lvm" 2>/dev/null || true
            done
            # Remove the flock file so the next usb_provision_loop call succeeds
            rm -f "$USB_PROVISION_LOCK_FILE" 2>/dev/null || true
            rm -f "$PROVISION_HALT_CACHE"    2>/dev/null || true
            # Signal the main loop to reset the fail-streak cooldown on its next iteration.
            # The main loop cannot be modified directly from this subshell context.
            touch "$PROVISION_COOLDOWN_RESET_FILE" 2>/dev/null || true
            log "Provision lock cleared; provision loop will resume on next main loop iteration"
            ;;
        clear_usb_quarantine|clear-usb-quarantine)
            local _q_target_bus="${args_bus_path:-${args_bus:-}}"
            refresh_usb_config
            scan_usb_devices
            load_usb_quarantine
            clear_usb_quarantine_state "${_q_target_bus:-}"
            build_usb_state_json
            ;;
        update_spoke|update-spoke)
            # Ask the spoke to self-update by calling its HTTP endpoint directly.
            # SERVER_URL already points at the spoke (e.g. http://192.168.x.x:8080).
            log "update_spoke: triggering spoke self-update via ${SERVER_URL}/api/self-update"
            local _resp _http
            _resp=$(curl -sS -o /tmp/_spoke_upd.json -w "%{http_code}" \
                --max-time 15 -X POST "${SERVER_URL}/api/self-update" 2>/dev/null || true)
            _http="${_resp:-000}"
            if [[ "$_http" == "200" ]]; then
                log "update_spoke: spoke accepted self-update request"
            else
                log "WARNING: update_spoke: spoke returned HTTP ${_http}"
                return 1
            fi
            ;;
        *)          return 1 ;;
    esac
}

process_single_ws_command() {
    local raw="${1:-{}}"
    local parsed cmd_id action vmid guest_type source_vmid branch repo_raw cmd_type status message
    parsed=$(python3 - "$raw" <<'PY' 2>/dev/null || true
import json, sys
raw = sys.argv[1] if len(sys.argv) > 1 else '{}'
try:
    cmd = json.loads(raw)
except Exception:
    cmd = {}
# Support both direct commands (action/args at top level) and queued commands
# (action/args nested inside payload by the hub command relay).
payload = cmd.get('payload', {}) if isinstance(cmd.get('payload', {}), dict) else {}
action_val = cmd.get('action', '') or payload.get('action', '')
args_raw = cmd.get('args', {}) if isinstance(cmd.get('args', {}), dict) else {}
args = args_raw or (payload.get('args', {}) if isinstance(payload.get('args', {}), dict) else {})
print(
    str(cmd.get('id', '')).replace('\t', ' '),
    str(action_val).replace('\t', ' ').replace('-', '_'),
    str(args.get('vmid', '')),
    str(args.get('type') or args.get('vm_type') or '').replace('\t', ' '),
    str(args.get('source_vmid', '')),
    str(args.get('branch', '')).replace('\t', ' '),
    str(args.get('repo_raw', '')).replace('\t', ' '),
    str(cmd.get('type', '')).replace('\t', ' ').replace('-', '_'),
    str(args.get('bus_path', '')).replace('\t', ' '),
    sep='\t'
)
PY
)
    IFS=$'\t' read -r cmd_id action vmid guest_type source_vmid branch repo_raw cmd_type args_bus_path <<< "$parsed"
    if [[ -z "$cmd_id" || -z "$action" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [CMD] SKIP: empty cmd_id or action — raw=${raw:0:200}" >> "$AGENT_LOG"
        return 0
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [CMD] RECV: action=$action vmid=$vmid type=${guest_type:-$cmd_type} id=$cmd_id" >> "$AGENT_LOG"
    status="completed"
    message="${action} completed"
    if ! execute_vm_command "$action" "$vmid" "${guest_type:-$cmd_type}" "$source_vmid" "$branch" "$repo_raw" "$args_bus_path" 2>>"$AGENT_LOG"; then
        status="failed"
        message="${action} failed — check $AGENT_LOG"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [CMD] DONE: action=$action vmid=$vmid status=$status id=$cmd_id" >> "$AGENT_LOG"
    ack_inbox_command "$cmd_id" "$status" "$message" || true
}

if [[ "${1:-}" == "--collect-telemetry" ]]; then
    collect_telemetry 2>/dev/null || true
    exit 0
fi
if [[ "${1:-}" == "--process-single-command" ]]; then
    process_single_ws_command "${2:-{}}"
    exit 0
fi
if [[ "${1:-}" == "--process-backup-command" ]]; then
    process_backup_ws_command "${2:-{}}"
    exit 0
fi
if [[ "${1:-}" == "--process-reseed-command" ]]; then
    process_reseed_ws_command "${2:-{}}"
    exit 0
fi

mkdir -p /var/lib/client-sim
# Reset the log offset on startup so the first telemetry payload ships the last
# 100 lines.  Do this here (in the main bash process) rather than in the WS
# client on every reconnect — moving it here prevents duplicate log delivery
# whenever the WebSocket drops and reconnects mid-session.
rm -f "$AGENT_LOG_OFFSET_FILE" 2>/dev/null || true
# Clean up any stale reseed lock from a previous crash
rm -f "$RESEED_LOCK_FILE"
write_reclone_state_cache idle "[]"
log "Proxmox agent v${AGENT_VERSION} starting. Server: $SERVER_URL"
_LAST_SELF_UPDATE=0
_PROV_FAIL_STREAK=0
_PROV_COOLDOWN_UNTIL=0
# Clean up stale provisioning flag files from any previous run.
# These are /tmp files that survive service restarts; without cleanup they
# make VMs appear permanently stuck in "provisioning" status.
# Preserve durable state files that must survive restarts:
#   .post_prov_retry  — tracks VMs waiting for retry/reclone after missed update.sh
#   .provision_done   — marks provisioning completion (watchdog grace period anchor)
#   .agent_unresponsive — tracks watchdog escalation state (reboot/reclone timing)
for _prov_f in "${PROV_DIR}"/*; do
    [[ "$_prov_f" == *.post_prov_retry ]] && continue
    [[ "$_prov_f" == *.provision_done ]] && continue
    [[ "$_prov_f" == *.agent_unresponsive ]] && continue
    rm -f "$_prov_f" 2>/dev/null || true
done
unset _prov_f

# Startup: clear provision lock and kill orphaned qm clone/list processes from the
# previous agent session.  When the agent is restarted by systemd, background qm clone
# subprocesses may survive as orphans (parent was killed but the Proxmox command ran
# via a detached subshell).  These orphans hold Proxmox task locks, causing qm list to
# hang in the new provision loop.  Killing them here unblocks the next provision cycle.
_orphan_killed=0
while IFS= read -r _orphan_pid; do
    [[ -n "$_orphan_pid" ]] || continue
    log "Startup: killing orphaned qm process PID $_orphan_pid"
    kill -KILL "$_orphan_pid" 2>/dev/null && (( _orphan_killed++ )) || true
done < <(ps aux 2>/dev/null | awk '/[q]m (clone|list)/{print $2}' || true)
(( _orphan_killed > 0 )) && { log "Startup: killed $_orphan_killed orphaned qm process(es); waiting for Proxmox to release locks"; sleep 3; }
# Flock is released when the old agent dies (fd closed), but the file may still exist.
rm -f "$USB_PROVISION_LOCK_FILE" 2>/dev/null || true
# Clear any stale cooldown-reset sentinel from a previous run (cooldown resets to 0 on startup anyway).
rm -f "$PROVISION_COOLDOWN_RESET_FILE" 2>/dev/null || true
unset _orphan_killed _orphan_pid

# Startup: clean up stale provision sentinel files left by a previous agent run.
# These plain ${PROV_DIR}/${vmid} files mark a USB bus as prov_status="provisioning".
# If the agent was killed mid-clone (e.g. during an update restart), the cleanup in
# clone_vm_for_usb never ran → the file persists → prov_run.running stays True → the
# spoke stops sending provision_unassigned → provisioning stalls indefinitely.
# A file is stale when its age exceeds CLONE_TIMEOUT_SECONDS (the maximum clone window).
_stale_prov_found=0
for _prov_file in "${PROV_DIR}"/[0-9]*; do
    [[ -f "$_prov_file" ]] || continue
    _fname="${_prov_file##*/}"
    [[ "$_fname" == *"."* ]] && continue   # skip .provision_done, .post_prov_retry, etc.
    _file_age=$(( $(date +%s) - $(stat -c %Y "$_prov_file" 2>/dev/null || echo 0) ))
    if (( _file_age > CLONE_TIMEOUT_SECONDS )); then
        log "Startup: removing stale provision sentinel ${_prov_file} (age=${_file_age}s > ${CLONE_TIMEOUT_SECONDS}s timeout)"
        rm -f "$_prov_file" 2>/dev/null || true
        (( _stale_prov_found++ )) || true
    fi
done
(( _stale_prov_found > 0 )) && log "Startup: removed ${_stale_prov_found} stale provision sentinel(s); provisioning will resume on next cycle"
unset _stale_prov_found _prov_file _fname _file_age
if [[ -z "$API_KEY" ]]; then
    register_and_wait_for_key
fi
ensure_state_file
refresh_usb_telemetry_only || true
blacklist_dongle_drivers || true
log "Host block $host_id → VM range $start_vmid-$end_vmid (max_slots=$MAX_USB_SLOTS)"

# Helper: collect and POST telemetry immediately
post_telemetry() {
    local telem response status body
    telem=$(collect_telemetry 2>/dev/null) || { log "WARNING: collect_telemetry failed (non-zero exit)"; return 0; }
    if [[ -z "$telem" ]]; then
        log "WARNING: collect_telemetry returned empty payload — skipping POST"
        return 0
    fi
    response=$(curl_api_status POST /api/proxmox/telemetry "$telem" 2>/dev/null || true)
    status="${response##*$'\n'}"
    body="${response%$'\n'*}"
    case "$status" in
        200) return 0 ;;
        202|401|403)
            handle_auth_failure "$status" "/api/proxmox/telemetry"
            return 0
            ;;
        "") log "WARNING: telemetry POST failed (curl error or no response)"; return 0 ;;
        *)
            log "WARNING: telemetry POST returned HTTP ${status} ${body:+body=${body:0:160}}"
            return 0
            ;;
    esac
}

# Helper: push recent log lines to spoke via HTTP (fallback when WS is unavailable).
# Uses the same offset file as collect_log_lines so lines aren't double-sent.
push_logs_http() {
    [[ -f "$AGENT_LOG" ]] || return 0
    local log_lines_json payload response status
    log_lines_json=$(collect_log_lines 2>/dev/null) || return 0
    [[ "$log_lines_json" == "[]" ]] && return 0
    payload=$(python3 -c "
import json, sys
lines = json.loads(sys.argv[1])
print(json.dumps({'hostname': sys.argv[2], 'log_lines': lines}))
" "$log_lines_json" "$(hostname 2>/dev/null || echo unknown)" 2>/dev/null) || return 0
    response=$(curl_api_status POST /api/proxmox/log-push "$payload" 2>/dev/null || true)
    status="${response##*$'\n'}"
    case "$status" in
        200) return 0 ;;
        202|401|403) handle_auth_failure "$status" "/api/proxmox/log-push" ;;
        "") log "WARNING: log-push POST failed (curl error or no response)" ;;
        *) log "WARNING: log-push POST returned HTTP ${status}" ;;
    esac
}

# ── Inbox command processor ────────────────────────────────────────────────────
# Runs in its own background loop every INBOX_INTERVAL seconds, fully decoupled
# from the main USB provisioning loop. Reclone wait+ACK is itself backgrounded
# so process_inbox always returns immediately — never blocked by clone operations.
start_proxmox_ws_client() {
    local poll_hostname script_path
    poll_hostname=$(hostname 2>/dev/null || printf '%s' "$h")
    script_path=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")
    python3 - "$script_path" "$SERVER_URL" "$API_KEY" "$poll_hostname" "$TELEMETRY_INTERVAL" "$PROGRESS_EVENT_QUEUE_DIR" "$HUB_SERVER_URL_FILE" "$HUB_LAST_SUCCESS_FILE" "$ENV_FILE" <<'PY' &
import asyncio, contextlib, json, sys, time
from pathlib import Path
script_path, default_server_url, api_key, hostname, telemetry_interval, progress_queue_dir, server_url_file, last_success_file = sys.argv[1:9]
env_file = sys.argv[9] if len(sys.argv) > 9 else ""
telemetry_interval = max(1, int(float(telemetry_interval or 3)))
queue_dir = Path(progress_queue_dir)
queue_dir.mkdir(parents=True, exist_ok=True)
server_url_path = Path(server_url_file)
last_success_path = Path(last_success_file)
try:
    import websockets
except ImportError:
    sys.exit(1)
def load_server_url():
    try:
        raw = server_url_path.read_text(encoding='utf-8').strip()
        if raw:
            return raw
    except Exception:
        pass
    return default_server_url
def load_api_key():
    """Re-read the API key from ENV_FILE on each WS reconnect.
    If the key was rotated by inbox auth failure + re-registration in the bash
    process, this ensures the WS client picks up the new key automatically
    without needing an explicit process restart."""
    if env_file:
        try:
            for line in Path(env_file).read_text(encoding='utf-8').splitlines():
                if line.startswith('CLIENT_SIM_API_KEY='):
                    key = line[len('CLIENT_SIM_API_KEY='):].strip()
                    if key:
                        return key
        except Exception:
            pass
    return api_key
def build_ws_url(server_url, current_key=None):
    ws_url = server_url.rstrip('/').replace('https://', 'wss://').replace('http://', 'ws://')
    return ws_url + f"/ws/proxmox?hostname={hostname}&api_key={current_key or api_key}"
def touch_success():
    try:
        last_success_path.parent.mkdir(parents=True, exist_ok=True)
        last_success_path.write_text(str(int(time.time())), encoding='utf-8')
    except Exception:
        pass
async def collect_telemetry():
    proc = await asyncio.create_subprocess_exec(
        'bash', script_path, '--collect-telemetry',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        print("[WARN] collect_telemetry timed out after 25s", file=sys.stderr)
        return None
    raw = stdout.decode().strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        print(f"[WARN] Malformed payload (truncated): {raw[:200]}", file=sys.stderr)
        return None
AGENT_LOG_FILE = '/var/log/client-sim-proxmox-agent.log'
def _open_agent_log():
    try:
        return open(AGENT_LOG_FILE, 'a', buffering=1)
    except Exception:
        return None
async def run_command(command):
    log_fh = _open_agent_log()
    try:
        proc = await asyncio.create_subprocess_exec(
            'bash', script_path, '--process-single-command', json.dumps(command),
            stdout=log_fh or asyncio.subprocess.DEVNULL,
            stderr=log_fh or asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    finally:
        if log_fh:
            log_fh.close()
async def run_command_bg(flag, command):
    log_fh = _open_agent_log()
    proc = await asyncio.create_subprocess_exec(
        'bash', script_path, flag, command,
        stdout=log_fh or asyncio.subprocess.DEVNULL,
        stderr=log_fh or asyncio.subprocess.DEVNULL,
    )
    async def _wait_and_close():
        await proc.wait()
        if log_fh:
            log_fh.close()
    asyncio.create_task(_wait_and_close())
async def handle_create_proxmox_token(ws, request_id):
    import shutil as _shutil
    TOKEN_ID = 'cs-hub'
    USER = 'root@pam'
    pvesh = _shutil.which('pvesh') or '/usr/bin/pvesh'
    async def send_result(msg):
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            pass
    try:
        del_proc = await asyncio.create_subprocess_exec(
            pvesh, 'delete', f'/access/users/{USER}/token/{TOKEN_ID}',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10)
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh, 'create', f'/access/users/{USER}/token/{TOKEN_ID}',
            '--privsep', '0', '--output-format', 'json',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            await send_result({'type': 'token_provision_error', 'request_id': request_id, 'error': f'pvesh failed: {stderr.decode().strip()[:300]}'})
            return
        data = json.loads(stdout.decode().strip())
        secret = str(data.get('value') or '').strip()
        if not secret:
            await send_result({'type': 'token_provision_error', 'request_id': request_id, 'error': 'pvesh returned no token value'})
            return
        await send_result({'type': 'token_provisioned', 'request_id': request_id, 'token': f'{USER}!{TOKEN_ID}={secret}'})
    except asyncio.TimeoutError:
        await send_result({'type': 'token_provision_error', 'request_id': request_id, 'error': 'pvesh timed out'})
    except Exception as exc:
        await send_result({'type': 'token_provision_error', 'request_id': request_id, 'error': str(exc)})
async def send_progress_events(ws):
    for event_file in sorted(queue_dir.glob('*.json')):
        try:
            raw = event_file.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            continue
        except Exception as exc:
            print(f"[WARN] Failed reading progress event {event_file.name}: {exc}", file=sys.stderr)
            continue
        if not raw:
            with contextlib.suppress(FileNotFoundError):
                event_file.unlink()
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            print(f"[WARN] Malformed progress event (truncated): {raw[:200]}", file=sys.stderr)
            with contextlib.suppress(FileNotFoundError):
                event_file.unlink()
            continue
        await ws.send(json.dumps(payload))
        touch_success()
        with contextlib.suppress(FileNotFoundError):
            event_file.unlink()
async def send_loop(ws):
    while True:
        await send_progress_events(ws)
        payload = await collect_telemetry()
        if payload is not None:
            await ws.send(json.dumps({'type': 'telemetry', 'payload': payload}))
            touch_success()
        else:
            # Telemetry collection timed out (e.g. pvesh blocked during reclone).
            # Send a minimal heartbeat so the server keeps last_seen fresh.
            try:
                await ws.send(json.dumps({'type': 'ping'}))
                touch_success()
            except Exception:
                pass
        await send_progress_events(ws)
        await asyncio.sleep(telemetry_interval)
async def main():
    backoff = 1
    while True:
        try:
            ws_url = build_ws_url(load_server_url(), load_api_key())
            try:
                import datetime as _dt
                with open(AGENT_LOG_FILE, 'a') as _lf:
                    _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] CONNECTING to {ws_url.split('?')[0]}\n")
            except Exception:
                pass
            async with websockets.connect(ws_url, ping_interval=120, ping_timeout=120) as ws:
                backoff = 1
                try:
                    import datetime as _dt
                    with open(AGENT_LOG_FILE, 'a') as _lf:
                        _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] CONNECTED\n")
                except Exception:
                    pass
                # Do NOT send an initial 'sync' here — the spoke already pushes pending
                # commands immediately on WS connect via ws_proxmox_endpoint.  Sending
                # sync would cause the spoke to reset and re-push commands a second time,
                # spawning duplicate delete subprocesses.
                touch_success()
                sender = asyncio.create_task(send_loop(ws))
                try:
                    async for message in ws:
                        touch_success()
                        try:
                            payload = json.loads(message)
                        except Exception:
                            print(f"[WARN] Malformed payload (truncated): {message[:200]}", file=sys.stderr)
                            continue
                        msg_type = str(payload.get('type') or '').lower()
                        if msg_type == 'commands':
                            cmds = payload.get('commands') or []
                            try:
                                import datetime as _dt
                                with open(AGENT_LOG_FILE, 'a') as _lf:
                                    _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] BATCH received: {len(cmds)} command(s): {[c.get('action') for c in cmds]}\n")
                            except Exception:
                                pass
                            for command in cmds:
                                action_name = str(command.get('action') or '')
                                if action_name == 'delete_vm':
                                    def _make_delete_task(cmd=command):
                                        t = asyncio.create_task(run_command_bg('--process-single-command', json.dumps(cmd)))
                                        def _on_done(task):
                                            exc = task.exception() if not task.cancelled() else None
                                            if exc:
                                                try:
                                                    import datetime as _dt2
                                                    with open(AGENT_LOG_FILE, 'a') as _lf2:
                                                        _lf2.write(f"[{_dt2.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CMD] ERROR: delete_vm task failed: {exc}\n")
                                                except Exception:
                                                    pass
                                        t.add_done_callback(_on_done)
                                        return t
                                    _make_delete_task()
                                else:
                                    await run_command(command)
                        elif msg_type == 'command':
                            # delete_vm is long-running (stop+destroy) — run as a background
                            # task so multiple deletes can execute in parallel without each
                            # one blocking the WS receive loop for the next command.
                            action = str(payload.get('action') or '').replace('-', '_')
                            try:
                                import datetime as _dt
                                with open(AGENT_LOG_FILE, 'a') as _lf:
                                    _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] SINGLE received: action={action}\n")
                            except Exception:
                                pass
                            if action == 'delete_vm':
                                def _make_single_delete_task(p=payload):
                                    t = asyncio.create_task(run_command_bg('--process-single-command', json.dumps(p)))
                                    def _on_done(task):
                                        exc = task.exception() if not task.cancelled() else None
                                        if exc:
                                            try:
                                                import datetime as _dt2
                                                with open(AGENT_LOG_FILE, 'a') as _lf2:
                                                    _lf2.write(f"[{_dt2.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CMD] ERROR: delete_vm task failed: {exc}\n")
                                            except Exception:
                                                pass
                                    t.add_done_callback(_on_done)
                                    return t
                                _make_single_delete_task()
                            else:
                                await run_command(payload)
                        elif msg_type == 'backup':
                            asyncio.create_task(run_command_bg('--process-backup-command', json.dumps(payload)))
                        elif msg_type == 'reseed':
                            asyncio.create_task(run_command_bg('--process-reseed-command', json.dumps(payload)))
                        elif msg_type == 'create_proxmox_token':
                            req_id = str(payload.get('request_id') or '')
                            asyncio.create_task(handle_create_proxmox_token(ws, req_id))
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
                    try:
                        import datetime as _dt
                        with open(AGENT_LOG_FILE, 'a') as _lf:
                            _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] DISCONNECTED\n")
                    except Exception:
                        pass
        except Exception as _exc:
            try:
                import datetime as _dt
                with open(AGENT_LOG_FILE, 'a') as _lf:
                    _lf.write(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WS] ERROR: {_exc!r} — retry in {min(backoff, 30)}s\n")
            except Exception:
                pass
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
asyncio.run(main())
PY
    WS_PID=$!
    log "Proxmox WebSocket client started (PID $WS_PID)"
}

USE_PROXMOX_WS=0
if python3 -c "import websockets" >/dev/null 2>&1; then
    start_proxmox_ws_client || true
    USE_PROXMOX_WS=1
fi

if [[ "$USE_PROXMOX_WS" -ne 1 ]]; then
    # Background real-time telemetry sender (every TELEMETRY_INTERVAL seconds)
    # Runs as a subprocess — reads node/VM stats fresh and USB state from cache files
    (
        while true; do
            sleep "$TELEMETRY_INTERVAL"
            post_telemetry || true
        done
    ) &
    TELEMETRY_PID=$!
    log "Background telemetry sender started (PID $TELEMETRY_PID, interval ${TELEMETRY_INTERVAL}s)"

    post_telemetry || true
fi

process_inbox() {
    local response_with_status response status poll_hostname
    local -a args
    poll_hostname=$(hostname 2>/dev/null || printf '%s' "$h")
    refresh_runtime_server_url
    args=(-sS --max-time 15 -G "${SERVER_URL}/api/inbox" --data-urlencode "hostname=${poll_hostname}" -w $'\n%{http_code}')
    [[ -n "$API_KEY" ]] && args+=(-H "X-API-Key: $API_KEY")
    response_with_status=$(curl "${args[@]}" 2>/dev/null || true)
    status="${response_with_status##*$'\n'}"
    response="${response_with_status%$'\n'*}"
    record_hub_contact_result "$status"
    case "$status" in
        200) ;;
        202|401|403)
            handle_auth_failure "$status" "/api/inbox"
            return 1  # Signal auth recovery so loop retries quickly with new key
            ;;
        "") return 0 ;;
        *)
            log "WARNING: inbox poll returned HTTP ${status}"
            return 0
            ;;
    esac
    [[ -z "$response" || "$response" == "[]" ]] && return 0

    log "Commands received: $response"
    local parsed_commands
    parsed_commands=$(python3 - "$response" <<'PY' 2>>"$AGENT_LOG" || true
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else '[]'
try:
    commands = json.loads(raw)
except Exception:
    commands = []
for cmd in commands:
    cid = str(cmd.get('id', '')).replace('\t', ' ')
    action = str(cmd.get('action', '')).replace('\t', ' ').replace('-', '_')
    vmid = cmd.get('args', {}).get('vmid', '')
    guest_type = str(cmd.get('args', {}).get('type') or cmd.get('args', {}).get('vm_type') or '').replace('\t', ' ')
    source_vmid = cmd.get('args', {}).get('source_vmid', '')
    branch = str(cmd.get('args', {}).get('branch') or '').replace('\t', ' ')
    repo_raw = str(cmd.get('args', {}).get('repo_raw') or '').replace('\t', ' ')
    ctype = str(cmd.get('type') or '').replace('\t', ' ').replace('-', '_')
    print(f"{cid}\t{action}\t{vmid}\t{guest_type}\t{source_vmid}\t{branch}\t{repo_raw}\t{ctype}")
PY
)
    local _seq_ids=() _seq_actions=() _seq_vmids=() _seq_types=() _seq_sources=() _seq_branches=() _seq_repo_raws=()
    local _rc_ids=() _rc_vmids=() _rc_types=() _rc_sources=()
    local _del_ids=() _del_vmids=()
    while IFS=$'\t' read -r cmd_id action vmid guest_type source_vmid branch repo_raw cmd_type; do
        [[ -z "$cmd_id" || -z "$action" ]] && continue
        if [[ "$action" == "reclone_vm" && -n "$vmid" ]]; then
            _rc_ids+=("$cmd_id")
            _rc_vmids+=("$vmid")
            _rc_types+=("${guest_type:-qemu}")
            _rc_sources+=("$source_vmid")
        elif [[ "$action" == "delete_vm" && -n "$vmid" ]]; then
            _del_ids+=("$cmd_id")
            _del_vmids+=("$vmid")
        else
            _seq_ids+=("$cmd_id")
            _seq_actions+=("$action")
            _seq_vmids+=("$vmid")
            _seq_types+=("${guest_type:-$cmd_type}")
            _seq_sources+=("$source_vmid")
            _seq_branches+=("$branch")
            _seq_repo_raws+=("$repo_raw")
        fi
    done <<< "$parsed_commands"

    for _si in "${!_seq_ids[@]}"; do
        log "Executing ${_seq_actions[$_si]} (vmid=${_seq_vmids[$_si]:-})"
        local status="completed" message=""
        if execute_vm_command "${_seq_actions[$_si]}" "${_seq_vmids[$_si]}" "${_seq_types[$_si]}" "${_seq_sources[$_si]}" "${_seq_branches[$_si]}" "${_seq_repo_raws[$_si]}" 2>>"$AGENT_LOG"; then
            message="${_seq_actions[$_si]} completed"
        else
            status="failed"
            message="${_seq_actions[$_si]} failed — check $AGENT_LOG"
        fi
        ack_inbox_command "${_seq_ids[$_si]}" "$status" "$message" || true
        post_telemetry
    done

    if [[ ${#_rc_vmids[@]} -gt 0 ]]; then
        load_state_file
        local _rc_active_pids=() _rc_pids=() _rc_batch_ids=() _rc_batch_vmids=() _rc_batch_buses=()
        local _conc="${RECLONE_CONCURRENCY:-1}"

        for _ri in "${!_rc_vmids[@]}"; do
            local _vmid="${_rc_vmids[$_ri]}" _cmd_id="${_rc_ids[$_ri]}" _guest_type="${_rc_types[$_ri]:-qemu}" _source_vmid="${_rc_sources[$_ri]:-}" _bus=""
            if [[ "$_guest_type" == "lxc" ]]; then
                if [[ -z "$_source_vmid" ]]; then
                    log "WARNING: No LXC template/source configured for CT $_vmid"
                    ack_inbox_command "$_cmd_id" "failed" "No LXC template/source configured for CT $_vmid" || true
                    continue
                fi
            else
                local _bus="${STATE_VMID_TO_BUS[$_vmid]:-}"
                if [[ -z "$_bus" ]]; then
                    local _usb_line
                    _usb_line=$(qm config "$_vmid" 2>/dev/null | grep -m1 '^usb[0-9]*: ' || true)
                    if [[ "$_usb_line" =~ host=([^,[:space:]]+) ]]; then
                        _bus="${BASH_REMATCH[1]}"
                        log "Recovered USB bus_path=$_bus for VM $_vmid from qm config"
                        STATE_VMID_TO_BUS["$_vmid"]="$_bus"
                        STATE_BUS_TO_VMID["$_bus"]="$_vmid"
                    fi
                fi
                local _vidpid="${USB_VIDPID_BY_BUS[$_bus]:-}"
                local _stored_vidpid="${STATE_VIDPID_BY_BUS[$_bus]:-$_vidpid}"
                local _product="${USB_NAME_BY_BUS[$_bus]:-$(find_label_for_vidpid "$_vidpid")}"
                local _image="${STATE_VMID_TO_IMAGE[$_vmid]:-1}"
                local _dtype="${CERTIFIED_TYPES[$_vidpid]:-wireless}"
                local _stored_type="${CERTIFIED_TYPES[$_stored_vidpid]:-wireless}"

                if [[ -z "$_bus" || ! -d "/sys/bus/usb/devices/$_bus" ]]; then
                    log "WARNING: USB device ${_bus:-<unknown>} is not present; cannot reclone VM $_vmid"
                    ack_inbox_command "$_cmd_id" "failed" "USB device not present for VM $_vmid" || true
                    continue
                fi
                if [[ "$_stored_type" != "$_dtype" ]] && ! is_truthy "$USE_ALL_DONGLES"; then
                    log "WARNING: VM $_vmid current type=$_dtype does not match stored type=$_stored_type — skipping reclone"
                    ack_inbox_command "$_cmd_id" "failed" "device type mismatch: VM $_vmid stored=$_stored_type current=$_dtype" || true
                    continue
                fi
                if [[ "$SIM_PHY" != "any" ]] && ! is_truthy "$USE_ALL_DONGLES" && ! sim_phy_accepts_type "$_stored_type"; then
                    log "WARNING: VM $_vmid stored type=$_stored_type is not allowed by sim_phy=$SIM_PHY use_all_dongles=$USE_ALL_DONGLES — skipping reclone"
                    ack_inbox_command "$_cmd_id" "failed" "sim_phy mismatch: VM $_vmid stored=$_stored_type sim_phy=$SIM_PHY (use_all_dongles=$USE_ALL_DONGLES)" || true
                    continue
                fi
            fi

            while [[ ${#_rc_active_pids[@]} -ge $_conc ]]; do
                local _live=()
                for _p in "${_rc_active_pids[@]}"; do
                    kill -0 "$_p" 2>/dev/null && _live+=("$_p")
                done
                _rc_active_pids=("${_live[@]}")
                [[ ${#_rc_active_pids[@]} -ge $_conc ]] && sleep 5
            done

            _RECLONE_CMD_IDS["$_vmid"]="$_cmd_id"
            local _rj=$(( RANDOM % 31 ))
            if [[ "$_guest_type" == "lxc" ]]; then
                log "Parallel reclone starting: CT $_vmid (source=$_source_vmid, jitter=${_rj}s)"
                local _local_cmd_id="$_cmd_id" _local_vmid="$_vmid" _local_src="$_source_vmid"
                (
                    [[ $_rj -gt 0 ]] && sleep "$_rj"
                    if clone_lxc_instance "$_local_vmid" "$_local_src"; then
                        ack_inbox_command "$_local_cmd_id" "completed" "reclone_vm completed" || true
                        log "ACK reclone: $_local_cmd_id status=completed vmid=$_local_vmid"
                    else
                        ack_inbox_command "$_local_cmd_id" "failed" "reclone_vm failed — check $AGENT_LOG" || true
                        log "ACK reclone: $_local_cmd_id status=failed vmid=$_local_vmid"
                    fi
                ) &
            else
                log "Parallel reclone starting: VM $_vmid (bus=$_bus type=$_dtype image=$_image, jitter=${_rj}s)"
                local _local_cmd_id="$_cmd_id" _local_vmid="$_vmid" _local_bus="$_bus" _local_product="$_product" _local_image="$_image" _local_dtype="$_dtype"
                (
                    [[ $_rj -gt 0 ]] && sleep "$_rj"
                    if _reclone_parallel_job "$_local_vmid" "$_local_bus" "$_local_product" "$_local_image" "$_local_dtype"; then
                        ack_inbox_command "$_local_cmd_id" "completed" "reclone_vm completed" || true
                        log "ACK reclone: $_local_cmd_id status=completed vmid=$_local_vmid"
                    else
                        ack_inbox_command "$_local_cmd_id" "failed" "reclone_vm failed — check $AGENT_LOG" || true
                        log "ACK reclone: $_local_cmd_id status=failed vmid=$_local_vmid"
                    fi
                ) &
            fi
            local _pid=$!
            _rc_active_pids+=("$_pid")
            _rc_pids+=("$_pid")
            _rc_batch_ids+=("$_cmd_id")
            _rc_batch_vmids+=("$_vmid")
            _rc_batch_buses+=("$_bus")
        done

        # Wait for reclone jobs and ACK results in a background subshell so
        # process_inbox returns immediately — never blocked by multi-minute clones.
        local _rc_vmids_json="[]"
        if [[ ${#_rc_batch_vmids[@]} -gt 0 ]]; then
            _rc_vmids_json="[$(IFS=,; echo "${_rc_batch_vmids[*]}")]"
            write_reclone_state_cache running "$_rc_vmids_json"
        fi
        # Background subshell: wait for all reclone jobs to finish (using kill -0 polling
        # since they are sibling PIDs, not children), then update state cache.
        # ACKs are now sent from within each reclone subshell above.
        local _snap_pids=("${_rc_pids[@]}")
        local _snap_vmids=("${_rc_batch_vmids[@]}")
        (
            for _p in "${_snap_pids[@]}"; do
                while kill -0 "$_p" 2>/dev/null; do sleep 2; done
            done
            # Reload state, clear missing flags for completed reclones, persist
            load_state_file
            for _vmid in "${_snap_vmids[@]}"; do
                local _b="${STATE_VMID_TO_BUS[$_vmid]:-}"
                [[ -n "$_b" ]] && STATE_MISSING_BY_BUS["$_b"]=""
            done
            save_state_file
            write_reclone_state_cache idle "[]"
        ) &
    fi

    # Parallel delete_vm: stop+destroy all selected guests concurrently.
    # Each delete subshell ACKs itself so process_inbox returns immediately —
    # never blocked while waiting for multi-minute VM destroys.
    if [[ ${#_del_vmids[@]} -gt 0 ]]; then
        load_state_file
        load_excluded_buses
        local _del_pids=()
        for _di in "${!_del_vmids[@]}"; do
            local _dvmid="${_del_vmids[$_di]}"
            _expire_vm_pending_commands "$_dvmid"
            # Mark VM as tearing_down immediately so collect_telemetry reflects it
            # before the delete completes, giving the hub real-time status feedback.
            echo "$(date +%s)" > "${PROV_DIR}/${_dvmid}.deleting"
        done
        build_usb_state_json
        # build_usb_state_json updated the USB state cache files; the WS send_loop
        # will pick up tearing_down status on its next collect_telemetry pass (≤3s).
        for _di in "${!_del_vmids[@]}"; do
            local _dvmid="${_del_vmids[$_di]}"
            local _dcmd_id="${_del_ids[$_di]}"
            (
                _del_t0=$(date +%s)
                log "Delete subshell started: vmid=$_dvmid cmd_id=$_dcmd_id"
                if _destroy_guest_only "$_dvmid" "" "1"; then
                    _del_elapsed=$(( $(date +%s) - _del_t0 ))
                    log "Parallel delete done: VMID $_dvmid elapsed=${_del_elapsed}s"
                    ack_inbox_command "$_dcmd_id" "completed" "delete_vm completed" || \
                        log "ERROR: ACK failed for $_dcmd_id (completed)"
                else
                    _del_elapsed=$(( $(date +%s) - _del_t0 ))
                    log "Parallel delete failed: VMID $_dvmid elapsed=${_del_elapsed}s"
                    ack_inbox_command "$_dcmd_id" "failed" "delete_vm failed — check $AGENT_LOG" || \
                        log "ERROR: ACK failed for $_dcmd_id (failed)"
                fi
                rm -f "${PROV_DIR}/${_dvmid}.deleting" 2>/dev/null || true
            ) &
            _del_pids+=($!)
        done
        # State cleanup and telemetry run in background once all deletes finish —
        # this unblocks process_inbox so the inbox loop keeps polling on schedule.
        local _snap_del_pids=("${_del_pids[@]}")
        local _snap_del_vmids=("${_del_vmids[@]}")
        (
            for _p in "${_snap_del_pids[@]}"; do
                while kill -0 "$_p" 2>/dev/null; do sleep 2; done
            done
            load_state_file
            for _dvmid in "${_snap_del_vmids[@]}"; do
                local _dbus="${STATE_VMID_TO_BUS[$_dvmid]:-}"
                if [[ -n "$_dbus" ]]; then
                    unset "STATE_MISSING_BY_BUS[$_dbus]"
                    unset "STATE_BUS_TO_VMID[$_dbus]"
                    unset "STATE_VIDPID_BY_BUS[$_dbus]"
                    # Exclude bus from auto-provisioning so the VM isn't immediately recreated.
                    STATE_EXCLUDED_BUS["$_dbus"]="1"
                    log "Bus $_dbus excluded from auto-provisioning after hub-initiated delete of VM $_dvmid"
                fi
                unset "STATE_VMID_TO_BUS[$_dvmid]"
                unset "STATE_VMID_TO_IMAGE[$_dvmid]"
            done
            save_state_file
            save_excluded_buses
            build_usb_state_json  # rebuild cache so post_telemetry doesn't report stale VMs
            post_telemetry
        ) &
    fi
}

# Launch inbox as an independent background loop.
# When WS is active it acts as a fallback: the spoke resets stale "delivered"
# commands (> 30 s un-acked) back to "pending" so they can be picked up here.
# When WS is unavailable it is the primary command path.
_inbox_poll_interval="$INBOX_INTERVAL"
if [[ "$USE_PROXMOX_WS" -eq 1 ]]; then
    # Longer interval when WS is the primary path — only needed as a fallback.
    _inbox_poll_interval=60
fi
(
    while true; do
        if ! process_inbox; then
            # Auth recovery just completed (new key saved); retry quickly so
            # commands are not delayed by the full poll interval.
            sleep 5
            process_inbox || true
        fi
        sleep "$_inbox_poll_interval"
    done
) &
INBOX_PID=$!
log "Background inbox poller started (PID $INBOX_PID, interval ${_inbox_poll_interval}s, ws_mode=$USE_PROXMOX_WS)"

# Start hardware watchdog in background
if [[ "$HW_WATCHDOG_ENABLED" -eq 1 ]]; then
    hw_watchdog_loop &
    HW_WATCHDOG_PID=$!
    log "Hardware watchdog started (PID $HW_WATCHDOG_PID)"
fi

while true; do
    refresh_runtime_server_url
    maybe_redetect_hub_url || true
    check_resource_halt || true
    # Reset provision cooldown if clear_provision_lock wrote the sentinel file.
    # The sentinel is written from a subshell (inbox poller / WS handler) which cannot
    # directly modify _PROV_COOLDOWN_UNTIL in this parent process.
    if [[ -f "$PROVISION_COOLDOWN_RESET_FILE" ]]; then
        rm -f "$PROVISION_COOLDOWN_RESET_FILE"
        _PROV_COOLDOWN_UNTIL=0
        _PROV_FAIL_STREAK=0
        log "Provision cooldown reset via clear_provision_lock signal"
    fi
    if [[ "$AUTO_PROVISION" == "on" ]] && [[ -f "$RESEED_LOCK_FILE" ]]; then
        log "Reseed in progress — skipping auto-provisioning cycle"
        sleep 5
        continue
    fi
    refresh_usb_config || true
    blacklist_dongle_drivers || true
    if [[ "$AUTO_PROVISION" == "on" ]]; then
        _prov_now=$(date +%s)
        if (( _prov_now < _PROV_COOLDOWN_UNTIL )); then
            _remaining=$(( _PROV_COOLDOWN_UNTIL - _prov_now ))
            log "Provision cooldown active (${_remaining}s remaining) — skipping provision cycle"
            # Update the halt cache so the stale 'pacing' reason from the last real run
            # does not persist for the duration of the cooldown.
            printf '{"halted":true,"reason":"cooldown","cooldown_remaining_s":%s,"ts":%s}\n' \
                "$_remaining" "$(date +%s)" > "$PROVISION_HALT_CACHE"
            refresh_usb_telemetry_only || true
        else
            _prov_rc=0
            # Watchdog: auto-clear stale provision lock so a D-state hung qm clone
            # does not block all future provision cycles indefinitely.
            if [[ -f "$USB_PROVISION_LOCK_FILE" ]]; then
                _lock_mtime=$(stat -c %Y "$USB_PROVISION_LOCK_FILE" 2>/dev/null || echo 0)
                _lock_age=$(( $(date +%s) - _lock_mtime ))
                _prov_lock_max_age=$(( ${CLONE_TIMEOUT_SECONDS:-1800} + 300 ))
                if (( _lock_age > _prov_lock_max_age )); then
                    log "WARNING: Provision lock stale (held ${_lock_age}s, max ${_prov_lock_max_age}s) — auto-clearing"
                    while IFS= read -r _qpid; do
                        [[ -n "$_qpid" ]] || continue
                        kill -TERM "$_qpid" 2>/dev/null || true
                    done < <(pgrep -f '^qm (clone|list)' 2>/dev/null || true)
                    sleep 2
                    while IFS= read -r _qpid; do
                        [[ -n "$_qpid" ]] || continue
                        kill -KILL "$_qpid" 2>/dev/null || true
                    done < <(pgrep -f '^qm (clone|list)' 2>/dev/null || true)
                    rm -f "$USB_PROVISION_LOCK_FILE" 2>/dev/null || true
                    rm -f "$PROVISION_HALT_CACHE" 2>/dev/null || true
                    _PROV_COOLDOWN_UNTIL=0
                    _PROV_FAIL_STREAK=0
                    log "Stale provision lock cleared; resuming provision loop"
                fi
            fi
            usb_provision_loop || _prov_rc=$?
            if (( _prov_rc == 2 )); then
                (( _PROV_FAIL_STREAK++ )) || true
                log "WARNING: All provision jobs failed (streak=${_PROV_FAIL_STREAK})"
                if (( _PROV_FAIL_STREAK >= 3 )); then
                    log "WARNING: ${_PROV_FAIL_STREAK} consecutive all-fail cycles — pausing provisioning for 5 minutes"
                    _PROV_COOLDOWN_UNTIL=$(( $(date +%s) + 300 ))
                    _PROV_FAIL_STREAK=0
                fi
            else
                _PROV_FAIL_STREAK=0
            fi
        fi
    else
        refresh_usb_telemetry_only || true
    fi

    # Retry queue: runs whenever auto-provisioning is on, independent of cooldown.
    # Handles VMs that provisioned but didn't come back after the post-hostname reboot.
    if [[ "$AUTO_PROVISION" == "on" ]]; then
        _run_post_prov_retry_queue || true
    fi

    # VM guest agent watchdog: runs on its own interval regardless of auto-provision state.
    _now_watchdog=$(date +%s)
    _watchdog_interval_s=$(( ${GUEST_AGENT_CHECK_INTERVAL_MINUTES:-10} * 60 ))
    if (( _now_watchdog - _LAST_AGENT_WATCHDOG_CHECK >= _watchdog_interval_s )); then
        _LAST_AGENT_WATCHDOG_CHECK=$_now_watchdog
        _run_vm_agent_watchdog || true
    fi

    # Post telemetry after USB scan (has fresh USB state in this process)
    post_telemetry
    push_logs_http || true

    # WebSocket client watchdog: restart if the Python WS process has died.
    if [[ "$USE_PROXMOX_WS" -eq 1 ]] && [[ -n "${WS_PID:-}" ]]; then
        if ! kill -0 "$WS_PID" 2>/dev/null; then
            log "WARNING: WebSocket client process $WS_PID died — restarting"
            start_proxmox_ws_client || true
            # WS_PID is updated inside start_proxmox_ws_client
        fi
    fi

    # Periodic self-update: check GitHub every SELF_UPDATE_INTERVAL seconds.
    # This ensures the agent updates even if the WebUI never sends update_agent.
    _now=$(date +%s)
    if (( _now - _LAST_SELF_UPDATE >= SELF_UPDATE_INTERVAL )); then
        if self_update_agent; then
            _LAST_SELF_UPDATE=$_now
        else
            _LAST_SELF_UPDATE=$(( _now - SELF_UPDATE_INTERVAL + SELF_UPDATE_RETRY_INTERVAL ))
            log "WARNING: Agent self-update failed; retrying in ${SELF_UPDATE_RETRY_INTERVAL}s"
        fi
    fi

    # Jitter: add 0-15s random delay so multiple agents don't poll in lockstep
    _jitter=$(( RANDOM % 16 ))
    sleep $(( POLL_INTERVAL + _jitter ))
done
