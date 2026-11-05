"""Proxmox API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Proxmox helpers, constants and shared state live in server.py; this file only defines routes.
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

# The router object every endpoint below is registered on.
router = APIRouter()

# Shared keep-alive httpx client for outbound Proxmox API calls (the VNC
# vncproxy POST in api_create_console_session). A fresh AsyncClient per
# console-open paid a new connection pool + TLS handshake each time; a single
# process-lifetime client reuses the pool. Created lazily so a missing httpx
# (the `httpx is None` guard below) doesn't crash import.
_console_http_client = None


def _console_client():
    """Return the shared console HTTP client, creating it on first use (None if httpx is missing).
    """
    # Declared global because the client is created once and stored at module level.
    global _console_http_client
    # Proxmox usually has a self-signed cert, so TLS verification is disabled for these calls.
    if _console_http_client is None and httpx is not None:
        _console_http_client = httpx.AsyncClient(verify=False)
    return _console_http_client




@router.get("/api/proxmox/usb-config")
async def get_proxmox_usb_config(hostname: str | None = Query(default=None)) -> dict[str, Any]:
    """Return the USB provisioning settings for one agent (or the defaults when no hostname is
    given).
    """
    return _proxmox_usb_config_payload(hostname)




@router.post("/api/proxmox/reclone-all")
async def api_proxmox_reclone_all() -> dict[str, Any]:
    """Start a rolling reclone of every eligible VM (and provision unassigned dongles) in the
    background.
    """
    # Only one reclone run at a time.
    if reclone_state.get("status") == "running":
        raise HTTPException(status_code=409, detail="A reclone run is already in progress")
    # Work out which VMs can be recloned and which certified dongles have no VM yet.
    eligible = _reclone_targets_for_run()
    unassigned_dongles = _proxmox_unassigned_present_usb()
    # Nothing to do: explain why instead of starting an empty run.
    if not eligible and not unassigned_dongles:
        raise HTTPException(
            status_code=400,
            detail=(
                "No reclone-capable guests or unassigned certified USB devices were found. "
                "Guests without a USB mapping or LXC template source are skipped."
            ),
        )
    # Run the reclone in the background and return immediately with the counts.
    asyncio.create_task(_run_rolling_reclone("manual"))
    return {"status": "started", "vm_count": len(eligible), "unassigned_dongles": len(unassigned_dongles)}




@router.post("/api/proxmox/reclone-state/clear")
async def api_proxmox_reclone_state_clear() -> dict[str, Any]:
    """Clear a stale failed/interrupted reclone state, resetting to idle.
    The last_run summary is preserved so the UI can still show what happened."""
    # Clearing is not allowed while a run is active.
    status = reclone_state.get("status", "idle")
    if status == "running":
        raise HTTPException(status_code=409, detail="Cannot clear reclone state while a reclone is running")
    # Reset every progress field to its idle value.
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
    # Push the reset state to connected UIs.
    await _broadcast_reclone_state()
    return {"cleared": True, "previous_status": status}




@router.post("/api/proxmox/telemetry", response_model=None)
async def proxmox_telemetry(request: Request, body: dict = Body(...)) -> dict[str, bool] | JSONResponse:
    """Receive telemetry from the Proxmox host agent."""
    # Read the agent's hostname from the payload plus its key, source IP and receive time.
    node = body.get("node", {}) or {}
    hostname = str(node.get("hostname", "") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    # The hostname is how we identify the agent.
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)

    # Check the agent's key; a non-None response is an error/pending reply to send back.
    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, now)
    if response is not None:
        return response
    # Use the canonical key from approved_proxmox_agents so proxmox_states entries
    # are keyed consistently regardless of case or minor hostname format differences.
    canonical_hostname = _approved_hostname or hostname
    # Merge the telemetry into the spoke state.
    try:
        return await _apply_proxmox_telemetry_state(body, canonical_hostname, now)
    except Exception:
        # A crash while applying telemetry is logged with its traceback and surfaced in the agent
        # log.
        tb = traceback.format_exc()
        logger.error("TELEMETRY HANDLER CRASH for %s:\n%s", hostname, tb)
        last_line = tb.splitlines()[-1] if tb.splitlines() else "unknown"
        proxmox_log_buffer.append(f"[SPOKE ERROR] telemetry crash: {last_line}")
        # Tracing is best-effort and must never hide the original error.
        try:
            _trace("telemetry_crash", hostname=hostname, error=last_line)
        except Exception:
            pass
        # Re-raise so FastAPI still returns a 500 to the agent.
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
    # Hostname may be at the top level or inside node; also read the key and source IP.
    hostname = str(body.get("hostname") or body.get("node", {}).get("hostname") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    # The hostname is required.
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    # Check the agent's key; a non-None response is an error/pending reply to send back.
    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, time.time())
    if response is not None:
        return response
    # Keep the non-empty lines.
    canonical_hn = _approved_hostname or hostname
    new_lines = [str(ln) for ln in (body.get("log_lines") or []) if ln]
    # With several agents, prefix each line with the host name (unless already prefixed).
    if new_lines:
        if len(approved_proxmox_agents) > 1:
            new_lines = [f"[{canonical_hn}] {ln}" if not ln.startswith(f"[{canonical_hn}]") else ln for ln in new_lines]
        # Append to the shared buffer, trimming the oldest lines beyond the size limit.
        proxmox_log_buffer.extend(new_lines)
        if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
            del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
        # Stream the new lines to connected UIs.
        await broadcast({"type": "proxmox_log_update", "lines": new_lines})
    return {"ok": True, "accepted": len(new_lines)}




@router.post("/api/proxmox/watchdog_event")
async def proxmox_watchdog_event(body: dict = Body(...)) -> dict[str, bool]:
    """Record a watchdog event from an agent in the logs and, for network events, in hw_faults."""
    # Read the event fields; a dict detail is stored as JSON text.
    event = str(body.get("event", "") or "").strip()
    service = str(body.get("service", "") or "").strip()
    hostname = str(body.get("hostname", "") or "").strip()
    timestamp = str(body.get("timestamp", "") or "").strip()
    detail_raw = body.get("detail", "") or ""
    detail = json.dumps(detail_raw) if isinstance(detail_raw, dict) else str(detail_raw).strip()
    # failure_count must be a non-negative integer.
    try:
        failure_count = max(0, int(body.get("failure_count", 0) or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="failure_count must be an integer")

    # These three fields are required.
    if not all((event, hostname, timestamp)):
        raise HTTPException(status_code=400, detail="event, hostname, and timestamp are required")

    # Build the log entry, adding detail only when present.
    entry = {
        "event": event,
        "service": service,
        "hostname": hostname,
        "timestamp": timestamp,
        "failure_count": failure_count,
    }
    if detail:
        entry["detail"] = detail
    # Append to the watchdog log, trimming the oldest entries beyond the limit.
    proxmox_watchdog_log.append(entry)
    if len(proxmox_watchdog_log) > PROXMOX_WATCHDOG_LOG_MAX:
        del proxmox_watchdog_log[:len(proxmox_watchdog_log) - PROXMOX_WATCHDOG_LOG_MAX]

    # Also add a one-line summary to the general agent log (detail capped at 120 chars).
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

    # Stream the new line to connected UIs.
    await broadcast({"type": "proxmox_log_update", "lines": [log_line]})
    return {"ok": True}




@router.post("/api/proxmox/hw_reset_event")
async def proxmox_hw_reset_event(body: dict = Body(...)) -> dict[str, bool]:
    """Called by the proxmox agent immediately before triggering a hard reset.
    Stores the event so the hub learns about it even if the agent never sends
    another telemetry post after rebooting."""
    # Read the event fields from the request.
    hostname  = str(body.get("hostname", "") or "").strip()
    reason    = str(body.get("reason", "") or "").strip()
    tier      = str(body.get("tier", "") or "").strip()
    ts        = body.get("ts") or time.time()
    patterns  = body.get("patterns") or []
    agent_ver = str(body.get("agent_version", "") or "").strip()

    # Normalised record of the reset.
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
    # Only replace the stored reset if this one is newer.
    existing = proxmox_state.get("hw_last_reset") or {}
    if not existing or float(ts) >= existing.get("ts", 0):
        proxmox_state["hw_last_reset"] = record

    # Append to fault log
    # Keep only the newest 100 faults.
    hw_faults = proxmox_state.get("hw_faults") or {"faults": []}
    hw_faults.setdefault("faults", []).append({**record, "type": "pre_reboot_notification"})
    hw_faults["faults"] = hw_faults["faults"][-100:]
    proxmox_state["hw_faults"] = hw_faults

    # Add a summary line to the agent log, trimming it to the size limit.
    log_line = (
        f"[HW-RESET] {hostname} initiating hard reset — tier={tier} reason={reason[:160]}"
    )
    proxmox_log_buffer.append(log_line)
    if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
        del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]

    # Tell UIs about the reset and the new log line.
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
    """Return the current Proxmox state summary shown on the VM Server page."""
    return _proxmox_status_payload()




@router.get("/api/proxmox/config/{hostname}")
async def get_proxmox_host_config(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Return the per-host Proxmox settings (currently just the VM set override)."""
    # Match an approved agent name if possible, otherwise normalise the given name.
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    # Load the host's saved settings and return a validated override value.
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
    """Save the VM set override (0-99) for one Proxmox host and notify UIs."""
    # Match an approved agent name if possible, otherwise normalise the given name.
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    # The override must be a whole number between 0 and 99.
    try:
        vm_set_override = int(body.get("vm_set_override", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="vm_set_override must be an integer") from exc
    if vm_set_override < 0 or vm_set_override > 99:
        raise HTTPException(status_code=422, detail="vm_set_override must be between 0 and 99")
    # Persist the new value and log it.
    config_entry = _save_proxmox_host_config(resolved_hostname, {"vm_set_override": vm_set_override})
    logger.info("Proxmox VM set override saved for host %s: %s", resolved_hostname, config_entry.get("vm_set_override", 0) or 0)
    # Push refreshed settings to UIs and return the saved value.
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
    """Report whether a Proxmox API token is stored for this host or globally (never the token)."""
    # Resolve the host name, then check for a per-host token and the global fallback token.
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
    """Store a Proxmox API token for one host."""
    # Accept either field name; the token is required.
    token = str(body.get("proxmox_token") or body.get("proxmox_api_token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="proxmox_token is required")
    # Resolve the host name; it must not be empty.
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    # Save the token and log (without the token itself).
    _save_proxmox_token_for_host(resolved_hostname, token)
    logger.info("Proxmox API token saved for host %s", resolved_hostname)
    return {"ok": True, "hostname": resolved_hostname, "configured": True}




@router.post("/api/proxmox/token/{hostname}/auto-provision")
async def auto_provision_proxmox_host_token(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Create a "cs-hub" API token for root@pam on a Proxmox host and save it for that host.

    Uses pvesh directly when this app runs on that host, otherwise asks the host's agent.
    """
    # Resolve the host name; it must not be empty.
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    # Fixed token name and Proxmox user; the request id matches the agent's reply to this call.
    TOKEN_ID = "cs-hub"
    USER = "root@pam"
    request_id = str(uuid.uuid4())

    # Look for pvesh in PATH and the usual install locations.
    pvesh_candidates = [
        shutil.which("pvesh"),
        "/usr/bin/pvesh",
        "/usr/sbin/pvesh",
        "/usr/local/bin/pvesh",
        "/usr/share/pve-manager/bin/pvesh",
        "/opt/proxmox/bin/pvesh",
    ]
    pvesh_path = next((c for c in pvesh_candidates if c and os.path.isfile(c)), None)
    # Use local pvesh only if it exists and this machine is the requested host.
    local_candidates = [socket.gethostname(), socket.getfqdn(), os.environ.get("HOSTNAME", "")]
    use_local_pvesh = bool(
        pvesh_path and any(_proxmox_hostnames_match(resolved_hostname, candidate) for candidate in local_candidates if candidate)
    )

    # Remote host: ask its agent to create the token.
    if not use_local_pvesh:
        # The agent must be approved before we can send it commands.
        if resolved_hostname not in approved_proxmox_agents:
            raise HTTPException(status_code=404, detail="Proxmox agent not approved")
        # One-slot queue the agent's reply is delivered into, keyed by request id.
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        _proxmox_token_provision_queues[request_id] = q
        # Queue the command and wait up to 30 s for the reply.
        try:
            await _queue_proxmox_command(
                "create_proxmox_token",
                {"request_id": request_id},
                command_type="token-provision",
                target=resolved_hostname,
            )
            result = await asyncio.wait_for(q.get(), timeout=30.0)
            # Success: save the returned token for this host.
            if result.get("ok"):
                token = str(result.get("token") or "").strip()
                if not token:
                    raise HTTPException(status_code=500, detail="Agent returned an empty token")
                _save_proxmox_token_for_host(resolved_hostname, token)
                logger.info("Proxmox API token auto-provisioned via agent for host %s", resolved_hostname)
                return {"ok": True, "hostname": resolved_hostname, "token_id": f"{USER}!{TOKEN_ID}"}
            # The agent reported a failure.
            raise HTTPException(status_code=500, detail=str(result.get("error") or "Agent failed to provision token"))
        except asyncio.TimeoutError as exc:
            # The agent did not answer in time.
            raise HTTPException(status_code=504, detail="Proxmox agent did not respond within 30 seconds") from exc
        finally:
            # Always remove the reply queue.
            _proxmox_token_provision_queues.pop(request_id, None)

    # Local host: delete any old token with the same name first; errors (e.g. none exists) are
    # ignored.
    try:
        del_proc = await asyncio.create_subprocess_exec(
            pvesh_path, "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10.0)
    except Exception:
        pass

    # Create a fresh token without privilege separation and get JSON output (15 s limit).
    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh_path, "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0", "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="pvesh timed out after 15 seconds") from exc

    # pvesh failed: report its error text.
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pvesh failed: {stderr.decode().strip()[:200]}")

    # Parse the JSON and extract the token secret.
    try:
        data = json.loads(stdout.decode().strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse pvesh output: {exc}") from exc
    secret = str(data.get("value") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="pvesh returned no token value")

    # Proxmox token format is USER!TOKENID=SECRET; save it for this host.
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
    # The host running the VM (from the agent map, else the connected agent) and its API token.
    proxmox_host = str(_proxmox_agent_vm_map.get(vmid) or state.proxmox_ws_hostname or "").strip()
    api_token = _get_proxmox_token_for_host(proxmox_host)
    # Both the host and a token are needed.
    if not proxmox_host:
        raise HTTPException(status_code=503, detail="Proxmox host unknown — no agent connected")
    if not api_token:
        raise HTTPException(status_code=503, detail="Proxmox API token not configured on spoke")
    # Only QEMU VMs and LXC containers have consoles.
    normalized_vmtype = str(vmtype or "qemu").strip().lower()
    if normalized_vmtype not in {"qemu", "lxc"}:
        raise HTTPException(status_code=400, detail="vmtype must be qemu or lxc")
    # Proxmox node names are the short host name; build the vncproxy URL and auth header.
    node = proxmox_host.split(".")[0]
    vncproxy_url = f"https://{proxmox_host}:8006/api2/json/nodes/{node}/{normalized_vmtype}/{vmid}/vncproxy"
    auth_header = {"Authorization": f"PVEAPIToken={api_token}"}
    # httpx is optional; without it we cannot call Proxmox.
    if httpx is None:
        raise HTTPException(status_code=503, detail="httpx not installed")
    # Ask Proxmox for a websocket VNC ticket and port.
    try:
        client = _console_client()
        resp = await client.post(vncproxy_url, headers=auth_header, json={"websocket": 1}, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Proxmox vncproxy returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        ticket = body["data"]["ticket"]
        port = int(body["data"]["port"])
    except HTTPException:
        # Our own HTTP errors pass through unchanged.
        raise
    except Exception as exc:
        # Network or parsing failures become 502.
        raise HTTPException(status_code=502, detail=f"Proxmox vncproxy call failed: {exc}") from exc
    # Remember the session under a random id; the console websocket uses it and it expires later.
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
    """Queue an agent self-update for one host (or the default agent) and return the queued
    command.
    """
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
    """Queue a command telling the agent to unlock the VM template."""
    cmd = await _queue_unlock_template_command()
    return {
        "queued": True,
        "id": cmd["id"],
        "target": cmd["target"],
        "action": cmd.get("action"),
    }




@router.delete("/api/proxmox/vms/{vmid}")
async def api_proxmox_delete_vm(vmid: int) -> dict[str, Any]:
    """Queue deletion of a VM or container and mark it as pending delete in the UI."""
    # Work out the delete arguments (VM type, name, ...) from the current inventory.
    try:
        args = _prepare_delete_vm_args({"vmid": vmid})
    except HTTPException as exc:
        if exc.status_code == 404:
            # VM not in current Proxmox inventory — it may have been manually removed
            # from Proxmox directly.  Queue the delete anyway with a safe default so the
            # agent can confirm it is gone (idempotent) and update its state files.
            args = {"vmid": vmid, "vm_type": "qemu"}
        else:
            # Any other error is passed through.
            raise
    # Queue the delete for the host that owns this VM and mark it pending so the UI greys it out.
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
    # Hostname from the body or the X-Hostname header; it is required.
    hostname = str(body.get("hostname", "") or request.headers.get("X-Hostname", "")).strip()
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    # Source IP is shown to the admin when approving.
    client_ip = request.client.host if request.client else "unknown"

    # Already approved: drop any stale pending entry and return the agent's key.
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        if pending_hostname is not None:
            pending_proxmox_agents.pop(pending_hostname, None)
            await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})

    # Not approved: add/update the pending entry, tell UIs, and reply 202 "pending".
    now = time.time()
    _upsert_pending_proxmox_agent(hostname, client_ip, now)
    await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
    return JSONResponse({"pending": True}, status_code=202)




@router.get("/api/proxmox/key")
async def proxmox_get_key(hostname: str = Query(...)) -> JSONResponse:
    """Agent polls this until approved. Returns key when ready."""
    # Approved: return the key.
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})
    # Still pending: 202 tells the agent to keep polling.
    if _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) is not None:
        return JSONResponse({"pending": True}, status_code=202)
    # Never registered.
    return JSONResponse({"error": "unknown hostname"}, status_code=404)




@router.get("/api/proxmox/pending")
async def proxmox_pending_list() -> list[dict[str, Any]]:
    """List agents waiting for approval."""
    return _pending_proxmox_payload()




@router.post("/api/proxmox/approve/{hostname}")
async def proxmox_approve(hostname: str) -> dict[str, Any]:
    """Approve a pending agent (or return the key of an already-approved one)."""
    # Decide what to broadcast after leaving the lock.
    pending_payload: list[dict[str, Any]] | None = None
    should_broadcast_state = False
    # Change the agent tables under the shared lock.
    async with state_lock:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
        # Already approved: just clear any pending duplicate and return the existing key.
        if approved_hostname is not None:
            if pending_hostname is not None:
                pending_proxmox_agents.pop(pending_hostname, None)
                pending_payload = _pending_proxmox_payload()
                should_broadcast_state = True
            result = {"approved": True, "hostname": approved_hostname, "key": approved_proxmox_agents[approved_hostname], "existing": True}
        else:
            # New approval: resolve the name, which must not be empty.
            resolved_hostname = pending_hostname or _normalize_proxmox_hostname(hostname)
            if not resolved_hostname:
                raise HTTPException(status_code=400, detail="hostname is required")

            # Generate a random key, store it, remove the pending entry and persist the approvals.
            key = str(uuid.uuid4())
            approved_proxmox_agents[resolved_hostname] = key
            pending_proxmox_agents.pop(pending_hostname or resolved_hostname, None)
            settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
            _save_settings()
            pending_payload = _pending_proxmox_payload()
            should_broadcast_state = True
            result = {"approved": True, "hostname": resolved_hostname, "key": key}

    # Notify UIs outside the lock.
    if pending_payload is not None:
        await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    if should_broadcast_state:
        await _broadcast_proxmox_state()
    return result




@router.post("/api/proxmox/reject/{hostname}")
async def proxmox_reject(hostname: str) -> dict[str, Any]:
    """Reject a pending agent by removing it from the pending list."""
    # Remove the entry under the shared lock and snapshot the pending list.
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        pending_proxmox_agents.pop(resolved_hostname, None)
        pending_payload = _pending_proxmox_payload()
    # Notify UIs.
    await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    await _broadcast_proxmox_state()
    return {"rejected": True, "hostname": resolved_hostname}




@router.delete("/api/proxmox/approved/{hostname}")
async def proxmox_revoke(hostname: str) -> dict[str, Any]:
    """Revoke an approved agent's key."""
    # Remove the agent's key and persist the change under the shared lock.
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        approved_proxmox_agents.pop(resolved_hostname, None)
        settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
        _save_settings()
    # Notify UIs.
    await _broadcast_proxmox_state()
    return {"revoked": True, "hostname": resolved_hostname}




@router.get("/api/proxmox/approved")
async def proxmox_approved_list() -> list[dict[str, Any]]:
    """List approved agents."""
    return _approved_proxmox_payload()




@router.post("/api/proxmox/autoprov/reset")
async def api_autoprov_reset() -> dict[str, Any]:
    """Reset auto-provisioning run state and summary without clearing all server state.
    Use when the provisioning panel is stuck showing in-progress after completion."""
    # Reset under the shared lock.
    async with state_lock:
        proxmox_state["prov_run"] = _default_provision_run_state()
        proxmox_state["prov_summary"] = None
    logger.info("Auto-provisioning status manually reset via API")
    return {"ok": True}
