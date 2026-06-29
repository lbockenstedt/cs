#!/usr/bin/env bash

set -uo pipefail

SERVICE_NAME="${SERVICE_NAME:-client-sim-agent.service}"
AGENT_SCRIPT="${AGENT_SCRIPT:-/usr/local/scripts/agent.sh}"
PID_FILE="${PID_FILE:-/var/run/client-sim-ws-agent.pid}"
HEALTH_FILE="${HEALTH_FILE:-/var/lib/client-sim/agent-health.json}"
HEALTH_STALE_SECS="${HEALTH_STALE_SECS:-120}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/usr/local/scripts/sim_watchdog.log}"
REBOOT_LOG="${REBOOT_LOG:-/usr/local/scripts/sim_reboot.log}"
ERROR_SEARCH="${ERROR_SEARCH:-Call Trace:}"
LOG_FILES=(/var/log/messages /var/log/syslog /var/log/kern.log)

log_event() {
  local target="${1:-$WATCHDOG_LOG}"
  shift || true
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$target"
}

process_is_running() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  return 0
}

process_is_zombie() {
  local pid="${1:-}"
  local state
  process_is_running "$pid" || return 1
  state=$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}' || true)
  [[ "$state" == Z* ]]
}

service_exists() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl cat "$SERVICE_NAME" >/dev/null 2>&1
}

health_age_secs() {
  [[ -f "$HEALTH_FILE" ]] || { echo 999999; return; }
  local max_ts
  max_ts=$(jq -r '
    [.last_heartbeat, .last_message_received, .last_status_sent, .updated_at, .last_connect]
    | map(select(. != null and (type == "number")))
    | if length == 0 then empty else max end
  ' "$HEALTH_FILE" 2>/dev/null) || { echo 999999; return; }
  [[ -z "$max_ts" || "$max_ts" == "null" ]] && { echo 999999; return; }
  local now age
  now=$(date +%s)
  age=$(( now - ${max_ts%.*} ))
  echo $(( age < 0 ? 0 : age ))
}

agent_expected() {
  local web_server=""
  local server_url=""

  if [[ ! -f /usr/local/scripts/ini-parser.sh || ! -f /usr/local/scripts/simulation.conf ]]; then
    return 0
  fi

  # shellcheck disable=SC1091
  source /usr/local/scripts/ini-parser.sh
  process_ini_file /usr/local/scripts/simulation.conf
  web_server=$(get_value 'simulation' 'web_server')
  server_url=$(get_value 'server' 'server_url')
  [[ "$web_server" == "on" && -n "$server_url" ]]
}

kill_legacy_agent() {
  local pid="${1:-}"
  local cmdline
  process_is_running "$pid" || return 0
  cmdline=$(ps -o args= -p "$pid" 2>/dev/null || true)
  if [[ "$cmdline" == *"/usr/local/scripts/agent.sh"* || "$cmdline" == *"linux/agent.sh"* ]]; then
    kill "$pid" 2>/dev/null || true
    log_event "$WATCHDOG_LOG" "Stopped legacy agent PID ${pid} before restart"
  fi
}

restart_agent() {
  local pid_from_file="${1:-}"
  local main_pid="${2:-}"

  if service_exists; then
    if [[ -n "$pid_from_file" && "$pid_from_file" != "$main_pid" ]]; then
      kill_legacy_agent "$pid_from_file"
    fi
    if systemctl restart "$SERVICE_NAME"; then
      log_event "$WATCHDOG_LOG" "Restarted ${SERVICE_NAME}"
    else
      log_event "$WATCHDOG_LOG" "FAILED restarting ${SERVICE_NAME}"
    fi
  elif [[ -x "$AGENT_SCRIPT" ]]; then
    if bash "$AGENT_SCRIPT"; then
      log_event "$WATCHDOG_LOG" "Restarted agent via fallback script launch"
    else
      log_event "$WATCHDOG_LOG" "FAILED fallback agent launch"
    fi
  else
    log_event "$WATCHDOG_LOG" "Agent restart skipped — no systemd unit and script missing"
  fi
}

check_agent_once() {
  local -a reasons=()
  local pid_from_file=""
  local main_pid="0"
  local health_age=999999

  if ! agent_expected; then
    exit 0
  fi

  pid_from_file=$(cat "$PID_FILE" 2>/dev/null || true)

  if service_exists; then
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
      reasons+=("systemd_inactive")
    fi
    main_pid=$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || echo 0)
    if [[ "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
      if process_is_zombie "$main_pid"; then
        reasons+=("systemd_mainpid_zombie")
      fi
    else
      reasons+=("systemd_mainpid_missing")
    fi
  fi

  if [[ -n "$pid_from_file" ]]; then
    if ! process_is_running "$pid_from_file"; then
      reasons+=("pidfile_stale")
    elif process_is_zombie "$pid_from_file"; then
      reasons+=("pidfile_zombie")
    fi
  else
    reasons+=("pidfile_missing")
  fi

  health_age=$(health_age_secs)
  if (( health_age > HEALTH_STALE_SECS )); then
    reasons+=("health_stale_${health_age}s")
  fi

  if (( ${#reasons[@]} == 0 )); then
    exit 0
  fi

  log_event "$WATCHDOG_LOG" "Agent unhealthy: reasons=$(IFS=,; echo "${reasons[*]}")"
  restart_agent "$pid_from_file" "$main_pid"
}

find_log_file() {
  local candidate
  for candidate in "${LOG_FILES[@]}"; do
    if [[ -r "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

monitor_kernel_log() {
  local line log_file

  if command -v journalctl >/dev/null 2>&1; then
    journalctl -kf -n0 --no-pager | while IFS= read -r line; do
      if [[ "$line" == *"$ERROR_SEARCH"* ]]; then
        log_event "$REBOOT_LOG" "Failure message found: $ERROR_SEARCH"
        log_event "$REBOOT_LOG" "Rebooting system"
        reboot
      fi
    done
    return 0
  fi

  if ! log_file=$(find_log_file); then
    log_event "$WATCHDOG_LOG" "No readable kernel log source found"
    exit 1
  fi

  tail -Fn0 "$log_file" | while IFS= read -r line; do
    if [[ "$line" == *"$ERROR_SEARCH"* ]]; then
      log_event "$REBOOT_LOG" "Failure message found in ${log_file}: $ERROR_SEARCH"
      log_event "$REBOOT_LOG" "Rebooting system"
      reboot
    fi
  done
}

case "${1:---log-monitor}" in
  --check-once)
    check_agent_once
    ;;
  --log-monitor|--daemon)
    monitor_kernel_log
    ;;
  *)
    echo "Usage: $0 [--check-once|--log-monitor]" >&2
    exit 1
    ;;
esac
