"""System API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    BASE_DIR,
    ClientStatus,
    HTTPException,
    Path,
    PlainTextResponse,
    REPO_URL,
    Request,
    STATIC_DIR,
    _api_health_payload,
    _apply_client_status,
    _async_save_commands,
    _broadcast_proxmox_state,
    _broadcast_reclone_state,
    _command_trace,
    _debug_log,
    _default_provision_run_state,
    _hw_alerts_payload,
    _proxmox_status_payload,
    _serialize_commands,
    _server_pressure,
    _server_start_time,
    _sync_sim_tags_for_client,
    asyncio,
    background_tasks,
    broadcast,
    commands,
    gkill_switch_state,
    logger,
    os,
    pending_proxmox_agents,
    proxmox_log_buffer,
    proxmox_state,
    reclone_state,
    refresh_webui_frontend,
    relay_state,
    repo_path,
    service_health,
    state,
    state_lock,
    time,
)

router = APIRouter()




@router.get("/api/local-wsites")
async def api_local_wsites() -> dict[str, Any]:
    """Extract unique wsite values from simulation.conf in the repo."""
    import configparser
    config_path = repo_path("configs", "simulation.conf")
    if not config_path.exists():
        return {"wsites": []}
    parser = configparser.ConfigParser()
    try:
        parser.read_string(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not parse simulation.conf: %s", exc)
        return {"wsites": []}
    wsites: set[str] = set()
    for section in parser.sections():
        if parser.has_option(section, "wsite"):
            val = parser.get(section, "wsite").strip()
            if val:
                wsites.add(val)
    return {"wsites": sorted(wsites)}




@router.get("/api/hardware-alerts")
async def api_hardware_alerts() -> dict[str, Any]:
    """Return configured hardware checks merged with current alert device data."""
    return {"hardware_alerts": _hw_alerts_payload()}




@router.post("/api/refresh-webui")
async def api_refresh_webui() -> dict[str, Any]:
    """Download and apply the latest cs-webui frontend files (app.js, style.css, index.html)
    without a full reinstall or service restart.  The browser just needs a hard-refresh
    (Ctrl+Shift+R) after this returns to pick up the new files."""
    try:
        await asyncio.wait_for(refresh_webui_frontend(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Frontend refresh timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    local_ver: str | None = None
    try:
        local_ver = (STATIC_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return {"status": "ok", "version": local_ver, "message": f"Frontend updated to v{local_ver} — do a hard-refresh (Ctrl+Shift+R)"}




@router.post("/api/status")
async def api_status(status: ClientStatus) -> dict[str, Any]:
    payload, watchdog_changed, ignored = await _apply_client_status(status)
    if ignored is not None:
        return ignored
    await broadcast({"type": "status_update", "client": payload})
    if watchdog_changed:
        await _broadcast_proxmox_state()
    asyncio.create_task(_sync_sim_tags_for_client(status.hostname))
    return {"status": "ok", "client": payload, "throttle_interval": _server_pressure["throttle_interval"]}




@router.get("/api/health")
async def api_health() -> dict[str, Any]:
    return await _api_health_payload()




@router.get("/api/debug")
async def api_debug() -> dict[str, Any]:
    """Server-side debug event log for diagnosing connectivity issues."""
    import datetime as _dt
    return {
        "server_start": _server_start_time,
        "server_uptime_s": round(time.time() - _server_start_time),
        "proxmox_connected": proxmox_state.get("connected"),
        "proxmox_last_seen": proxmox_state.get("last_seen"),
        "relay_connected": relay_state.get("connected"),
        "relay_last_sync": relay_state.get("last_sync"),
        "relay_error": relay_state.get("error"),
        "events": list(reversed(_debug_log)),
    }




@router.get("/api/debug/command-trace")
async def api_debug_command_trace() -> dict[str, Any]:
    """Returns the last 300 command relay events for diagnosing hub→spoke→agent pipeline issues."""
    async with state_lock:
        cmds_snapshot = list(_serialize_commands())
    agent_connected = state.proxmox_ws_connection is not None
    return {
        "agent_connected": agent_connected,
        "agent_hostname": state.proxmox_ws_hostname,
        "command_queue": cmds_snapshot,
        "trace": list(reversed(_command_trace)),
    }




@router.get("/api/services/status")
async def api_services_status() -> dict[str, Any]:
    return {
        "tasks": service_health,
        "task_names": list(background_tasks.keys()),
    }




# ── System health & service control ───────────────────────────────────────────

@router.get("/api/system/health")
async def api_system_health(request: Request) -> dict[str, Any]:
    """LXC host resource snapshot + service status + Proxmox install command."""
    import shutil as _shutil

    # Disk
    try:
        disk = _shutil.disk_usage(BASE_DIR)
        disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
    except Exception:
        disk_info = {"total": 0, "used": 0, "free": 0}

    # Memory via /proc/meminfo
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                try:
                    mem[k.strip()] = int(v.strip().split()[0])
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_info = {"total_kb": mem_total, "available_kb": mem_avail,
                "used_kb": mem_total - mem_avail}

    # Load average
    try:
        load_parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        load = load_parts[:3]
    except Exception:
        load = ["?", "?", "?"]

    # Uptime seconds
    try:
        uptime_secs = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        uptime_secs = 0.0

    # Service active state
    try:
        proc = await asyncio.create_subprocess_shell(
            "systemctl is-active client-sim-dashboard 2>/dev/null || echo inactive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        svc_status = stdout.decode().strip()
    except Exception:
        svc_status = "unknown"

    # Pre-built Proxmox agent install command
    base = str(request.base_url).rstrip("/")
    raw_base = REPO_URL.replace(".git", "").replace(
        "github.com", "raw.githubusercontent.com"
    )
    branch = os.environ.get("REPO_BRANCH", "main")
    install_cmd = (
        f"bash <(curl -sSL {raw_base}/{branch}/proxmox/install-proxmox-agent.sh)"
        f" --server {base}"
    )

    return {
        "disk": disk_info,
        "memory": mem_info,
        "load": load,
        "uptime_secs": uptime_secs,
        "service_status": svc_status,
        "proxmox_install_cmd": install_cmd,
    }




@router.post("/api/service/{action}")
async def api_service_control(action: str) -> dict[str, Any]:
    """Start, stop, or restart the client-sim-dashboard service."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action must be start, stop, or restart")
    try:
        proc = await asyncio.create_subprocess_shell(
            f"sudo -n systemctl {action} client-sim-dashboard",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        return {"status": "timeout",
                "message": f"systemctl {action} timed out — service may be restarting"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    if rc != 0:
        return {"status": "error",
                "message": stderr.decode().strip() or f"exit code {rc}"}
    return {"status": "ok", "message": f"Service {action} sent"}




# ── Cache-clear endpoints ──────────────────────────────────────────────────────

@router.post("/api/server/clear-cache")
async def api_server_clear_cache() -> dict[str, Any]:
    """Reset all server-side in-memory state (Proxmox, reclone, commands, update-all).
    Does not restart the service — the UI will receive fresh empty state via WS broadcast."""
    async with state_lock:
        proxmox_state.update({
            "connected": False, "last_seen": None, "node": {}, "vms": [],
            "unknown_usb": [], "usb_state": [], "present_usb": [],
            "agent_version": None, "pve_version": None, "template_lock": "",
            "prov_summary": None, "prov_run": _default_provision_run_state(),
        })
        state._prev_usb_by_vmid = {}  # clear transition-detection snapshot so no phantom "failed" on next telemetry
        proxmox_log_buffer.clear()
        pending_proxmox_agents.clear()
        commands.clear()
        await _async_save_commands()
        reclone_state.update({
            "status": "idle", "type": None, "total": 0, "completed": 0,
            "failed": 0, "current_vm": None, "log": [], "auto_recovery_log": [],
            "last_run": None, "started_at": None,
        })
        state.update_all_state.update({
            "running": False, "phase": "idle", "total_agents": 0,
            "completed_agents": 0, "failed_agents": 0, "agent_cmds": [],
            "started_at": None, "error": None,
        })

    await broadcast({"type": "proxmox_update", **_proxmox_status_payload()})
    await _broadcast_reclone_state()
    await broadcast({"type": "update_all_progress", **state.update_all_state})
    await broadcast({"type": "commands_update", "commands": []})
    logger.info("Server cache cleared by user request")
    return {"status": "ok", "message": "Server cache cleared"}




@router.get("/api/kill-switch", response_class=PlainTextResponse)
async def api_kill_switch() -> str:
    """Return the current global kill switch value ('on' or 'off').
    Clients should poll this as their primary source — always fetched from
    solutions-hpe/main so no fork can override it."""
    return gkill_switch_state["value"]




@router.get("/api/kill-switch/status")
async def api_kill_switch_status() -> dict[str, Any]:
    """Return full gkill_switch state for the WebUI dashboard."""
    return {
        "value": gkill_switch_state["value"],
        "last_fetched": gkill_switch_state["last_fetched"],
        "error": gkill_switch_state["error"],
    }
