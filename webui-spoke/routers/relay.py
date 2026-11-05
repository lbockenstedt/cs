"""Relay API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Relay helpers and shared state live in server.py; this file only defines routes.
from server import (
    Any,
    Body,
    HTTPException,
    Query,
    _broadcast_relay_state,
    _hub_tls_verify,
    _public_settings,
    _relay_diag_append,
    _relay_status_payload,
    _save_settings,
    asyncio,
    broadcast,
    httpx,
    logger,
    relay_diag_log,
    relay_sites,
    relay_state,
    relay_sync_once,
    settings,
    socket,
    state,
    state_lock,
    time,
)

# The router object every endpoint below is registered on.
router = APIRouter()




@router.post("/api/relay/trigger")
async def api_relay_trigger() -> dict[str, Any]:
    """Manually trigger an immediate relay sync."""
    # A sync only makes sense when relay is on and a server URL is set.
    if settings.get("relay_enabled") != "on":
        raise HTTPException(status_code=400, detail="Relay is not enabled")
    if not settings.get("relay_server_url"):
        raise HTTPException(status_code=400, detail="Relay server URL not configured")
    # Run the sync in the background so the request returns at once.
    asyncio.create_task(relay_sync_once())
    return {"status": "ok", "message": "Relay sync triggered"}




@router.post("/api/relay/ingest")
async def api_relay_ingest(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Accept a site snapshot from a remote WebUI acting as a relay agent."""
    # site_id is required; snapshots without a tenant go in a shared bucket.
    site_id = payload.get("site_id")
    if not site_id:
        raise HTTPException(status_code=422, detail="site_id is required")
    tenant_id = payload.get("tenant_id") or "__untenanted__"
    # Store the snapshot under the lock, stamped with the time it arrived.
    async with state_lock:
        relay_sites.setdefault(tenant_id, {})[site_id] = {**payload, "ingested_at": time.time()}
    # Tell connected UIs a new snapshot arrived.
    await broadcast({
        "type": "relay_ingest",
        "tenant_id": tenant_id,
        "site_id": site_id,
        "client_count": payload.get("client_count", 0),
        "timestamp": payload.get("timestamp"),
    })
    # Log the ingest and confirm which tenant/site was stored.
    logger.info("Ingested relay snapshot from tenant=%s site=%s (%d clients)", tenant_id, site_id, payload.get("client_count", 0))
    return {"status": "ok", "tenant_id": tenant_id, "site_id": site_id}




@router.get("/api/relay/sites")
async def api_relay_sites(tenant_id: str | None = Query(None)) -> dict[str, Any]:
    """Return ingested site snapshots. Optionally filter by tenant_id."""
    # Read under the lock so we never see a half-updated table.
    async with state_lock:
        if tenant_id:
            sites = list(relay_sites.get(tenant_id, {}).values())
        else:
            # Return all tenants flattened
            sites = [site for tenant in relay_sites.values() for site in tenant.values()]
    return {"sites": sites, "tenant_id": tenant_id}




@router.get("/api/relay/status")
async def api_relay_status_endpoint() -> dict[str, Any]:  # Serve the enriched relay payload so on-demand status checks match websocket broadcasts exactly.
    """Return the current relay status (same payload as the websocket broadcasts)."""
    return _relay_status_payload()  # Reuse the shared relay payload so the REST endpoint matches broadcasted isolation, spoke, and check-in fields exactly.




@router.get("/api/relay/monitored-items")
async def api_relay_monitored_items() -> dict[str, Any]:
    """Return hub-synced monitored items for this spoke (fetched each relay cycle)."""
    return state._hub_monitored_items




@router.post("/api/relay/revert-local")
async def api_relay_revert_local() -> dict[str, Any]:
    """Immediately revert hub_managed to False, restoring local control.

    Use when the hub tenant has been deleted or the hub is permanently unreachable.
    The spoke will stop accepting hub config pushes and allow local settings changes.
    """
    # Remember whether we were hub-managed, then clear hub control and its credentials.
    was_managed = bool(settings.get("hub_managed"))
    settings["hub_managed"] = False
    settings["relay_api_key"] = ""
    settings["relay_tenant_id"] = ""
    # Persist the change, then tell UIs about the new relay state and settings.
    _save_settings()
    await _broadcast_relay_state()
    await broadcast({"type": "settings_update", "settings": _public_settings()})
    # Log and add a diagnostics entry so the revert is traceable.
    logger.info("hub_managed manually reverted to local control by operator")
    _relay_diag_append("hub_managed_reverted", status_code=None, reason="manual operator revert via /api/relay/revert-local")
    return {"status": "ok", "was_managed": was_managed, "message": "Reverted to local control — hub_managed cleared"}




@router.get("/api/relay/diag")
async def api_relay_diag() -> dict[str, Any]:
    """Return registration diagnostics: config summary, live hub reachability, and registration log."""
    # Read the configured hub URL and this machine's hostname.
    server_url = settings.get("relay_server_url", "").rstrip("/")
    hostname = socket.gethostname()

    # Live reachability check — use just the base URL (scheme+host+port), not the tenant path
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(server_url) if server_url else None
    hub_base_url = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed and _parsed.netloc else server_url
    reachability: dict[str, Any] = {"tested_url": server_url or "(not set)", "ok": False, "detail": ""}
    if server_url:
        # Ask the hub's health endpoint (8 s limit) and record the result.
        try:
            async with httpx.AsyncClient(timeout=8, verify=_hub_tls_verify()) as hc:
                r = await hc.get(f"{hub_base_url}/api/health")
                reachability = {
                    "tested_url": f"{hub_base_url}/api/health",
                    "ok": r.status_code < 400,
                    "http_status": r.status_code,
                    "detail": r.text[:200],
                }
        except Exception as exc:
            # Any network error counts as unreachable; keep the error text.
            reachability = {
                "tested_url": f"{hub_base_url}/api/health",
                "ok": False,
                "detail": str(exc),
            }
    else:
        # No URL configured, so nothing to test.
        reachability["detail"] = "Server URL not configured"

    # Summary of the relay settings (the API key itself is never returned).
    config_check = {
        "relay_enabled": settings.get("relay_enabled", "off"),
        "hub_tls_verify": settings.get("hub_tls_verify", "off"),
        "server_url": server_url or "(not set)",
        "spoke_name": settings.get("relay_spoke_name", "") or "(not set — will use hostname)",
        "hostname": hostname,
        "spoke_id": settings.get("relay_spoke_id", "") or "(none)",
        "api_key_configured": bool(settings.get("relay_api_key")),
        "tenant_id": settings.get("relay_tenant_id", "") or "(none)",
    }

    # Return config, live state, reachability and the log (newest first).
    return {
        "config": config_check,
        "current_state": dict(relay_state),
        "reachability": reachability,
        "log": list(reversed(relay_diag_log)),
    }
