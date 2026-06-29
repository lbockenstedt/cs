#!/usr/bin/env bash
set -uo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/webui-watchdog}"
STATE_FILE="${STATE_FILE:-${STATE_DIR}/state}"
LOG_FILE="${LOG_FILE:-/var/log/webui-watchdog.log}"
INSTALL_DIR="${INSTALL_DIR:-/opt/client-sim-dashboard}"
ENV_FILE="${ENV_FILE:-${INSTALL_DIR}/.env}"
WEBUI_SERVICE="${WEBUI_SERVICE:-client-sim-dashboard}"
INSTALLER_PATH="${INSTALLER_PATH:-/opt/client-sim-repo/webui-spoke/install-lxc.sh}"
INSTALLER_TMP_PATH="${INSTALLER_TMP_PATH:-/tmp/install-lxc-latest.sh}"
HEALTH_PATH="${HEALTH_PATH:-/api/health}"
PORT="${PORT:-8000}"
REPO_BRANCH="${REPO_BRANCH:-main}"
FAILURE_COUNT=0
INSTALLED_VERSION=""
SCHEME="http"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

load_port() {
  local env_port env_branch env_inst_ver env_tls

  if [[ ! -f "$ENV_FILE" ]]; then
    return
  fi

  env_port=$(awk -F= '/^PORT=/{print $2; exit}' "$ENV_FILE" | tr -d '"[:space:]')
  if [[ "$env_port" =~ ^[0-9]+$ ]]; then
    PORT="$env_port"
  fi

  env_branch=$(awk -F= '/^REPO_BRANCH=/{print $2; exit}' "$ENV_FILE" | tr -d '"[:space:]')
  if [[ -n "$env_branch" ]]; then
    REPO_BRANCH="$env_branch"
  fi

  env_inst_ver=$(awk -F= '/^INSTALLER_VERSION=/{print $2; exit}' "$ENV_FILE" | tr -d '"[:space:]')
  if [[ -n "$env_inst_ver" ]]; then
    INSTALLED_VERSION="$env_inst_ver"
  fi

  # Read SPOKE_TLS to use the correct scheme for health checks
  env_tls=$(awk -F= '/^SPOKE_TLS=/{print $2; exit}' "$ENV_FILE" | tr -d '"[:space:]')
  if [[ "$env_tls" == "on" || "$env_tls" == "true" || "$env_tls" == "1" ]]; then
    SCHEME="https"
  fi
}

load_failure_count() {
  if [[ -f "$STATE_FILE" ]]; then
    read -r FAILURE_COUNT <"$STATE_FILE" || FAILURE_COUNT=0
  fi

  if [[ ! "$FAILURE_COUNT" =~ ^[0-9]+$ ]]; then
    FAILURE_COUNT=0
  fi
}

save_failure_count() {
  printf '%s\n' "$1" >"$STATE_FILE"
}

rerun_installer() {
  local latest_installer_url installer_to_run installer_label

  latest_installer_url="https://raw.githubusercontent.com/solutions-hpe/client-sim/${REPO_BRANCH}/webui-spoke/install-lxc.sh"
  installer_to_run="$INSTALLER_PATH"
  installer_label="$INSTALLER_PATH"

  if curl -fsSL "$latest_installer_url" -o "$INSTALLER_TMP_PATH"; then
    installer_to_run="$INSTALLER_TMP_PATH"
    installer_label="$latest_installer_url"
  else
    log "installer_rerun_download_warning url=${latest_installer_url} fallback=${INSTALLER_PATH} after=5_failures"
  fi

  if [[ -f "$installer_to_run" ]]; then
    if bash "$installer_to_run" --unattended >>"$LOG_FILE" 2>&1; then
      log "installer_rerun_complete path=${installer_label}"
    else
      log "installer_rerun_failed path=${installer_label}"
    fi
  else
    log "installer_rerun_missing path=${INSTALLER_PATH}"
  fi
}

main() {
  local -a reasons=()
  local failure_count_after=0

  mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"
  touch "$LOG_FILE"

  load_port
  load_failure_count

  if ! systemctl is-active --quiet "$WEBUI_SERVICE"; then
    reasons+=("systemd_inactive")
  fi

  if ! curl -sf --max-time 5 --insecure "${SCHEME}://localhost:${PORT}${HEALTH_PATH}" >/dev/null; then
    reasons+=("health_check_failed")
  fi

  if (( ${#reasons[@]} == 0 )); then
    if (( FAILURE_COUNT > 0 )); then
      log "recovered service=${WEBUI_SERVICE} port=${PORT} previous_failures=${FAILURE_COUNT}"
    fi
    save_failure_count 0

    # Proactive update check: fetch the remote INSTALLER_VERSION and reinstall if newer.
    remote_ver=$(curl -sSf --max-time 10 \
      "https://raw.githubusercontent.com/solutions-hpe/client-sim/${REPO_BRANCH}/webui-spoke/INSTALLER_VERSION" \
      2>/dev/null | tr -d '[:space:]')
    if [[ -n "$remote_ver" && -n "$INSTALLED_VERSION" && "$remote_ver" != "$INSTALLED_VERSION" ]]; then
      log "update_available installed=${INSTALLED_VERSION} remote=${remote_ver} — running installer"
      rerun_installer
    fi

    exit 0
  fi

  failure_count_after=$((FAILURE_COUNT + 1))
  save_failure_count "$failure_count_after"
  log "failure count=${failure_count_after} service=${WEBUI_SERVICE} port=${PORT} reasons=$(IFS=,; echo "${reasons[*]}")"

  if (( failure_count_after == 2 )); then
    if systemctl restart "$WEBUI_SERVICE"; then
      log "restart service=${WEBUI_SERVICE} after=2_failures"
    else
      log "restart_failed service=${WEBUI_SERVICE} after=2_failures"
    fi
    exit 0
  fi

  if (( failure_count_after >= 5 )); then
    log "installer_rerun_start path=${INSTALLER_PATH} repo_branch=${REPO_BRANCH} after=5_failures"
    rerun_installer
    save_failure_count 0
  fi
}

main "$@"
