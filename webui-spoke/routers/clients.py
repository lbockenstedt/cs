"""Clients API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Helpers, models and shared state live in server.py; this file only defines the routes.
from server import (
    Any,
    Body,
    ClientControlResponse,
    HTTPException,
    Request,
    _ack_command_internal,
    _poll_agent_inbox,
    _require_shared_client_key,
    _resolve_proxmox_agent_hostname,
    _save_client_history,
    _send_email_notifications,
    _send_teams_notifications,
    _trace,
    approved_proxmox_agents,
    asyncio,
    broadcast,
    broadcast_full_state,
    clients,
    current_clients,
    logger,
    serialize_client,
    settings,
    state_lock,
    time,
)

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/client/key")
async def api_client_key() -> dict[str, str]:
    """Return the shared client API key so agents can authenticate to /ws/client.
    This endpoint is intentionally public — agents need the key before they can connect.
    The spoke URL itself acts as the first factor of access control."""
    # Return the key, or "" when none has been generated yet.
    return {"client_api_key": str(settings.get("client_api_key", "") or "")}




@router.get("/api/clients")
async def api_clients() -> list[dict[str, Any]]:
    """Return the current list of simulation clients and their status."""
    return await current_clients()




@router.delete("/api/clients/history")
async def api_purge_client_history() -> dict[str, Any]:
    """Purge all persisted client records (in-memory and on disk)."""
    # Clear the in-memory client table under the shared lock.
    async with state_lock:
        clients.clear()
    # Rewrite the history file (blocking I/O, so in a thread), then tell UIs the list was purged.
    await asyncio.to_thread(_save_client_history)
    await broadcast({"type": "clients_purged"})
    logger.info("Client history purged by user request")
    return {"status": "ok", "message": "Client history cleared"}




@router.post("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control(hostname: str, overrides: dict[str, str]) -> dict[str, Any]:
    """Merge setting overrides into one client's config so its next check-in picks them up."""
    # Overrides are stored as strings, matching the ini values clients read.
    normalized = {key: str(value) for key, value in overrides.items()}
    # Update the client under the shared lock; unknown hostnames are a 404.
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname].setdefault("overrides", {}).update(normalized)
        payload = serialize_client(hostname, clients[hostname])

    # Push the change to connected UIs.
    await broadcast({"type": "overrides_update", "client": payload})
    return {"hostname": hostname, "overrides": payload["overrides"], "client": payload}




@router.delete("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control_clear(hostname: str) -> dict[str, Any]:
    """Remove all setting overrides from one client."""
    # Reset the overrides under the shared lock; unknown hostnames are a 404.
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname]["overrides"] = {}
        payload = serialize_client(hostname, clients[hostname])

    # Push the change to connected UIs.
    await broadcast({"type": "overrides_cleared", "client": payload})
    return {"hostname": hostname, "overrides": {}, "client": payload}




@router.post("/api/clients/all/control")
async def api_all_clients_control(overrides: dict[str, str]) -> dict[str, Any]:
    """Apply the same setting overrides to every known client."""
    # Overrides are stored as strings, matching the ini values clients read.
    normalized = {key: str(value) for key, value in overrides.items()}
    # Merge into every client under the shared lock and count how many were updated.
    async with state_lock:
        for client in clients.values():
            client.setdefault("overrides", {}).update(normalized)
        updated = len(clients)

    # Many clients changed, so resend the whole state to UIs.
    await broadcast_full_state()
    return {"status": "ok", "updated": updated, "overrides": normalized}




@router.get("/api/inbox")
async def poll_inbox(request: Request, hostname: str) -> list[dict[str, Any]]:
    """Device polls for pending commands addressed to it. Marks them delivered."""
    # The hostname query parameter is required.
    if not hostname:
        raise HTTPException(status_code=422, detail="hostname is required")
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    # Proxmox agents send X-API-Key; simulation clients send X-Client-Key.
    api_key = request.headers.get("X-API-Key", "")
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    # It is an approved agent only if the hostname is approved and its key matches.
    is_approved_proxmox = (
        approved_hostname is not None
        and api_key == approved_proxmox_agents[approved_hostname]
    )
    # Not a proxmox agent: require the shared simulation client key instead (raises if wrong).
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox")
    elif api_key != approved_proxmox_agents[approved_hostname]:
        # Defensive re-check of the agent key.
        raise HTTPException(status_code=401, detail="invalid key")
    # Return pending commands for this host and mark them delivered.
    return await _poll_agent_inbox(hostname, approved_hostname)




@router.post("/api/inbox/ack")
async def ack_command(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    """Device reports command result."""
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    # Proxmox agents send X-API-Key; simulation clients send X-Client-Key.
    api_key = request.headers.get("X-API-Key", "")
    # The reporting host comes from a header or the body; used only for tracing.
    ack_hostname = request.headers.get("X-Hostname", "") or body.get("hostname", "")
    # Proxmox agent if the key matches any approved agent's key.
    is_approved_proxmox = any(
        api_key == v for v in approved_proxmox_agents.values()
    ) if api_key else False
    # Otherwise require the shared client key (raises if wrong) and label the sender generically.
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox/ack")
        ack_hostname = ack_hostname or "(sim-client)"
    # Record the command's final status.
    result = await _ack_command_internal(body)
    # Write a trace entry for troubleshooting command delivery.
    _trace("inbox_ack_received", hostname=ack_hostname or "(unknown)",
           cmd_id=str(body.get("id", "")), status=body.get("status", ""),
           message=str(body.get("message", ""))[:200])
    return result




@router.post("/api/notifications/test")
async def api_notifications_test(body: dict[str, Any]) -> dict[str, Any]:
    """Send a test notification via email or Teams."""
    # Which channel to test.
    channel = body.get("channel", "")  # "email" | "teams"
    # Start from saved notification settings.
    notif = dict(settings.get("notifications", {}))
    # Allow overriding with posted values (for unsaved fields)
    notif.update({k: v for k, v in body.items() if k != "channel"})

    # A fake "check went from ok to error" event to send through the chosen channel.
    test_transition = [{
        "check_type": "sim",
        "check_id": "test",
        "check_name": "Test Notification",
        "site": "test-site",
        "old": "ok",
        "new": "error",
        "ts": time.time(),
    }]

    # Send it through the chosen channel, validating that the required settings are present.
    try:
        if channel == "email":
            if not notif.get("smtp_host") or not notif.get("smtp_to"):
                raise HTTPException(status_code=422, detail="smtp_host and smtp_to are required")
            # SMTP is blocking, so send email in a worker thread.
            await asyncio.to_thread(_send_email_notifications, notif, test_transition)
        elif channel == "teams":
            url = notif.get("teams_webhook_url", "")
            if not url:
                raise HTTPException(status_code=422, detail="teams_webhook_url is required")
            await _send_teams_notifications(url, test_transition)
        else:
            # Unknown channel.
            raise HTTPException(status_code=422, detail="channel must be 'email' or 'teams'")
    except HTTPException:
        # Our own validation errors pass through unchanged.
        raise
    except Exception as exc:
        # Any other send failure becomes a 500 with the error text.
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "ok", "channel": channel}
