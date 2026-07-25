#!/bin/bash
version=.01
# agent.sh — Client websocket agent — v1.05
# Launches a background websocket client that streams status and receives commands.

set -u

PID_FILE="/usr/local/scripts/client-sim-ws-agent.pid"
STATUS_FILE="/usr/local/scripts/client-status.json"
HEALTH_FILE="/var/lib/client-sim/agent-health.json"
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-agent.log"

mkdir -p "$(dirname "$HEALTH_FILE")"
touch "$debug" "$log" 2>/dev/null && chmod a+w "$debug" "$log" 2>/dev/null || true

echo "Agent Script $(date)" | tee -a "$debug"

log_info() {
  echo "$*" | tee -a "$debug" "$log"
}

log_warning() {
  local payload="${1:-}"
  echo "[WARN] Malformed payload (truncated): ${payload:0:200}" | tee -a "$debug" "$log" >&2
}

pid_is_active() {
  local pid="${1:-}"
  local state
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state=$(ps -o stat= -p "$pid" 2>/dev/null | awk '{print $1}' || true)
  [[ "$state" == Z* ]] && return 1
  return 0
}

cleanup() {
  local current_pid=""
  current_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ "$current_pid" == "$$" ]]; then
    rm -f "$PID_FILE"
  fi
}

trap cleanup EXIT INT TERM

if [[ ! -f /usr/local/scripts/ini-parser.sh || ! -f /usr/local/scripts/simulation.conf ]]; then
  log_info "[ERROR] Agent prerequisites missing: ini-parser.sh or simulation.conf"
  exit 1
fi

source '/usr/local/scripts/ini-parser.sh'
process_ini_file '/usr/local/scripts/simulation.conf'

web_server=$(get_value 'simulation' 'web_server')
# Default to ON (hub mode); flip to off ONLY when the conf literally says "off"
# so an unreadable/missing config (empty value) stays ON. See update.sh.
[[ "$web_server" != "off" ]] && web_server="on"
server_url=$(get_value 'server' 'server_url')
platform="${CLIENT_SIM_PLATFORM:-linux}"
hostname_val=$(hostname)

handle_command() {
  local raw_cmd="${1:-}"
  local cmd_id action args_json
  if command -v jq &>/dev/null; then
    cmd_id=$(printf '%s' "$raw_cmd"    | jq -r '.id     // ""' 2>/dev/null || true)
    action=$(printf '%s' "$raw_cmd"    | jq -r '.action // ""' 2>/dev/null || true)
    args_json=$(printf '%s' "$raw_cmd" | jq -c '.args   // {}' 2>/dev/null || echo '{}')
  else
    cmd_id=$(printf '%s' "$raw_cmd"  | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('id',''))" 2>/dev/null || true)
    action=$(printf '%s' "$raw_cmd"  | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('action',''))" 2>/dev/null || true)
    args_json=$(printf '%s' "$raw_cmd" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(json.dumps(d.get('args',{})))" 2>/dev/null || echo '{}')
  fi
  printf '%s\t%s\t%s\n' "$cmd_id" "$action" "$args_json"
}

run_command() {
  local raw_cmd="${1:-}"
  local cmd_id action args_json arg_value status message reboot_now parsed_cmd
  if ! parsed_cmd=$(handle_command "$raw_cmd" 2>/dev/null); then
    log_warning "$raw_cmd"
    parsed_cmd=$'\t\t'
  fi
  IFS=$'\t' read -r cmd_id action args_json <<< "$parsed_cmd"
  if ! arg_value=$(printf '%s' "$args_json" | jq -r '.value // ""' 2>/dev/null); then
    arg_value=$(printf '%s' "$args_json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('value',''))" 2>/dev/null || true)
  fi
  status="completed"
  message=""
  reboot_now="false"

  echo "Executing command: $cmd_id action=$action" | tee -a "$debug" "$log"
  case "$action" in
    restart_sim)
      # simulation.sh is sourced by startup.sh (argv is startup.sh), so pgrep
      # can't find it — read the PID it wrote instead.
      sim_pid=$(cat /usr/local/scripts/simulation.pid 2>/dev/null)
      if [[ -n "$sim_pid" ]] && kill -0 "$sim_pid" 2>/dev/null; then
        # Sourced loop can't be cleanly re-exec'd — USR1 triggers a config
        # reload as best-effort; a full restart requires a reboot.
        kill -USR1 "$sim_pid" 2>/dev/null || true
        message="Reload signal sent (PID $sim_pid); full restart requires a reboot"
      else
        message="simulation.sh not running — no action taken"
      fi
      ;;
    reboot)
      sim_pid=$(cat /usr/local/scripts/simulation.pid 2>/dev/null)
      if [[ -z "$sim_pid" ]] || ! kill -0 "$sim_pid" 2>/dev/null; then
        echo "Early-boot guard: skipping reboot command — simulation not yet running" | tee -a "$debug"
        message="Skipped — early-boot protection (simulation not running)"
      else
        message="Rebooting now"
        reboot_now="true"
      fi
      ;;
    update_now)
      if bash /usr/local/scripts/update.sh; then
        message="Update completed successfully"
      else
        status="failed"
        message="Update failed — check debug-agent.log"
        echo "update_now: update.sh exited non-zero" | tee -a "$debug"
      fi
      ;;
    kill_switch)
      ks_val="${arg_value:-on}"
      if [[ "$ks_val" != "on" && "$ks_val" != "off" ]]; then ks_val="on"; fi
      sed -i "s/^kill_switch=.*/kill_switch=${ks_val}/" /usr/local/scripts/simulation.conf
      sim_pid=$(cat /usr/local/scripts/simulation.pid 2>/dev/null)
      if [[ -n "$sim_pid" ]] && kill -0 "$sim_pid" 2>/dev/null; then
        kill -USR1 "$sim_pid" 2>/dev/null || true
      fi
      if [[ "$ks_val" == "on" ]]; then
        message="Kill switch activated"
      else
        message="Kill switch deactivated — simulation will restart"
      fi
      ;;
    debug_mode)
      # Per-client remote debug mode (immediate, non-persistent). Writes a marker
      # file the Python WS loop's tailer task polls; 'off' removes it. While the
      # flag is present the tailer streams sim.log + debug-update.log +
      # debug-agent.log (level=basic; level=advanced adds journalctl + dmesg) up
      # to the hub as {"type":"debug_log",...}. Auto-off ~30m via the deadline in
      # the flag. NOT written to simulation.conf (immediate command, not a
      # persisted sim flag). See plan: precious-napping-seahorse.md.
      dbg_flag="/usr/local/scripts/debug-mode.flag"
      dbg_enabled=$(printf '%s' "$args_json" | jq -r '.enabled // ""' 2>/dev/null || true)
      [[ -z "$dbg_enabled" ]] && dbg_enabled=$(printf '%s' "$args_json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('enabled',''))" 2>/dev/null || true)
      dbg_level=$(printf '%s' "$args_json" | jq -r '.level // "basic"' 2>/dev/null || echo basic)
      [[ "$dbg_level" != "advanced" ]] && dbg_level="basic"
      if [[ "$dbg_enabled" == "false" || "$dbg_enabled" == "off" || "$dbg_enabled" == "0" || "$dbg_enabled" == "no" ]]; then
        rm -f "$dbg_flag" 2>/dev/null || true
        message="Debug mode disabled"
      else
        dbg_deadline=$(( $(date +%s) + 30 * 60 ))
        printf 'level=%s\ndeadline=%s\nenabled_at=%s\n' "$dbg_level" "$dbg_deadline" "$(date +%s)" > "$dbg_flag" 2>/dev/null || true
        chmod a+r "$dbg_flag" 2>/dev/null || true
        message="Debug mode enabled (level=$dbg_level, auto-off in 30m)"
      fi
      ;;
    *)
      status="failed"
      message="Unknown action: $action"
      echo "Unknown action: $action" | tee -a "$debug"
      ;;
  esac

  if command -v jq &>/dev/null; then
    jq -n \
      --arg id      "$cmd_id" \
      --arg status  "$status" \
      --arg message "$message" \
      --arg reboot  "$reboot_now" \
      '{id:$id, status:$status, message:$message, reboot:$reboot}'
  else
    # Pass values via the environment so server-supplied $cmd_id/$message
    # can't break out of the python program string.
    CMD_ID="$cmd_id" CMD_STATUS="$status" CMD_MESSAGE="$message" CMD_REBOOT="$reboot_now" \
      python3 -c 'import json,os; print(json.dumps({"id":os.environ["CMD_ID"],"status":os.environ["CMD_STATUS"],"message":os.environ["CMD_MESSAGE"],"reboot":os.environ["CMD_REBOOT"]}))'
  fi
}

if [[ "${1:-}" == "--handle-command" ]]; then
  run_command "${2:-{}}"
  exit 0
fi

if [[ "$web_server" != "on" || -z "$server_url" ]]; then
  log_info "Agent disabled — web_server=$web_server server_url_present=$([[ -n "$server_url" ]] && echo yes || echo no)"
  exit 0
fi

existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
if pid_is_active "$existing_pid"; then
  exit 0
fi
# Suppress errors: remove stale PID file before writing new one.
rm -f "$PID_FILE" 2>/dev/null || true

if [[ "${1:-}" != "--daemon" ]]; then
  nohup bash "$0" --daemon >/dev/null 2>&1 &
  echo $! > "$PID_FILE"
  chmod a+rw "$PID_FILE" 2>/dev/null || true
  exit 0
fi

echo $$ > "$PID_FILE"
chmod a+rw "$PID_FILE" 2>/dev/null || true

python3 - "$0" "$server_url" "$hostname_val" "$platform" "$STATUS_FILE" "$HEALTH_FILE" "$debug" "$log" <<'PY'
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time

script_path, server_url, hostname, platform, status_file, health_file, debug_log, main_log = sys.argv[1:9]
health_path = pathlib.Path(health_file)
health_path.parent.mkdir(parents=True, exist_ok=True)


def log_message(message):
    for path in (debug_log, main_log):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except Exception:
            pass


def warn(raw):
    log_message(f"[WARN] Malformed payload (truncated): {str(raw)[:200]}")


def write_health(**updates):
    try:
        data = json.loads(health_path.read_text()) if health_path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("hostname", hostname)
    data.setdefault("platform", platform)
    data["pid"] = os.getpid()
    data["updated_at"] = time.time()
    data.update(updates)
    health_path.write_text(json.dumps(data))


write_health(state="starting", connected=False, last_error="")

try:
    import websockets
except ImportError:
    message = "python3 websockets module is not installed"
    log_message(f"[ERROR] {message}")
    write_health(state="fatal", connected=False, last_error=message, last_failure=time.time())
    sys.exit(1)

try:
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    key_url = server_url.rstrip('/') + "/api/client/key"
    with urllib.request.urlopen(key_url, timeout=10, context=ctx) as resp:
        client_api_key = json.loads(resp.read()).get("client_api_key", "")
except Exception as exc:
    log_message(f"[WARN] Could not fetch client_api_key from spoke: {exc!r}")
    client_api_key = ""

import urllib.parse
ws_url = server_url.rstrip('/').replace('https://', 'wss://').replace('http://', 'ws://')
ws_url += f"/ws/client?hostname={urllib.parse.quote(hostname)}&platform={urllib.parse.quote(platform)}"
if client_api_key:
    ws_url += f"&api_key={urllib.parse.quote(client_api_key)}"


def detect_has_usb():
    """True if this client has a USB WiFi adapter (→ a T2 sim client). Detects a
    wireless netdev whose sysfs device path resolves through ``/usb/``. Live
    hardware fact, so it's stamped on every status frame (the hub's
    csClassifyClient reads has_usb first). Best-effort: any error → False."""
    try:
        import glob
        import os as _os
        for path in glob.glob("/sys/class/net/*"):
            iface = _os.path.basename(path)
            if iface == "lo":
                continue
            if not (_os.path.isdir(_os.path.join(path, "wireless")) or
                    _os.path.isdir(_os.path.join(path, "phy80211"))):
                continue  # not a wireless interface
            if "/usb" in _os.path.realpath(path):
                return True
        return False
    except Exception:
        return False


def fallback_status():
    return {
        "hostname": hostname,
        "simulation_id": "",
        "platform": platform,
        "iteration": 0,
        "connected_ssid": "",
        "gateway_reachable": False,
        "active_simulations": [],
        "errors": [],
        "config": {},
        "has_usb": detect_has_usb(),
    }


def load_status():
    path = pathlib.Path(status_file)
    if not path.exists():
        return fallback_status()
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return fallback_status()
    payload.setdefault("hostname", hostname)
    payload.setdefault("platform", platform)
    payload.setdefault("simulation_id", "")
    payload.setdefault("iteration", 0)
    payload.setdefault("gateway_reachable", False)
    payload.setdefault("active_simulations", [])
    payload.setdefault("errors", [])
    payload.setdefault("config", {})
    # Live-detected each frame (hardware fact, not from the status file) so the
    # hub can classify this client T1 (no USB WiFi) vs T2 (USB WiFi dongle).
    payload["has_usb"] = detect_has_usb()
    return payload


# ── Remote debug-mode log tailer ────────────────────────────────────────────
# Activated by the `debug_mode` command (see bash run_command above), which
# writes /usr/local/scripts/debug-mode.flag with level + deadline. This task
# polls the flag and streams new log lines up to the hub as
# {"type":"debug_log","payload":{"lines":[...],"level":...}} so an operator can
# troubleshoot one box remotely from the WebUI. Auto-off ~30m (deadline in the
# flag); 'off' removes the flag. Rides the spoke's throttle interval so a
# throttled client slows the debug stream too.
DEBUG_FLAG = "/usr/local/scripts/debug-mode.flag"
BASIC_DEBUG_LOGS = [
    "/usr/local/scripts/sim.log",
    "/usr/local/scripts/debug-update.log",
    "/usr/local/scripts/debug-agent.log",
]
DEBUG_BATCH_CAP = 200       # max lines per WS frame
DEBUG_LOOP_INTERVAL = 2.5   # seconds between tail cycles (floor)


def _read_debug_flag():
    """Return (level, deadline, enabled_at) from the flag file, or None if the
    flag is absent/unreadable (debug mode off)."""
    try:
        p = pathlib.Path(DEBUG_FLAG)
        if not p.exists():
            return None
        kv = {}
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        level = kv.get("level", "basic")
        if level != "advanced":
            level = "basic"
        return level, int(kv.get("deadline", "0") or 0), int(kv.get("enabled_at", "0") or 0)
    except Exception:
        return None


def _tail_file(path, offsets):
    """Return new lines from `path` since the last offset. Handles
    truncation/rotation (file shrank → reset to 0). Best-effort: never raises."""
    try:
        st = os.stat(path)
    except Exception:
        return []
    prev = offsets.get(path, 0)
    if st.st_size < prev:
        prev = 0  # truncated/rotated
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(prev)
            chunk = f.read()
            offsets[path] = f.tell()
        return [ln for ln in chunk.splitlines() if ln.strip() != ""]
    except Exception:
        return []


def _new_journal_lines(since_ts):
    """New journalctl lines since `since_ts` (epoch). Non-blocking poll
    (--since, no -f). Best-effort: empty on any failure (e.g. no journal)."""
    try:
        import datetime
        iso = datetime.datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
        out = subprocess.run(
            ["journalctl", "--since", iso, "-q", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
        )
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def _new_dmesg_lines(seen):
    """Return (new_lines, new_seen_count). `seen` = dmesg line count already
    sent; we send lines beyond it. Best-effort: ([], seen) on failure."""
    try:
        out = subprocess.run(["dmesg", "--color=never"],
                             capture_output=True, text=True, timeout=5)
        lines = out.stdout.splitlines()
        if len(lines) <= seen:
            return [], seen
        return lines[seen:], len(lines)
    except Exception:
        return [], seen


async def debug_log_loop(ws, interval_ref):
    """Poll the debug-mode flag and stream new log lines up to the hub until the
    flag is cleared (off) or its deadline passes (auto-off). Cancelling the task
    (on WS disconnect) stops streaming; the flag file is left in place so a
    reconnect resumes (the deadline still bounds it)."""
    offsets = {}
    journal_since = time.time()
    dmesg_seen = 0
    last_enabled_at = 0
    advanced_ready = False
    while True:
        try:
            flag = _read_debug_flag()
            if flag is None:
                await asyncio.sleep(DEBUG_LOOP_INTERVAL)
                continue
            level, deadline, enabled_at = flag
            now = time.time()
            if now >= deadline:
                # Auto-off: the previous cycle already flushed available lines,
                # so just remove the flag and idle. No special final flush needed.
                try:
                    pathlib.Path(DEBUG_FLAG).unlink(missing_ok=True)
                except Exception:
                    pass
                log_message("[INFO] Debug mode auto-off (deadline reached)")
                # Reset session state so a future enable starts clean.
                offsets = {}
                journal_since = time.time()
                dmesg_seen = 0
                last_enabled_at = 0
                advanced_ready = False
                await asyncio.sleep(DEBUG_LOOP_INTERVAL)
                continue
            # New session (or re-enabled): reset offsets + advanced state.
            if enabled_at != last_enabled_at:
                last_enabled_at = enabled_at
                offsets = {}
                journal_since = enabled_at if enabled_at else time.time()
                dmesg_seen = 0
                advanced_ready = False
            lines = []
            for p in BASIC_DEBUG_LOGS:
                lines.extend(_tail_file(p, offsets))
            if level == "advanced":
                if not advanced_ready:
                    # Seed dmesg_seen to the current buffer length so we only
                    # stream NEW kernel lines, not the whole ring buffer.
                    _dl, dmesg_seen = _new_dmesg_lines(0)
                    dmesg_seen = dmesg_seen or 0
                    journal_since = time.time()
                    advanced_ready = True
                lines.extend(_new_journal_lines(journal_since))
                journal_since = time.time()
                dl, dmesg_seen = _new_dmesg_lines(dmesg_seen)
                lines.extend(dl)
            if lines:
                # Tag advanced system lines so the hub view can tell sim.log
                # entries apart from journal/dmesg (they share the frame).
                batch = lines[:DEBUG_BATCH_CAP]
                try:
                    await ws.send(json.dumps({
                        "type": "debug_log",
                        "payload": {"lines": batch, "level": level},
                    }))
                except Exception as exc:
                    log_message(f"[WARN] debug_log send failed: {exc!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_message(f"[WARN] debug_log_loop error: {exc!r}")
        # Ride throttle: a throttled client slows the debug stream in step.
        await asyncio.sleep(max(DEBUG_LOOP_INTERVAL, interval_ref["value"] * 0.5))


async def handle_command(ws, command):
    proc = await asyncio.create_subprocess_exec(
        "bash", script_path, "--handle-command", json.dumps(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    raw = stdout.decode().strip().splitlines()
    if not raw:
        return
    try:
        ack = json.loads(raw[-1])
    except Exception:
        warn(raw[-1])
        return
    await ws.send(json.dumps({"type": "ack", "payload": ack}))
    write_health(last_ack=time.time(), last_heartbeat=time.time(), state="connected", connected=True)
    if ack.get("reboot") == "true":
        subprocess.Popen(["sudo", "reboot"])


async def send_loop(ws, interval_ref):
    import random
    # Stagger the first send by a random fraction of the initial interval to
    # prevent phase-lock when many clients all connect at the same time.
    await asyncio.sleep(random.uniform(0, interval_ref["value"]))
    while True:
        await ws.send(json.dumps({"type": "status", "payload": load_status()}))
        write_health(last_status_sent=time.time(), last_heartbeat=time.time(), state="connected", connected=True)
        # Apply jitter ±15% to avoid synchronized bursts when throttle is active
        jitter = interval_ref["value"] * random.uniform(0.85, 1.15)
        await asyncio.sleep(jitter)


async def main():
    import random
    backoff = 1
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                now = time.time()
                backoff = 1
                write_health(state="connected", connected=True, last_connect=now, last_heartbeat=now, last_error="")
                await ws.send(json.dumps({"type": "sync"}))
                write_health(last_status_sent=time.time(), last_heartbeat=time.time(), state="connected", connected=True)
                # Mutable container so send_loop always reads the current value
                interval_ref = {"value": 15}
                sender = asyncio.create_task(send_loop(ws, interval_ref))
                # Remote debug-mode log tailer — polls debug-mode.flag and streams
                # log lines up as {"type":"debug_log",...}. No-op when the flag is
                # absent. Cancelled alongside sender on disconnect.
                debug_tailer = asyncio.create_task(debug_log_loop(ws, interval_ref))
                try:
                    async for message in ws:
                        now = time.time()
                        write_health(last_message_received=now, last_heartbeat=now, state="connected", connected=True)
                        try:
                            payload = json.loads(message)
                        except Exception:
                            warn(message)
                            continue
                        msg_type = str(payload.get("type") or "").lower()
                        if msg_type == "commands":
                            for command in payload.get("commands") or []:
                                await handle_command(ws, command)
                        elif msg_type == "throttle":
                            new_interval = int(payload.get("interval") or 15)
                            new_interval = max(5, min(300, new_interval))
                            if new_interval != interval_ref["value"]:
                                interval_ref["value"] = new_interval
                                log_message(f"[INFO] Send interval updated to {new_interval}s (server throttle)")
                finally:
                    sender.cancel()
                    debug_tailer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
                    with contextlib.suppress(asyncio.CancelledError):
                        await debug_tailer
                    write_health(state="disconnected", connected=False, last_disconnect=time.time())
        except Exception as exc:
            retry_delay = min(backoff, 30)
            log_message(f"[WARN] Agent websocket loop error: {exc!r}")
            write_health(
                state="reconnecting",
                connected=False,
                last_error=repr(exc),
                last_failure=time.time(),
                next_retry_in=retry_delay,
            )
            await asyncio.sleep(retry_delay)
            backoff = min(backoff * 2, 30)


asyncio.run(main())
PY
