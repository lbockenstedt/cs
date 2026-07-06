"""Clients API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
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

router = APIRouter()




@router.get("/api/client/key")
async def api_client_key() -> dict[str, str]:
    """Return the shared client API key so agents can authenticate to /ws/client.
    This endpoint is intentionally public — agents need the key before they can connect.
    The spoke URL itself acts as the first factor of access control."""
    return {"client_api_key": str(settings.get("client_api_key", "") or "")}




@router.get("/api/clients")
async def api_clients() -> list[dict[str, Any]]:
    return await current_clients()




@router.delete("/api/clients/history")
async def api_purge_client_history() -> dict[str, Any]:
    """Purge all persisted client records (in-memory and on disk)."""
    async with state_lock:
        clients.clear()
    await asyncio.to_thread(_save_client_history)
    await broadcast({"type": "clients_purged"})
    logger.info("Client history purged by user request")
    return {"status": "ok", "message": "Client history cleared"}




@router.post("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control(hostname: str, overrides: dict[str, str]) -> dict[str, Any]:
    normalized = {key: str(value) for key, value in overrides.items()}
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname].setdefault("overrides", {}).update(normalized)
        payload = serialize_client(hostname, clients[hostname])

    await broadcast({"type": "overrides_update", "client": payload})
    return {"hostname": hostname, "overrides": payload["overrides"], "client": payload}




@router.delete("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control_clear(hostname: str) -> dict[str, Any]:
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname]["overrides"] = {}
        payload = serialize_client(hostname, clients[hostname])

    await broadcast({"type": "overrides_cleared", "client": payload})
    return {"hostname": hostname, "overrides": {}, "client": payload}




@router.post("/api/clients/all/control")
async def api_all_clients_control(overrides: dict[str, str]) -> dict[str, Any]:
    normalized = {key: str(value) for key, value in overrides.items()}
    async with state_lock:
        for client in clients.values():
            client.setdefault("overrides", {}).update(normalized)
        updated = len(clients)

    await broadcast_full_state()
    return {"status": "ok", "updated": updated, "overrides": normalized}




@router.get("/api/inbox")
async def poll_inbox(request: Request, hostname: str) -> list[dict[str, Any]]:
    """Device polls for pending commands addressed to it. Marks them delivered."""
    if not hostname:
        raise HTTPException(status_code=422, detail="hostname is required")
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    # Proxmox agents send X-API-Key; simulation clients send X-Client-Key.
    api_key = request.headers.get("X-API-Key", "")
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    is_approved_proxmox = (
        approved_hostname is not None
        and api_key == approved_proxmox_agents[approved_hostname]
    )
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox")
    elif api_key != approved_proxmox_agents[approved_hostname]:
        raise HTTPException(status_code=401, detail="invalid key")
    return await _poll_agent_inbox(hostname, approved_hostname)




@router.post("/api/inbox/ack")
async def ack_command(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    """Device reports command result."""
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    api_key = request.headers.get("X-API-Key", "")
    ack_hostname = request.headers.get("X-Hostname", "") or body.get("hostname", "")
    is_approved_proxmox = any(
        api_key == v for v in approved_proxmox_agents.values()
    ) if api_key else False
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox/ack")
        ack_hostname = ack_hostname or "(sim-client)"
    result = await _ack_command_internal(body)
    _trace("inbox_ack_received", hostname=ack_hostname or "(unknown)",
           cmd_id=str(body.get("id", "")), status=body.get("status", ""),
           message=str(body.get("message", ""))[:200])
    return result




@router.post("/api/notifications/test")
async def api_notifications_test(body: dict[str, Any]) -> dict[str, Any]:
    """Send a test notification via email or Teams."""
    channel = body.get("channel", "")  # "email" | "teams"
    notif = dict(settings.get("notifications", {}))
    # Allow overriding with posted values (for unsaved fields)
    notif.update({k: v for k, v in body.items() if k != "channel"})

    test_transition = [{
        "check_type": "sim",
        "check_id": "test",
        "check_name": "Test Notification",
        "site": "test-site",
        "old": "ok",
        "new": "error",
        "ts": time.time(),
    }]

    try:
        if channel == "email":
            if not notif.get("smtp_host") or not notif.get("smtp_to"):
                raise HTTPException(status_code=422, detail="smtp_host and smtp_to are required")
            await asyncio.to_thread(_send_email_notifications, notif, test_transition)
        elif channel == "teams":
            url = notif.get("teams_webhook_url", "")
            if not url:
                raise HTTPException(status_code=422, detail="teams_webhook_url is required")
            await _send_teams_notifications(url, test_transition)
        else:
            raise HTTPException(status_code=422, detail="channel must be 'email' or 'teams'")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "ok", "channel": channel}
