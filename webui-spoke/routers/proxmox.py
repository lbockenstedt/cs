"""Proxmox API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    Body,
    Depends,
    HTTPException,
    JSONResponse,
    PROXMOX_LOG_MAX,
    PROXMOX_WATCHDOG_LOG_MAX,
    Query,
    Request,
    SpokeUser,
    _DIRECT_CONSOLE_TTL,
    _apply_proxmox_telemetry_state,
    _approved_proxmox_payload,
    _authorize_proxmox_agent,
    _broadcast_proxmox_state,
    _broadcast_reclone_state,
    _default_provision_run_state,
    _direct_console_sessions,
    _get_proxmox_host_config,
    _get_proxmox_token_for_host,
    _normalize_proxmox_hostname,
    _pending_delete_vmids,
    _pending_proxmox_payload,
    _prepare_delete_vm_args,
    _proxmox_agent_vm_map,
    _proxmox_hostnames_match,
    _proxmox_status_payload,
    _proxmox_token_provision_queues,
    _proxmox_unassigned_present_usb,
    _proxmox_usb_config_payload,
    _public_settings,
    _queue_proxmox_agent_update,
    _queue_proxmox_command,
    _queue_unlock_template_command,
    _reclone_targets_for_run,
    _resolve_proxmox_agent_hostname,
    _resolve_proxmox_vm_target,
    _run_rolling_reclone,
    _sanitize_vm_set_override,
    _save_proxmox_host_config,
    _save_proxmox_token_for_host,
    _save_settings,
    _trace,
    _upsert_pending_proxmox_agent,
    approved_proxmox_agents,
    asyncio,
    broadcast,
    httpx,
    json,
    logger,
    os,
    pending_proxmox_agents,
    proxmox_log_buffer,
    proxmox_state,
    proxmox_watchdog_log,
    reclone_state,
    require_auth,
    settings,
    shutil,
    socket,
    state,
    state_lock,
    time,
    traceback,
    uuid,
)

router = APIRouter()

# Shared keep-alive httpx client for outbound Proxmox API calls (the VNC
# vncproxy POST in api_create_console_session). A fresh AsyncClient per
# console-open paid a new connection pool + TLS handshake each time; a single
# process-lifetime client reuses the pool. Created lazily so a missing httpx
# (the `httpx is None` guard below) doesn't crash import.
_console_http_client = None


def _console_client():
    global _console_http_client
    if _console_http_client is None and httpx is not None:
        _console_http_client = httpx.AsyncClient(verify=False)
    return _console_http_client




@router.get("/api/proxmox/usb-config")
async def get_proxmox_usb_config(hostname: str | None = Query(default=None)) -> dict[str, Any]:
    return _proxmox_usb_config_payload(hostname)




@router.post("/api/proxmox/reclone-all")
async def api_proxmox_reclone_all() -> dict[str, Any]:
    if reclone_state.get("status") == "running":
        raise HTTPException(status_code=409, detail="A reclone run is already in progress")
    eligible = _reclone_targets_for_run()
    unassigned_dongles = _proxmox_unassigned_present_usb()
    if not eligible and not unassigned_dongles:
        raise HTTPException(
            status_code=400,
            detail=(
                "No reclone-capable guests or unassigned certified USB devices were found. "
                "Guests without a USB mapping or LXC template source are skipped."
            ),
        )
    asyncio.create_task(_run_rolling_reclone("manual"))
    return {"status": "started", "vm_count": len(eligible), "unassigned_dongles": len(unassigned_dongles)}




@router.post("/api/proxmox/reclone-state/clear")
async def api_proxmox_reclone_state_clear() -> dict[str, Any]:
    """Clear a stale failed/interrupted reclone state, resetting to idle.
    The last_run summary is preserved so the UI can still show what happened."""
    status = reclone_state.get("status", "idle")
    if status == "running":
        raise HTTPException(status_code=409, detail="Cannot clear reclone state while a reclone is running")
    reclone_state.update({
        "status": "idle",
        "type": None,
        "total": 0,
        "completed": 0,
        "failed": 0,
        "current_vm": None,
        "log": [],
        "started_at": None,
        "last_run": None,
        "auto_recovery_log": [],
    })
    await _broadcast_reclone_state()
    return {"cleared": True, "previous_status": status}




@router.post("/api/proxmox/telemetry", response_model=None)
async def proxmox_telemetry(request: Request, body: dict = Body(...)) -> dict[str, bool] | JSONResponse:
    """Receive telemetry from the Proxmox host agent."""
    node = body.get("node", {}) or {}
    hostname = str(node.get("hostname", "") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)

    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, now)
    if response is not None:
        return response
    # Use the canonical key from approved_proxmox_agents so proxmox_states entries
    # are keyed consistently regardless of case or minor hostname format differences.
    canonical_hostname = _approved_hostname or hostname
    try:
        return await _apply_proxmox_telemetry_state(body, canonical_hostname, now)
    except Exception:
        tb = traceback.format_exc()
        logger.error("TELEMETRY HANDLER CRASH for %s:\n%s", hostname, tb)
        last_line = tb.splitlines()[-1] if tb.splitlines() else "unknown"
        proxmox_log_buffer.append(f"[SPOKE ERROR] telemetry crash: {last_line}")
        try:
            _trace("telemetry_crash", hostname=hostname, error=last_line)
        except Exception:
            pass
        raise





@router.get("/api/proxmox/logs")
async def get_proxmox_logs() -> dict[str, Any]:
    """Return the in-memory agent log ring buffer."""
    return {"lines": proxmox_log_buffer}




@router.post("/api/proxmox/logs/clear")
async def clear_proxmox_logs() -> dict[str, bool]:
    """Clear the in-memory agent log buffer."""
    proxmox_log_buffer.clear()
    await broadcast({"type": "proxmox_log_update", "lines": [], "cleared": True})
    return {"ok": True}




@router.post("/api/proxmox/log-push", response_model=None)
async def proxmox_log_push(request: Request, body: dict = Body(...)) -> dict[str, bool] | JSONResponse:
    """Lightweight HTTP log-push endpoint — agent sends log lines here even when WS is unavailable.
    Accepts: {"hostname": "...", "log_lines": ["line1", ...]}
    Auth: X-API-Key header (same as telemetry endpoint).
    """
    hostname = str(body.get("hostname") or body.get("node", {}).get("hostname") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, time.time())
    if response is not None:
        return response
    canonical_hn = _approved_hostname or hostname
    new_lines = [str(ln) for ln in (body.get("log_lines") or []) if ln]
    if new_lines:
        if len(approved_proxmox_agents) > 1:
            new_lines = [f"[{canonical_hn}] {ln}" if not ln.startswith(f"[{canonical_hn}]") else ln for ln in new_lines]
        proxmox_log_buffer.extend(new_lines)
        if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
            del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
        await broadcast({"type": "proxmox_log_update", "lines": new_lines})
    return {"ok": True, "accepted": len(new_lines)}




@router.post("/api/proxmox/watchdog_event")
async def proxmox_watchdog_event(body: dict = Body(...)) -> dict[str, bool]:
    event = str(body.get("event", "") or "").strip()
    service = str(body.get("service", "") or "").strip()
    hostname = str(body.get("hostname", "") or "").strip()
    timestamp = str(body.get("timestamp", "") or "").strip()
    detail_raw = body.get("detail", "") or ""
    detail = json.dumps(detail_raw) if isinstance(detail_raw, dict) else str(detail_raw).strip()
    try:
        failure_count = max(0, int(body.get("failure_count", 0) or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="failure_count must be an integer")

    if not all((event, hostname, timestamp)):
        raise HTTPException(status_code=400, detail="event, hostname, and timestamp are required")

    entry = {
        "event": event,
        "service": service,
        "hostname": hostname,
        "timestamp": timestamp,
        "failure_count": failure_count,
    }
    if detail:
        entry["detail"] = detail
    proxmox_watchdog_log.append(entry)
    if len(proxmox_watchdog_log) > PROXMOX_WATCHDOG_LOG_MAX:
        del proxmox_watchdog_log[:len(proxmox_watchdog_log) - PROXMOX_WATCHDOG_LOG_MAX]

    detail_suffix = f" detail={detail[:120]}" if detail else ""
    log_line = (
        f"[{timestamp}] WATCHDOG event={event} service={service} "
        f"hostname={hostname} failure_count={failure_count}{detail_suffix}"
    )
    proxmox_log_buffer.append(log_line)
    if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
        del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]

    # For network-related events and startup, also store in hw_faults so they surface in the hub panel
    if event in {"net_reboot", "net_down", "watchdog_started"}:
        hw_faults = proxmox_state.get("hw_faults") or {"faults": []}
        hw_faults.setdefault("faults", []).append({
            "type": event,
            "check": "network_watchdog" if event != "watchdog_started" else "watchdog_startup",
            "message": detail or f"Gateway unreachable — {event}" if event != "watchdog_started" else f"Watchdog started — boot_time={detail_raw.get('boot_time','?') if isinstance(detail_raw, dict) else '?'}",
            "hostname": hostname,
            "ts": timestamp,
        })
        hw_faults["faults"] = hw_faults["faults"][-100:]
        proxmox_state["hw_faults"] = hw_faults

    await broadcast({"type": "proxmox_log_update", "lines": [log_line]})
    return {"ok": True}




@router.post("/api/proxmox/hw_reset_event")
async def proxmox_hw_reset_event(body: dict = Body(...)) -> dict[str, bool]:
    """Called by the proxmox agent immediately before triggering a hard reset.
    Stores the event so the hub learns about it even if the agent never sends
    another telemetry post after rebooting."""
    hostname  = str(body.get("hostname", "") or "").strip()
    reason    = str(body.get("reason", "") or "").strip()
    tier      = str(body.get("tier", "") or "").strip()
    ts        = body.get("ts") or time.time()
    patterns  = body.get("patterns") or []
    agent_ver = str(body.get("agent_version", "") or "").strip()

    record = {
        "ts": ts,
        "hostname": hostname,
        "reason": reason,
        "tier": tier,
        "patterns": patterns,
        "agent_version": agent_ver,
        "source": "pre_reboot_notification",
    }

    # Store as last reset so the relay includes it immediately
    existing = proxmox_state.get("hw_last_reset") or {}
    if not existing or float(ts) >= existing.get("ts", 0):
        proxmox_state["hw_last_reset"] = record

    # Append to fault log
    hw_faults = proxmox_state.get("hw_faults") or {"faults": []}
    hw_faults.setdefault("faults", []).append({**record, "type": "pre_reboot_notification"})
    hw_faults["faults"] = hw_faults["faults"][-100:]
    proxmox_state["hw_faults"] = hw_faults

    log_line = (
        f"[HW-RESET] {hostname} initiating hard reset — tier={tier} reason={reason[:160]}"
    )
    proxmox_log_buffer.append(log_line)
    if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
        del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]

    await broadcast({
        "type": "proxmox_hw_reset",
        "hostname": hostname,
        "reason": reason,
        "tier": tier,
        "patterns": patterns,
        "ts": ts,
        "agent_version": agent_ver,
    })
    await broadcast({"type": "proxmox_log_update", "lines": [log_line]})
    return {"ok": True}




@router.get("/api/proxmox/status")
async def get_proxmox_status() -> dict[str, Any]:
    return _proxmox_status_payload()




@router.get("/api/proxmox/config/{hostname}")
async def get_proxmox_host_config(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    host_config = _get_proxmox_host_config(resolved_hostname)
    return {
        "hostname": resolved_hostname,
        "vm_set_override": _sanitize_vm_set_override(host_config.get("vm_set_override", 0)),
    }




@router.put("/api/proxmox/config/{hostname}")
async def save_proxmox_host_config(
    hostname: str,
    body: dict[str, Any] = Body(...),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    try:
        vm_set_override = int(body.get("vm_set_override", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="vm_set_override must be an integer") from exc
    if vm_set_override < 0 or vm_set_override > 99:
        raise HTTPException(status_code=422, detail="vm_set_override must be between 0 and 99")
    config_entry = _save_proxmox_host_config(resolved_hostname, {"vm_set_override": vm_set_override})
    logger.info("Proxmox VM set override saved for host %s: %s", resolved_hostname, config_entry.get("vm_set_override", 0) or 0)
    settings_payload = _public_settings()
    await broadcast({"type": "settings_update", "settings": settings_payload})
    return {
        "ok": True,
        "hostname": resolved_hostname,
        "vm_set_override": _sanitize_vm_set_override(config_entry.get("vm_set_override", 0)),
    }




@router.get("/api/proxmox/token/{hostname}")
async def get_proxmox_host_token_status(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    per_host = str((settings.get("proxmox_tokens") or {}).get(resolved_hostname, "") or "").strip()
    global_tok = _get_proxmox_token_for_host(None)
    return {
        "hostname": resolved_hostname,
        "configured": bool(per_host),
        "global_configured": bool(global_tok),
    }




@router.put("/api/proxmox/token/{hostname}")
async def save_proxmox_host_token(
    hostname: str,
    body: dict[str, Any] = Body(...),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    token = str(body.get("proxmox_token") or body.get("proxmox_api_token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="proxmox_token is required")
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    _save_proxmox_token_for_host(resolved_hostname, token)
    logger.info("Proxmox API token saved for host %s", resolved_hostname)
    return {"ok": True, "hostname": resolved_hostname, "configured": True}




@router.post("/api/proxmox/token/{hostname}/auto-provision")
async def auto_provision_proxmox_host_token(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    TOKEN_ID = "cs-hub"
    USER = "root@pam"
    request_id = str(uuid.uuid4())

    pvesh_candidates = [
        shutil.which("pvesh"),
        "/usr/bin/pvesh",
        "/usr/sbin/pvesh",
        "/usr/local/bin/pvesh",
        "/usr/share/pve-manager/bin/pvesh",
        "/opt/proxmox/bin/pvesh",
    ]
    pvesh_path = next((c for c in pvesh_candidates if c and os.path.isfile(c)), None)
    local_candidates = [socket.gethostname(), socket.getfqdn(), os.environ.get("HOSTNAME", "")]
    use_local_pvesh = bool(
        pvesh_path and any(_proxmox_hostnames_match(resolved_hostname, candidate) for candidate in local_candidates if candidate)
    )

    if not use_local_pvesh:
        if resolved_hostname not in approved_proxmox_agents:
            raise HTTPException(status_code=404, detail="Proxmox agent not approved")
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        _proxmox_token_provision_queues[request_id] = q
        try:
            await _queue_proxmox_command(
                "create_proxmox_token",
                {"request_id": request_id},
                command_type="token-provision",
                target=resolved_hostname,
            )
            result = await asyncio.wait_for(q.get(), timeout=30.0)
            if result.get("ok"):
                token = str(result.get("token") or "").strip()
                if not token:
                    raise HTTPException(status_code=500, detail="Agent returned an empty token")
                _save_proxmox_token_for_host(resolved_hostname, token)
                logger.info("Proxmox API token auto-provisioned via agent for host %s", resolved_hostname)
                return {"ok": True, "hostname": resolved_hostname, "token_id": f"{USER}!{TOKEN_ID}"}
            raise HTTPException(status_code=500, detail=str(result.get("error") or "Agent failed to provision token"))
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Proxmox agent did not respond within 30 seconds") from exc
        finally:
            _proxmox_token_provision_queues.pop(request_id, None)

    try:
        del_proc = await asyncio.create_subprocess_exec(
            pvesh_path, "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10.0)
    except Exception:
        pass

    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh_path, "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0", "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="pvesh timed out after 15 seconds") from exc

    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pvesh failed: {stderr.decode().strip()[:200]}")

    try:
        data = json.loads(stdout.decode().strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse pvesh output: {exc}") from exc
    secret = str(data.get("value") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="pvesh returned no token value")

    full_token = f"{USER}!{TOKEN_ID}={secret}"
    _save_proxmox_token_for_host(resolved_hostname, full_token)
    logger.info("Proxmox API token auto-provisioned locally for host %s", resolved_hostname)
    return {"ok": True, "hostname": resolved_hostname, "token_id": f"{USER}!{TOKEN_ID}"}




@router.post("/api/proxmox/console/{vmid}")
async def api_create_console_session(
    vmid: int,
    vmtype: str = Query("qemu"),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Create a direct Proxmox VNC console session for the spoke's own VM Server view."""
    proxmox_host = str(_proxmox_agent_vm_map.get(vmid) or state.proxmox_ws_hostname or "").strip()
    api_token = _get_proxmox_token_for_host(proxmox_host)
    if not proxmox_host:
        raise HTTPException(status_code=503, detail="Proxmox host unknown — no agent connected")
    if not api_token:
        raise HTTPException(status_code=503, detail="Proxmox API token not configured on spoke")
    normalized_vmtype = str(vmtype or "qemu").strip().lower()
    if normalized_vmtype not in {"qemu", "lxc"}:
        raise HTTPException(status_code=400, detail="vmtype must be qemu or lxc")
    node = proxmox_host.split(".")[0]
    vncproxy_url = f"https://{proxmox_host}:8006/api2/json/nodes/{node}/{normalized_vmtype}/{vmid}/vncproxy"
    auth_header = {"Authorization": f"PVEAPIToken={api_token}"}
    if httpx is None:
        raise HTTPException(status_code=503, detail="httpx not installed")
    try:
        client = _console_client()
        resp = await client.post(vncproxy_url, headers=auth_header, json={"websocket": 1}, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Proxmox vncproxy returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        ticket = body["data"]["ticket"]
        port = int(body["data"]["port"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxmox vncproxy call failed: {exc}") from exc
    session_id = str(uuid.uuid4())
    _direct_console_sessions[session_id] = {
        "proxmox_host": proxmox_host,
        "node": node,
        "vmid": vmid,
        "vmtype": normalized_vmtype,
        "api_token": api_token,
        "ticket": ticket,
        "port": port,
        "expires": time.time() + _DIRECT_CONSOLE_TTL,
    }
    return {"session_id": session_id, "expires_in": _DIRECT_CONSOLE_TTL}





@router.post("/api/proxmox/update-agent")
async def api_proxmox_update_agent(hostname: str | None = None) -> dict[str, Any]:
    cmd = await _queue_proxmox_agent_update(target=hostname or None)
    return {
        "queued": 1,
        "id": cmd["id"],
        "target": cmd["target"],
        "branch": cmd["args"].get("branch"),
        "source": cmd["args"].get("repo_raw"),
    }




@router.post("/api/proxmox/unlock-template")
async def api_proxmox_unlock_template() -> dict[str, Any]:
    cmd = await _queue_unlock_template_command()
    return {
        "queued": True,
        "id": cmd["id"],
        "target": cmd["target"],
        "action": cmd.get("action"),
    }




@router.delete("/api/proxmox/vms/{vmid}")
async def api_proxmox_delete_vm(vmid: int) -> dict[str, Any]:
    try:
        args = _prepare_delete_vm_args({"vmid": vmid})
    except HTTPException as exc:
        if exc.status_code == 404:
            # VM not in current Proxmox inventory — it may have been manually removed
            # from Proxmox directly.  Queue the delete anyway with a safe default so the
            # agent can confirm it is gone (idempotent) and update its state files.
            args = {"vmid": vmid, "vm_type": "qemu"}
        else:
            raise
    cmd = await _queue_proxmox_command("delete_vm", args, target=_resolve_proxmox_vm_target(vmid))
    _pending_delete_vmids.add(vmid)
    await _broadcast_proxmox_state()
    return {
        "queued": 1,
        "ids": [cmd["id"]],
        "vmid": vmid,
        "vm_type": args.get("vm_type"),
        "vm_name": args.get("vm_name"),
    }




@router.post("/api/proxmox/register")
async def proxmox_register(request: Request, body: dict = Body(...)) -> JSONResponse:
    """Called by agent with no key. Adds to pending if not approved."""
    hostname = str(body.get("hostname", "") or request.headers.get("X-Hostname", "")).strip()
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    client_ip = request.client.host if request.client else "unknown"

    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        if pending_hostname is not None:
            pending_proxmox_agents.pop(pending_hostname, None)
            await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})

    now = time.time()
    _upsert_pending_proxmox_agent(hostname, client_ip, now)
    await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
    return JSONResponse({"pending": True}, status_code=202)




@router.get("/api/proxmox/key")
async def proxmox_get_key(hostname: str = Query(...)) -> JSONResponse:
    """Agent polls this until approved. Returns key when ready."""
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})
    if _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) is not None:
        return JSONResponse({"pending": True}, status_code=202)
    return JSONResponse({"error": "unknown hostname"}, status_code=404)




@router.get("/api/proxmox/pending")
async def proxmox_pending_list() -> list[dict[str, Any]]:
    return _pending_proxmox_payload()




@router.post("/api/proxmox/approve/{hostname}")
async def proxmox_approve(hostname: str) -> dict[str, Any]:
    pending_payload: list[dict[str, Any]] | None = None
    should_broadcast_state = False
    async with state_lock:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
        if approved_hostname is not None:
            if pending_hostname is not None:
                pending_proxmox_agents.pop(pending_hostname, None)
                pending_payload = _pending_proxmox_payload()
                should_broadcast_state = True
            result = {"approved": True, "hostname": approved_hostname, "key": approved_proxmox_agents[approved_hostname], "existing": True}
        else:
            resolved_hostname = pending_hostname or _normalize_proxmox_hostname(hostname)
            if not resolved_hostname:
                raise HTTPException(status_code=400, detail="hostname is required")

            key = str(uuid.uuid4())
            approved_proxmox_agents[resolved_hostname] = key
            pending_proxmox_agents.pop(pending_hostname or resolved_hostname, None)
            settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
            _save_settings()
            pending_payload = _pending_proxmox_payload()
            should_broadcast_state = True
            result = {"approved": True, "hostname": resolved_hostname, "key": key}

    if pending_payload is not None:
        await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    if should_broadcast_state:
        await _broadcast_proxmox_state()
    return result




@router.post("/api/proxmox/reject/{hostname}")
async def proxmox_reject(hostname: str) -> dict[str, Any]:
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        pending_proxmox_agents.pop(resolved_hostname, None)
        pending_payload = _pending_proxmox_payload()
    await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    await _broadcast_proxmox_state()
    return {"rejected": True, "hostname": resolved_hostname}




@router.delete("/api/proxmox/approved/{hostname}")
async def proxmox_revoke(hostname: str) -> dict[str, Any]:
    """Revoke an approved agent's key."""
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        approved_proxmox_agents.pop(resolved_hostname, None)
        settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
        _save_settings()
    await _broadcast_proxmox_state()
    return {"revoked": True, "hostname": resolved_hostname}




@router.get("/api/proxmox/approved")
async def proxmox_approved_list() -> list[dict[str, Any]]:
    return _approved_proxmox_payload()




@router.post("/api/proxmox/autoprov/reset")
async def api_autoprov_reset() -> dict[str, Any]:
    """Reset auto-provisioning run state and summary without clearing all server state.
    Use when the provisioning panel is stuck showing in-progress after completion."""
    async with state_lock:
        proxmox_state["prov_run"] = _default_provision_run_state()
        proxmox_state["prov_summary"] = None
    logger.info("Auto-provisioning status manually reset via API")
    return {"ok": True}
