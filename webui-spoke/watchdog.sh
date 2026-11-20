#!/usr/bin/env bash
# webui-watchdog: restart-only
# install-lxc.sh looks for the line above: an installed watchdog without it is
# an old one that re-ran the installer as root, and is disabled at once.
set -uo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/webui-watchdog}"
STATE_FILE="${STATE_FILE:-${STATE_DIR}/state}"
LOG_FILE="${LOG_FILE:-/var/log/webui-watchdog.log}"
INSTALL_DIR="${INSTALL_DIR:-/opt/client-sim-dashboard}"
ENV_FILE="${ENV_FILE:-${INSTALL_DIR}/.env}"
WEBUI_SERVICE="${WEBUI_SERVICE:-client-sim-dashboard}"
HEALTH_PATH="${HEALTH_PATH:-/api/health}"
PORT="${PORT:-8000}"
FAILURE_COUNT=0
SCHEME="http"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

load_port() {
  local env_port env_tls

  if [[ ! -f "$ENV_FILE" ]]; then
    return
  fi

  env_port=$(awk -F= '/^PORT=/{print $2; exit}' "$ENV_FILE" | tr -d '"[:space:]')
  if [[ "$env_port" =~ ^[0-9]+$ ]]; then
    PORT="$env_port"
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

  # The watchdog runs as root but never runs the installer. It used to
  # (after 5 failures, or whenever the version in the service-owned .env
  # differed from GitHub's), and the installer then ran git, pip and the
  # watchdog copy from directories the service user owns, so the service
  # user could make root run its code. A service that is still down after
  # two restarts needs an operator to re-run the installer as root.
  if (( failure_count_after >= 5 )); then
    if systemctl restart "$WEBUI_SERVICE"; then
      log "restart service=${WEBUI_SERVICE} after=5_failures installer_rerun_needed=run_install-lxc.sh_as_root"
    else
      log "restart_failed service=${WEBUI_SERVICE} after=5_failures installer_rerun_needed=run_install-lxc.sh_as_root"
    fi
    save_failure_count 0
  fi
}

main "$@"
