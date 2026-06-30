#!/bin/bash
# install-proxmox-agent.sh — Install the Client-Sim Proxmox agent on this host.
# Usage: curl -sSL <raw_url> | bash -s -- --server http://172.16.1.59:8000 [--hub-url https://cs-hub.example.com:8443] [--tenant-id <uuid>] [--installer-key <key>] [--key apikey] [--interval 60]
# Or run directly: bash install-proxmox-agent.sh --server http://... --hub-url https://... --tenant-id ... --installer-key ...

SCRIPT_VERSION="1.07"

set -euo pipefail

AGENT_BIN="/usr/local/bin/client-sim-proxmox-agent"
WATCHDOG_BIN="/usr/local/bin/proxmox-watchdog"
SERVICE_NAME="client-sim-proxmox-agent"
ENV_FILE="/etc/client-sim-proxmox-agent.env"
SYSTEMD_DIR="/etc/systemd/system"
INSTALLER_DIR="/opt/proxmox-agent-installer"
INSTALLER_SCRIPT="${INSTALLER_DIR}/install-proxmox-agent.sh"
WATCHDOG_STATE_DIR="/var/lib/proxmox-watchdog"
AGENT_PORT="${CLIENT_SIM_AGENT_PORT:-9105}"
AZURE_ACCOUNT="csvmstorage"
AZURE_CONTAINER="vms"
SPOKE_IP=""
SPOKE_NAME=""
SPOKE_PORT="8000"

HUB_URL=""
TENANT_ID=""
INSTALLER_KEY="${CLIENT_SIM_INSTALLER_KEY:-}"
HUB_SET=0
TENANT_SET=0

SERVER_URL=""
API_KEY=""
POLL_INTERVAL="60"
REPO_BRANCH="${REPO_BRANCH:-main}"
UNATTENDED=0
SERVER_SET=0
KEY_SET=0
INTERVAL_SET=0
BRANCH_SET=0

OVERRIDE_CONFIG_URL="https://raw.githubusercontent.com/lbockenstedt/cs/${REPO_BRANCH}/proxmox/installer-override.conf"
if _override_content=$(curl -sf "$OVERRIDE_CONFIG_URL"); then
    source <(echo "$_override_content")
    echo "[override] Branch config applied from ${REPO_BRANCH}/proxmox/installer-override.conf"
fi

_get_installer_sas() {
    if [ -z "$HUB_URL" ]; then
        return 0
    fi
    if [ -z "$INSTALLER_KEY" ]; then
        echo "[WARN] Installer key not set; skipping SAS token fetch." >&2
        return 0
    fi

    local response
    if ! response=$(curl -sf -H "X-Installer-Key: ${INSTALLER_KEY}" "${HUB_URL}/api/backups/installer/sas-token"); then
        echo "[WARN] Failed to fetch installer SAS token; falling back to direct Azure URLs." >&2
        return 0
    fi

    local sas_url
    sas_url=$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
print((data.get("sas_url") or "").strip())
' <<< "$response")
    if [ -z "$sas_url" ]; then
        echo "[WARN] Hub returned an empty SAS token; falling back to direct Azure URLs." >&2
        return 0
    fi
    echo "$sas_url"
}

_restore_template_from_azure() {
    local _sas_url _sas_query="" _blob_base blob_manifest_url
    _sas_url=$(_get_installer_sas)
    _blob_base="https://${AZURE_ACCOUNT}.blob.core.windows.net/${AZURE_CONTAINER}"
    blob_manifest_url="${_blob_base}?restype=container&comp=list"
    if [ -n "$_sas_url" ]; then
        _sas_query="${_sas_url#*\?}"
        blob_manifest_url="${_blob_base}?${_sas_query}&restype=container&comp=list"
    fi

    local blob_manifest
    if ! blob_manifest=$(curl -sf "$blob_manifest_url"); then
        echo "[WARN] Unable to query Azure template backups. Skipping template restore."
        return 0
    fi

    local blob_list
    blob_list=$(printf '%s\n' "$blob_manifest" | grep -oP '(?<=<Name>)[^<]+\.vma\.zst' | sort || true)

    if [ -z "$blob_list" ]; then
        echo "[WARN] No template backups found in Azure (${AZURE_ACCOUNT}/${AZURE_CONTAINER}). Skipping template restore."
        return 0
    fi

    local blob_count
    blob_count=$(echo "$blob_list" | wc -l)
    local selected_blob

    if [ "$blob_count" -eq 1 ]; then
        selected_blob=$(echo "$blob_list" | head -1)
        echo "[INFO] Found template: $selected_blob"
    else
        echo "[INFO] Multiple templates available:"
        local i=1
        while IFS= read -r blob; do
            echo "  $i) $(basename "$blob")"
            i=$((i+1))
        done <<< "$blob_list"
        printf "Select template [1-${blob_count}]: "
        if ! read -r selection; then
            echo "[WARN] No template selection received. Skipping template restore."
            return 0
        fi
        if ! [[ "$selection" =~ ^[0-9]+$ ]]; then
            echo "[WARN] Invalid selection. Skipping template restore."
            return 0
        fi
        selected_blob=$(echo "$blob_list" | sed -n "${selection}p")
        if [ -z "$selected_blob" ]; then
            echo "[WARN] Invalid selection. Skipping template restore."
            return 0
        fi
    fi

    local blob_url="${_blob_base}/${selected_blob}"
    if [ -n "$_sas_query" ]; then
        blob_url="${blob_url}?${_sas_query}"
    fi
    local local_file="${INSTALLER_DIR}/$(basename "$selected_blob")"
    echo "[INFO] Downloading template from Azure: ${selected_blob}"
    if ! curl -L --progress-bar -o "$local_file" "$blob_url"; then
        echo "[ERROR] Failed to download template. Skipping restore."
        rm -f "$local_file"
        return 0
    fi

    echo "[INFO] Restoring template to VM ID 100..."
    if ! qmrestore "$local_file" 100 --force; then
        echo "[ERROR] qmrestore failed. Skipping template conversion."
        rm -f "$local_file"
        return 0
    fi

    echo "[INFO] Converting VM 100 to template..."
    if qm template 100; then
        echo "[INFO] Template VM 100 ready."
    else
        echo "[WARN] qm template conversion failed — VM 100 restored but not marked as template."
    fi

    rm -f "$local_file"
}

_restore_spoke_from_azure() {
    local _sas_url _sas_query="" _blob_base blob_manifest_url
    _sas_url=$(_get_installer_sas)
    _blob_base="https://${AZURE_ACCOUNT}.blob.core.windows.net/${AZURE_CONTAINER}"
    blob_manifest_url="${_blob_base}?restype=container&comp=list"
    if [ -n "$_sas_url" ]; then
        _sas_query="${_sas_url#*\?}"
        blob_manifest_url="${_blob_base}?${_sas_query}&restype=container&comp=list"
    fi

    local blob_manifest
    if ! blob_manifest=$(curl -sf "$blob_manifest_url"); then
        echo "[WARN] Unable to query Azure spoke backups. Skipping spoke restore."
        return 0
    fi

    local blob_list
    blob_list=$(printf '%s\n' "$blob_manifest" | grep -oP '(?<=<Name>)[^<]+\.vma\.zst' | sort || true)

    if [ -z "$blob_list" ]; then
        echo "[WARN] No spoke backups found in Azure (${AZURE_ACCOUNT}/${AZURE_CONTAINER}). Skipping spoke VM restore."
        return 0
    fi

    local blob_count
    blob_count=$(echo "$blob_list" | wc -l)
    local selected_blob

    if [ "$blob_count" -eq 1 ]; then
        selected_blob=$(echo "$blob_list" | head -1)
        echo "[INFO] Found spoke backup: $selected_blob"
    else
        echo "[INFO] Available backups in Azure:"
        local i=1
        while IFS= read -r blob; do
            echo "  $i) $(basename "$blob")"
            i=$((i+1))
        done <<< "$blob_list"
        printf "Select backup for spoke VM 1001 [1-${blob_count}]: "
        if ! read -r selection; then
            echo "[WARN] No spoke backup selection received. Skipping spoke restore."
            return 0
        fi
        if ! [[ "$selection" =~ ^[0-9]+$ ]]; then
            echo "[WARN] Invalid selection. Skipping spoke restore."
            return 0
        fi
        selected_blob=$(echo "$blob_list" | sed -n "${selection}p")
        if [ -z "$selected_blob" ]; then
            echo "[WARN] Invalid selection. Skipping spoke restore."
            return 0
        fi
    fi

    local blob_url="${_blob_base}/${selected_blob}"
    if [ -n "$_sas_query" ]; then
        blob_url="${blob_url}?${_sas_query}"
    fi
    local local_file="${INSTALLER_DIR}/$(basename "$selected_blob")"
    echo "[INFO] Downloading spoke backup from Azure: ${selected_blob}"
    if ! curl -L --progress-bar -o "$local_file" "$blob_url"; then
        echo "[ERROR] Download failed. Skipping spoke restore."
        rm -f "$local_file"
        return 0
    fi

    echo "[INFO] Restoring spoke VM to ID 1001..."
    local restore_cmd
    if echo "$selected_blob" | grep -q "lxc"; then
        restore_cmd="pct restore 1001 $local_file --force"
    else
        restore_cmd="qmrestore $local_file 1001 --force"
    fi
    if ! $restore_cmd; then
        echo "[ERROR] Restore failed. Skipping rename and start."
        rm -f "$local_file"
        return 0
    fi

    local pxmx_hostname
    pxmx_hostname=$(hostname)
    local svr_num
    svr_num=$(echo "$pxmx_hostname" | grep -oP '\d+$' || true)
    if [ -n "$svr_num" ]; then
        local spoke_name="spoke-svr-${svr_num}"
        echo "[INFO] Renaming VM 1001 to ${spoke_name}..."
        if qm list 2>/dev/null | awk '{print $1}' | grep -q '^1001$'; then
            qm set 1001 --name "$spoke_name" 2>/dev/null || true
        else
            pct set 1001 --hostname "$spoke_name" 2>/dev/null || true
        fi
    else
        echo "[WARN] Could not extract server number from hostname '$pxmx_hostname' — skipping rename."
    fi

    echo "[INFO] Starting spoke VM 1001..."
    if qm list 2>/dev/null | awk '{print $1}' | grep -q '^1001$'; then
        qm start 1001 || echo "[WARN] qm start 1001 failed — start it manually."
    else
        pct start 1001 || echo "[WARN] pct start 1001 failed — start it manually."
    fi

    rm -f "$local_file"
    echo "[INFO] Spoke VM 1001 restore complete."

    # Wait for IP then configure hub settings
    if _wait_for_spoke_ip; then
        _configure_spoke_hub
    fi
}

# Waits up to ~3 minutes for VM 1001 to boot and report a non-loopback IP.
# Sets SPOKE_IP on success. Works for both QEMU (qm agent) and LXC (pct exec).
_wait_for_spoke_ip() {
    echo "[INFO] Waiting for spoke VM 1001 to boot and report an IP address..."
    local is_qemu=0
    qm list 2>/dev/null | awk '{print $1}' | grep -q '^1001$' && is_qemu=1

    local attempts=0 max_attempts=36  # 36 × 5s = 3 minutes
    while [ "$attempts" -lt "$max_attempts" ]; do
        local ip=""
        if [ "$is_qemu" -eq 1 ]; then
            # qm agent network-get-interfaces returns JSON; extract first non-loopback IPv4
            ip=$(qm agent 1001 network-get-interfaces 2>/dev/null \
                | python3 -c "
import json, sys
ifaces = json.load(sys.stdin)
for iface in ifaces:
    if iface.get('name','') == 'lo':
        continue
    for addr in iface.get('ip-addresses', []):
        if addr.get('ip-address-type') == 'ipv4':
            print(addr['ip-address'])
            sys.exit(0)
" 2>/dev/null || true)
        else
            ip=$(pct exec 1001 -- bash -c "hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' | head -1" 2>/dev/null || true)
            # Fallback: any non-loopback, non-link-local IPv4
            if [[ -z "$ip" ]]; then
                ip=$(pct exec 1001 -- bash -c "hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|169\.|::)' | grep -E '^[0-9]+\.' | head -1" 2>/dev/null || true)
            fi
        fi

        if [ -n "$ip" ]; then
            SPOKE_IP="$ip"
            echo "[INFO] Spoke VM 1001 IP: ${SPOKE_IP}"
            return 0
        fi

        attempts=$((attempts + 1))
        printf "\r[INFO] Waiting for IP... (%ds)" "$((attempts * 5))"
        sleep 5
    done
    echo ""
    echo "[WARN] Spoke VM 1001 did not report an IP within 3 minutes — configure hub settings manually."
    return 1
}

# Bootstraps hub URL and tenant ID on the spoke from inside VM 1001 via localhost-only API.
_configure_spoke_hub() {
    if [ -z "$HUB_URL" ] && [ -z "$TENANT_ID" ]; then
        echo "[INFO] No --hub-url or --tenant-id provided — skipping spoke hub bootstrap."
        return 0
    fi

    local is_qemu=0
    qm list 2>/dev/null | awk '{print $1}' | grep -q '^1001$' && is_qemu=1

    echo "[INFO] Waiting for spoke API to be ready inside VM 1001..."
    local attempts=0 max_attempts=24 api_ready=0  # 24 × 5s = 2 minutes
    while [ "$attempts" -lt "$max_attempts" ]; do
        if [ "$is_qemu" -eq 1 ]; then
            local health_result health_exit
            health_result=$(qm guest exec 1001 --timeout 10 -- curl -sf "http://127.0.0.1:${SPOKE_PORT}/api/health" 2>/dev/null || true)
            health_exit=$(printf '%s' "$health_result" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(1)
    raise SystemExit(0)
print(data.get('exitcode', 1))
" 2>/dev/null || echo "1")
            if [ "$health_exit" = "0" ]; then
                api_ready=1
                break
            fi
        else
            if pct exec 1001 -- curl -sf "http://127.0.0.1:${SPOKE_PORT}/api/health" >/dev/null 2>&1; then
                api_ready=1
                break
            fi
        fi
        attempts=$((attempts + 1))
        printf "\r[INFO] Waiting for spoke API... (%ds)" "$((attempts * 5))"
        sleep 5
    done
    echo ""

    if [ "$api_ready" -ne 1 ]; then
        echo "[WARN] Spoke API not reachable inside VM 1001 — configure hub settings manually."
        return 0
    fi

    echo "[INFO] Bootstrapping spoke hub settings via localhost inside VM 1001..."

    local payload payload_quoted
    payload=$(python3 -c "
import json, sys
d = {}
hub_url = sys.argv[1]
tenant_id = sys.argv[2]
if hub_url:
    d['relay_server_url'] = hub_url
    d['relay_enabled'] = 'on'
if tenant_id:
    d['relay_tenant_id'] = tenant_id
    d['relay_tenant_hint'] = tenant_id
print(json.dumps(d))
" "$HUB_URL" "$TENANT_ID")
    payload_quoted=$(python3 -c "
import shlex, sys
print(shlex.quote(sys.argv[1]))
" "$payload")

    local bootstrap_cmd
    bootstrap_cmd="http_status=\$(curl -sS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:${SPOKE_PORT}/api/bootstrap -H 'Content-Type: application/json' -d ${payload_quoted} 2>/dev/null || echo 000); echo \"\$http_status\"; [ \"\$http_status\" = \"200\" ] || [ \"\$http_status\" = \"201\" ] || [ \"\$http_status\" = \"409\" ]"

    local bootstrap_status="000" bootstrap_exit=1 bootstrap_output=""
    if [ "$is_qemu" -eq 1 ]; then
        local bootstrap_result
        bootstrap_result=$(qm guest exec 1001 --timeout 30 -- /bin/bash -c "$bootstrap_cmd" 2>/dev/null || true)
        bootstrap_exit=$(printf '%s' "$bootstrap_result" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(1)
    raise SystemExit(0)
print(data.get('exitcode', 1))
" 2>/dev/null || echo "1")
        bootstrap_status=$(printf '%s' "$bootstrap_result" | python3 -c "
import base64, json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('000')
    raise SystemExit(0)
out = data.get('out-data', '')
text = out.strip()
if text:
    try:
        decoded = base64.b64decode(text, validate=True).decode('utf-8', 'ignore').strip()
        if decoded:
            text = decoded
    except Exception:
        pass
lines = text.splitlines()
print(lines[-1].strip() if lines else '000')
" 2>/dev/null || echo "000")
    else
        if bootstrap_output=$(pct exec 1001 -- /bin/bash -c "$bootstrap_cmd" 2>/dev/null); then
            bootstrap_exit=0
        fi
        bootstrap_status=$(printf '%s' "$bootstrap_output" | tail -n 1 | tr -d '\r')
        [ -n "$bootstrap_status" ] || bootstrap_status="000"
    fi

    if [ "$bootstrap_exit" = "0" ]; then
        if [ "$bootstrap_status" = "409" ]; then
            echo "[INFO] Spoke hub bootstrap already configured."
        else
            echo "[INFO] Spoke hub bootstrap completed successfully."
        fi
        [ -n "$HUB_URL" ]   && echo "  Hub URL   : $HUB_URL"
        [ -n "$TENANT_ID" ] && echo "  Tenant ID : $TENANT_ID"
    else
        echo "[WARN] Failed to bootstrap spoke hub settings (HTTP ${bootstrap_status}) — configure manually in the spoke UI."
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)      SERVER_URL="$2"; SERVER_SET=1; shift 2 ;;
        --key)         API_KEY="$2"; KEY_SET=1; shift 2 ;;
        --interval)    POLL_INTERVAL="$2"; INTERVAL_SET=1; shift 2 ;;
        --branch)      REPO_BRANCH="$2"; BRANCH_SET=1; shift 2 ;;
        --hub-url)     HUB_URL="$2"; HUB_SET=1; shift 2 ;;
        --installer-key) INSTALLER_KEY="$2"; shift 2 ;;
        --tenant-id)   TENANT_ID="$2"; TENANT_SET=1; shift 2 ;;
        --unattended)  UNATTENDED=1; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -f "$ENV_FILE" ]]; then
    existing_server=$(grep -oP '(?<=CLIENT_SIM_SERVER_URL=).*' "$ENV_FILE" || true)
    existing_key=$(grep -oP '(?<=CLIENT_SIM_API_KEY=).*' "$ENV_FILE" || true)
    existing_interval=$(grep -oP '(?<=CLIENT_SIM_POLL_INTERVAL=).*' "$ENV_FILE" || true)
    existing_branch=$(grep -oP '(?<=CLIENT_SIM_REPO_BRANCH=).*' "$ENV_FILE" || true)
    existing_agent_port=$(grep -oP '(?<=CLIENT_SIM_AGENT_PORT=).*' "$ENV_FILE" || true)

    # If --server was explicitly provided it takes priority and is written to the env file.
    # The existing CLIENT_SIM_SERVER_URL is NOT loaded here — it is written at the end
    # after all flags are resolved, so --server always wins on reinstall.
    [[ $KEY_SET -eq 1 ]] || API_KEY="$existing_key"
    [[ $INTERVAL_SET -eq 1 ]] || [[ -z "$existing_interval" ]] || POLL_INTERVAL="$existing_interval"
    [[ $BRANCH_SET -eq 1 ]] || [[ -z "$existing_branch" ]] || REPO_BRANCH="$existing_branch"
    [[ -z "$existing_agent_port" ]] || AGENT_PORT="$existing_agent_port"
fi

[[ "$HUB_SET" -eq 0 && -n "${OVERRIDE_HUB_URL:-}" ]] && HUB_URL="$OVERRIDE_HUB_URL"
[[ "$TENANT_SET" -eq 0 && -n "${OVERRIDE_TENANT_ID:-}" ]] && TENANT_ID="$OVERRIDE_TENANT_ID"
[[ "$SERVER_SET" -eq 0 && -n "${OVERRIDE_SERVER_URL:-}" ]] && SERVER_URL="$OVERRIDE_SERVER_URL"

REPO_RAW="https://raw.githubusercontent.com/lbockenstedt/cs/${REPO_BRANCH}"

if [[ -z "$SERVER_URL" ]]; then
    # No --server given — auto-detect spoke IP from LXC 1001
    if ! command -v pct &>/dev/null; then
        echo "ERROR: --server <url> is required (pct not available for auto-detection)"
        exit 1
    fi
    if ! pct list 2>/dev/null | awk '{print $1}' | grep -q '^1001$'; then
        echo "ERROR: LXC container 1001 not found and --server not specified"
        exit 1
    fi
    echo "[INFO] No --server specified — detecting spoke IP from LXC 1001..."
    _wait_for_spoke_ip
    if [[ -z "$SPOKE_IP" ]]; then
        echo "ERROR: Could not determine IP from LXC 1001; pass --server http://<ip>:8000"
        exit 1
    fi
    SERVER_URL="http://${SPOKE_IP}:${SPOKE_PORT}"
    echo "[INFO] Auto-detected server: $SERVER_URL"
fi

if ! command -v qm &>/dev/null && [[ ! -x /usr/sbin/qm ]]; then
    echo "WARNING: 'qm' not found — continuing anyway, but this script is intended for Proxmox hosts."
fi

echo "=== Client-Sim Proxmox Agent Installer v${SCRIPT_VERSION} ==="
echo "Server    : $SERVER_URL"
echo "Branch    : $REPO_BRANCH"
echo "Key       : ${API_KEY:+(set)}"
echo "Hub URL   : ${HUB_URL:-(not set)}"
echo "Tenant ID : ${TENANT_ID:-(not set)}"
echo "Mode      : $([[ $UNATTENDED -eq 1 ]] && echo unattended || echo interactive)"
echo

install -d -m 0755 "$INSTALLER_DIR" "$WATCHDOG_STATE_DIR"

echo "[1/6] Downloading agent and watchdog scripts..."
curl -sSL "${REPO_RAW}/proxmox/proxmox-agent.sh" -o "$AGENT_BIN"
curl -sSL "${REPO_RAW}/proxmox/watchdog.sh" -o "$WATCHDOG_BIN"
curl -sSL "${REPO_RAW}/proxmox/install-proxmox-agent.sh" -o "$INSTALLER_SCRIPT"
chmod +x "$AGENT_BIN" "$WATCHDOG_BIN" "$INSTALLER_SCRIPT"
AGENT_VERSION=$(grep -oP '(?<=^AGENT_VERSION=")[^"]+' "$AGENT_BIN" 2>/dev/null || true)
echo "  OK: $AGENT_BIN${AGENT_VERSION:+ (agent v${AGENT_VERSION})}"
echo "  OK: $WATCHDOG_BIN"
echo "  OK: $INSTALLER_SCRIPT"

echo "[1b/6] Deploying Proxmox helper scripts to /etc/pve/scripts/..."
# clone.sh, ini-parser.sh, check_guest.sh, client-setup.conf, and sync-scripts.sh
# live in /etc/pve/scripts/ (Proxmox cluster FS — chmod not supported here).
# Without these files clone operations cannot run.
PVE_SCRIPTS_DIR="/etc/pve/scripts"
PVE_SCRIPT_FILES=(
    "clone.sh"
    "ini-parser.sh"
    "check_guest.sh"
    "client-setup.conf"
    "sync-scripts.sh"
)
if [[ -d /etc/pve ]]; then
    mkdir -p "$PVE_SCRIPTS_DIR"
    _pve_ok=0
    _pve_fail=0
    for _pve_file in "${PVE_SCRIPT_FILES[@]}"; do
        _pve_dest="${PVE_SCRIPTS_DIR}/${_pve_file}"
        _pve_tmp="${PVE_SCRIPTS_DIR}/.${_pve_file}.tmp"
        if curl -fsSL --connect-timeout 15 --max-time 60 "${REPO_RAW}/proxmox/${_pve_file}" -o "$_pve_tmp" 2>/dev/null; then
            # Only write if content changed (avoids touching cluster FS unnecessarily)
            if ! cmp -s "$_pve_tmp" "$_pve_dest" 2>/dev/null; then
                mv "$_pve_tmp" "$_pve_dest"
                echo "  OK: ${_pve_dest} (updated)"
            else
                rm -f "$_pve_tmp"
                echo "  OK: ${_pve_dest} (unchanged)"
            fi
            (( _pve_ok++ )) || true
        else
            rm -f "$_pve_tmp"
            echo "  WARNING: failed to download ${_pve_file} — skipping"
            (( _pve_fail++ )) || true
        fi
    done
    echo "  Deployed ${_pve_ok} file(s)${_pve_fail:+, ${_pve_fail} skipped} to ${PVE_SCRIPTS_DIR}"
else
    echo "  SKIP: /etc/pve not found — not a Proxmox host or pmxcfs not mounted"
fi

echo "[2/6] Downloading systemd units..."
curl -sSL "${REPO_RAW}/proxmox/client-sim-proxmox-agent.service" -o "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
curl -sSL "${REPO_RAW}/proxmox/proxmox-watchdog.service" -o "${SYSTEMD_DIR}/proxmox-watchdog.service"
curl -sSL "${REPO_RAW}/proxmox/proxmox-watchdog.timer" -o "${SYSTEMD_DIR}/proxmox-watchdog.timer"
chmod 0644 "${SYSTEMD_DIR}/${SERVICE_NAME}.service" "${SYSTEMD_DIR}/proxmox-watchdog.service" "${SYSTEMD_DIR}/proxmox-watchdog.timer"
echo "  OK: ${SYSTEMD_DIR}/${SERVICE_NAME}.service"
echo "  OK: ${SYSTEMD_DIR}/proxmox-watchdog.service"
echo "  OK: ${SYSTEMD_DIR}/proxmox-watchdog.timer"

echo "[3/6] Writing environment file..."
cat > "$ENV_FILE" <<ENV
CLIENT_SIM_API_KEY=${API_KEY}
CLIENT_SIM_POLL_INTERVAL=${POLL_INTERVAL}
CLIENT_SIM_REPO_BRANCH=${REPO_BRANCH}
CLIENT_SIM_REPO_RAW=${REPO_RAW%/${REPO_BRANCH}}
CLIENT_SIM_AGENT_PORT=${AGENT_PORT}
ENV
# Persist the server URL so systemd restarts don't require --server each time.
# When --server is given the agent will never attempt LXC 1001 auto-detection.
if [[ "$SERVER_SET" -eq 1 ]] && [[ -n "$SERVER_URL" ]]; then
    echo "CLIENT_SIM_SERVER_URL=${SERVER_URL}" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
echo "  OK: $ENV_FILE"

echo "[4/6] Preparing watchdog state..."
install -d -m 0755 "$WATCHDOG_STATE_DIR"
echo "  OK: $WATCHDOG_STATE_DIR"

echo "[5/7] Enabling and (re)starting service + timer..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME" --no-block
systemctl enable --now proxmox-watchdog.timer --no-block
systemctl start proxmox-watchdog.service --no-block || true
sleep 5
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  OK: service running"
else
    echo "  WARNING: service failed to start — check: journalctl -u $SERVICE_NAME"
fi
if systemctl is-active --quiet proxmox-watchdog.timer; then
    echo "  OK: watchdog timer running"
else
    echo "  WARNING: watchdog timer failed to start — check: systemctl status proxmox-watchdog.timer"
fi

echo "[6/7] Crash hardening: kernel watchdog + hung-task detection + kdump..."
# Kernel settings: log hung tasks after 120 s, reboot automatically on kernel
# panic / oops so the host recovers without manual intervention.
SYSCTL_CONF="/etc/sysctl.d/99-client-sim-watchdog.conf"
cat > "$SYSCTL_CONF" <<'SYSCTL'
# Client-Sim: detect and recover from kernel hangs / panics
kernel.hung_task_timeout_secs=120
kernel.panic=10
kernel.panic_on_oops=1
SYSCTL
sysctl -p "$SYSCTL_CONF" >/dev/null 2>&1 && echo "  OK: kernel hang/panic sysctl applied" \
    || echo "  WARNING: sysctl apply failed — settings will take effect on next reboot"

# Load the softdog kernel watchdog so the host auto-reboots if the kernel
# freezes and nothing feeds /dev/watchdog within soft_margin seconds.
MODULES_CONF="/etc/modules-load.d/client-sim-watchdog.conf"
if ! grep -q "^softdog" "$MODULES_CONF" 2>/dev/null; then
    echo "softdog" >> "$MODULES_CONF"
fi
modprobe softdog soft_margin=60 2>/dev/null && echo "  OK: softdog watchdog module loaded" \
    || echo "  WARNING: softdog module unavailable — kernel-level reboot watchdog not active"

# Install kdump-tools for post-crash kernel dump collection.
# Dumps land in /var/crash/ and survive reboots for later analysis.
if apt-get install -y -qq kdump-tools 2>/dev/null; then
    systemctl enable kdump-tools 2>/dev/null || true
    echo "  OK: kdump-tools installed — crash dumps will be written to /var/crash/"
else
    echo "  INFO: kdump-tools not available on this kernel/distro — skipping crash dump setup"
fi

echo "[7/7] Testing connection to WebUI..."
if curl -sSf --max-time 5 "${SERVER_URL}/api/health" | grep -q '"status".*"ok"'; then
    echo "  OK: WebUI reachable at $SERVER_URL"
else
    echo "  WARNING: Could not reach WebUI at $SERVER_URL"
fi

echo "[INFO] Checking for template VM (ID 100)..."
if qm list 2>/dev/null | awk '{print $1}' | grep -q '^100$'; then
    echo "[INFO] Template VM 100 already exists — skipping Azure restore."
else
    echo "[INFO] VM 100 not found — checking Azure for template backup..."
    _restore_template_from_azure
fi

if [[ "$SERVER_SET" -eq 1 ]]; then
    echo "[INFO] --server provided — spoke is managed externally, skipping spoke VM 1001 check."
else
    echo "[INFO] Checking for spoke VM (ID 1001)..."
    if qm list 2>/dev/null | awk '{print $1}' | grep -q '^1001$' || \
       pct list 2>/dev/null | awk '{print $1}' | grep -q '^1001$'; then
        echo "[INFO] Spoke VM 1001 already exists — skipping restore."
    else
        echo "[INFO] VM 1001 not found — checking Azure for spoke backup..."
        _restore_spoke_from_azure
    fi
fi

echo
echo "=== Installation complete ==="
echo "  Agent    : v${AGENT_VERSION:-unknown}"
[ -n "$SPOKE_IP" ] && echo "  Spoke IP : ${SPOKE_IP}  (login: http://${SPOKE_IP}:${SPOKE_PORT})"
echo "  Logs     : journalctl -u $SERVICE_NAME -f"
echo "  Status   : systemctl status $SERVICE_NAME"
echo "  Watchdog : systemctl status proxmox-watchdog.timer"
