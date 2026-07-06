#!/bin/bash
set -u

PID_FILE="/var/run/client-sim-t3-ws-agent.pid"
SERVER_URL="${CLIENT_SIM_SERVER_URL:-}"
HOSTNAME_VAL="${CLIENT_SIM_HOSTNAME:-$(hostname)}"

[[ -z "$SERVER_URL" ]] && exit 0

handle_command() {
  local raw_cmd="${1:-{}}"
  python3 - "$raw_cmd" <<'PY'
import json, sys
cmd = json.loads(sys.argv[1] or '{}')
print(cmd.get('id',''), cmd.get('action',''), sep='\t')
PY
}

run_command() {
  local raw_cmd="${1:-{}}"
  local cmd_id action status message reboot_now
  IFS=$'\t' read -r cmd_id action < <(handle_command "$raw_cmd")
  status="completed"
  message=""
  reboot_now="false"
  case "$action" in
    restart_sim)
      nohup bash /usr/scripts/wireless.sh >/dev/null 2>&1 &
      message="wireless.sh restart triggered"
      ;;
    update_now)
      nohup bash /usr/scripts/update_script.sh >/dev/null 2>&1 &
      message="update_script.sh triggered"
      ;;
    reboot)
      message="Rebooting now"
      reboot_now="true"
      ;;
    *)
      status="failed"
      message="Unsupported action: $action"
      ;;
  esac
  python3 - <<PY
import json
print(json.dumps({"id": "$cmd_id", "status": "$status", "message": "$message", "reboot": "$reboot_now"}))
PY
}

if [[ "${1:-}" == "--handle-command" ]]; then
  run_command "${2:-{}}"
  exit 0
fi

if [[ "${1:-}" != "--daemon" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      exit 0
    fi
  fi
  nohup bash "$0" --daemon >/dev/null 2>&1 &
  echo $! > "$PID_FILE"
  exit 0
fi

trap 'rm -f "$PID_FILE"' EXIT

echo $$ > "$PID_FILE"

python3 - "$0" "$SERVER_URL" "$HOSTNAME_VAL" <<'PY'
import asyncio, contextlib, json, pathlib, subprocess, sys
script_path, server_url, hostname = sys.argv[1:4]
try:
    import websockets
except ImportError:
    sys.exit(0)

ws_url = server_url.rstrip('/').replace('https://', 'wss://').replace('http://', 'ws://')
ws_url += f"/ws/client?hostname={hostname}&platform=t3"

def detect_has_usb():
    """True if this client has a USB WiFi adapter (→ a T2 sim client). Detects a
    wireless netdev whose sysfs device path resolves through ``/usb/``.
    Best-effort: any error → False."""
    try:
        import glob, os
        for path in glob.glob("/sys/class/net/*"):
            iface = os.path.basename(path)
            if iface == "lo":
                continue
            if not (os.path.isdir(os.path.join(path, "wireless")) or
                    os.path.isdir(os.path.join(path, "phy80211"))):
                continue  # not a wireless interface
            if "/usb" in os.path.realpath(path):
                return True
        return False
    except Exception:
        return False


def collect_status():
    ssid = ""
    try:
        ssid = subprocess.check_output(["iwgetid", "-r"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        pass
    gateway_reachable = False
    try:
        subprocess.check_call(["ping", "-c", "1", "-W", "2", "8.8.8.8"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        gateway_reachable = True
    except Exception:
        pass
    return {
        "hostname": hostname,
        "simulation_id": "",
        "platform": "t3",
        "iteration": 0,
        "connected_ssid": ssid,
        "gateway_reachable": gateway_reachable,
        "active_simulations": [],
        "errors": [],
        "config": {},
        "has_usb": detect_has_usb(),
    }

async def handle_command(ws, command):
    proc = await asyncio.create_subprocess_exec(
        "bash", script_path, "--handle-command", json.dumps(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    lines = stdout.decode().strip().splitlines()
    if not lines:
        return
    ack = json.loads(lines[-1])
    await ws.send(json.dumps({"type": "ack", "payload": ack}))
    if ack.get("reboot") == "true":
        subprocess.Popen(["sudo", "reboot"])

async def send_loop(ws):
    while True:
        await ws.send(json.dumps({"type": "status", "payload": collect_status()}))
        await asyncio.sleep(30)

async def main():
    backoff = 1
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                backoff = 1
                await ws.send(json.dumps({"type": "sync"}))
                sender = asyncio.create_task(send_loop(ws))
                try:
                    async for message in ws:
                        payload = json.loads(message)
                        if str(payload.get("type") or "").lower() == "commands":
                            for command in payload.get("commands") or []:
                                await handle_command(ws, command)
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
        except Exception:
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)

asyncio.run(main())
PY
