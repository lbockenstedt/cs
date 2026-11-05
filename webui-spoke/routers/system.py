"""System API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# System helpers, paths and shared state live in server.py; this file only defines routes.
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

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/local-wsites")
async def api_local_wsites() -> dict[str, Any]:
    """Extract unique wsite values from simulation.conf in the repo."""
    # Imported here because only this endpoint needs it.
    import configparser
    # No config file means no sites.
    config_path = repo_path("configs", "simulation.conf")
    if not config_path.exists():
        return {"wsites": []}
    # Parse the file; a broken file is logged and treated as empty.
    parser = configparser.ConfigParser()
    try:
        parser.read_string(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not parse simulation.conf: %s", exc)
        return {"wsites": []}
    # Collect each section's non-empty wsite value, without duplicates.
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
    # Download the new frontend files, allowing up to 60 s.
    try:
        await asyncio.wait_for(refresh_webui_frontend(), timeout=60)
    except asyncio.TimeoutError:
        # Too slow: report a gateway timeout.
        raise HTTPException(status_code=504, detail="Frontend refresh timed out")
    except Exception as exc:
        # Any other failure: report it as a server error.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Read the version now installed; it stays None if the file is missing.
    local_ver: str | None = None
    try:
        local_ver = (STATIC_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return {"status": "ok", "version": local_ver, "message": f"Frontend updated to v{local_ver} — do a hard-refresh (Ctrl+Shift+R)"}




@router.post("/api/status")
async def api_status(status: ClientStatus) -> dict[str, Any]:
    """Accept a status heartbeat from a client, store it and notify UIs."""
    # Apply the heartbeat; an ignored client gets the returned reply instead.
    payload, watchdog_changed, ignored = await _apply_client_status(status)
    if ignored is not None:
        return ignored
    # Tell UIs about the update, plus Proxmox state if the watchdog changed.
    await broadcast({"type": "status_update", "client": payload})
    if watchdog_changed:
        await _broadcast_proxmox_state()
    # Sync the client's simulation tags in the background.
    asyncio.create_task(_sync_sim_tags_for_client(status.hostname))
    # Reply with the current throttle interval so busy servers can slow clients down.
    return {"status": "ok", "client": payload, "throttle_interval": _server_pressure["throttle_interval"]}




@router.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Return the server health summary."""
    return await _api_health_payload()




@router.get("/api/debug")
async def api_debug() -> dict[str, Any]:
    """Server-side debug event log for diagnosing connectivity issues."""
    # Local alias, kept from the original code.
    import datetime as _dt
    # Uptime and connection state for Proxmox and relay, with newest events first.
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
    # Copy the command queue under the lock.
    async with state_lock:
        cmds_snapshot = list(_serialize_commands())
    # The agent counts as connected while its websocket is open.
    agent_connected = state.proxmox_ws_connection is not None
    return {
        "agent_connected": agent_connected,
        "agent_hostname": state.proxmox_ws_hostname,
        "command_queue": cmds_snapshot,
        "trace": list(reversed(_command_trace)),
    }




@router.get("/api/services/status")
async def api_services_status() -> dict[str, Any]:
    """Return background task health and the names of running tasks."""
    return {
        "tasks": service_health,
        "task_names": list(background_tasks.keys()),
    }




# ── System health & service control ───────────────────────────────────────────

@router.get("/api/system/health")
async def api_system_health(request: Request) -> dict[str, Any]:
    """LXC host resource snapshot + service status + Proxmox install command."""
    # Local alias for shutil.
    import shutil as _shutil

    # Disk
    # Disk usage of the install directory (zeros on error).
    try:
        disk = _shutil.disk_usage(BASE_DIR)
        disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
    except Exception:
        disk_info = {"total": 0, "used": 0, "free": 0}

    # Memory via /proc/meminfo
    mem: dict[str, int] = {}
    # Parse "Key: value kB" lines; lines that do not parse are skipped.
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
    # Used memory is total minus available.
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_info = {"total_kb": mem_total, "available_kb": mem_avail,
                "used_kb": mem_total - mem_avail}

    # Load average
    # First three load average values ("?" if unreadable).
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
    # Ask systemd whether the dashboard service is running (5 s limit).
    try:
        proc = await asyncio.create_subprocess_shell(
            "systemctl is-active client-sim-dashboard 2>/dev/null || echo inactive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        svc_status = stdout.decode().strip()
    except Exception:
        # Any failure means we do not know.
        svc_status = "unknown"

    # Pre-built Proxmox agent install command
    # Build the install command from this server's URL and the raw GitHub repo URL.
    base = str(request.base_url).rstrip("/")
    raw_base = REPO_URL.replace(".git", "").replace(
        "github.com", "raw.githubusercontent.com"
    )
    branch = os.environ.get("REPO_BRANCH", "main")
    install_cmd = (
        f"bash <(curl -sSL {raw_base}/{branch}/proxmox/install-proxmox-agent.sh)"
        f" --server {base}"
    )

    # Return everything as one snapshot.
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
    # Only these three actions are allowed.
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action must be start, stop, or restart")
    # Run systemctl through passwordless sudo (15 s limit).
    try:
        proc = await asyncio.create_subprocess_shell(
            f"sudo -n systemctl {action} client-sim-dashboard",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        # Timing out is normal for restart, since this process may be the one restarting.
        return {"status": "timeout",
                "message": f"systemctl {action} timed out — service may be restarting"}
    except Exception as exc:
        # Any other failure: report the error.
        return {"status": "error", "message": str(exc)}

    # A non-zero exit is an error; show stderr or the exit code.
    if rc != 0:
        return {"status": "error",
                "message": stderr.decode().strip() or f"exit code {rc}"}
    return {"status": "ok", "message": f"Service {action} sent"}




# ── Cache-clear endpoints ──────────────────────────────────────────────────────

@router.post("/api/server/clear-cache")
async def api_server_clear_cache() -> dict[str, Any]:
    """Reset all server-side in-memory state (Proxmox, reclone, commands, update-all).
    Does not restart the service — the UI will receive fresh empty state via WS broadcast."""
    # Reset everything under the lock so no request sees half-cleared state.
    async with state_lock:
        # Proxmox state back to "nothing known".
        proxmox_state.update({
            "connected": False, "last_seen": None, "node": {}, "vms": [],
            "unknown_usb": [], "usb_state": [], "present_usb": [],
            "agent_version": None, "pve_version": None, "template_lock": "",
            "prov_summary": None, "prov_run": _default_provision_run_state(),
        })
        state._prev_usb_by_vmid = {}  # clear transition-detection snapshot so no phantom "failed" on next telemetry
        # Drop logs, pending agents and the command queue (and save the empty queue).
        proxmox_log_buffer.clear()
        pending_proxmox_agents.clear()
        commands.clear()
        await _async_save_commands()
        # Reclone progress back to idle.
        reclone_state.update({
            "status": "idle", "type": None, "total": 0, "completed": 0,
            "failed": 0, "current_vm": None, "log": [], "auto_recovery_log": [],
            "last_run": None, "started_at": None,
        })
        # Update-all progress back to idle.
        state.update_all_state.update({
            "running": False, "phase": "idle", "total_agents": 0,
            "completed_agents": 0, "failed_agents": 0, "agent_cmds": [],
            "started_at": None, "error": None,
        })

    # Push the empty state to every UI and log the action.
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
