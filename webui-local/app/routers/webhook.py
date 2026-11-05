"""Central webhook event receiver.

Aruba Central posts alert events to /api/{tenant_id}/webhook/central; each event becomes a
finding in the tenant's Central status cache and is broadcast live to browsers.
"""
# Allow ``X | None`` style hints on older Python versions.
from __future__ import annotations

# logging records received events; secrets.compare_digest does timing-safe key comparison.
import logging
import secrets
# Type hints for loosely structured JSON payloads.
from typing import Any, Optional

# FastAPI router, header extraction, HTTP errors and access to the raw request.
from fastapi import APIRouter, Header, HTTPException, Request

# store loads tenants; ArubaFinding models a check result; decrypt_dict reads Aruba settings.
from .. import store
from ..aruba import ArubaFinding
from ..crypto import decrypt_dict
# Shared in-memory Central status per tenant (and its setter) maintained by the poller.
from ..tasks import _hub_central_status, _set_hub_central_status
# ws_broadcast pushes updates to connected browsers.
from ..ws import ws_broadcast

# Router with its own /api prefix (app.main mounts it without one).
router = APIRouter(prefix="/api")
# Module logger.
logger = logging.getLogger(__name__)


def _webhook_status_from_payload(payload: dict[str, Any]) -> str:
    """Map a Central alert to a traffic-light status: cleared/unknown -> green,
    minor -> yellow, critical/major -> red.
    """
    # A cleared alert means the problem is resolved.
    if str(payload.get("state") or "").strip().lower() == "cleared":
        return "green"
    # Otherwise colour by severity.
    severity = str(payload.get("severity") or "").strip().lower()
    if severity in {"critical", "major"}:
        return "red"
    if severity == "minor":
        return "yellow"
    return "green"


def _merge_findings(current: list[dict[str, Any]] | None, webhook_findings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the existing findings plus any webhook findings not already present.

    Findings are identified by (site, check, source); existing entries win over webhook ones.
    """
    # Copy the current findings (ignoring malformed entries) so the original list is not changed.
    merged = [dict(item) for item in (current or []) if isinstance(item, dict)]
    # Keys of findings already present, used to avoid duplicates.
    seen = {
        (
            str(item.get("site") or "").strip(),
            str(item.get("check") or "").strip(),
            str(item.get("source") or "").strip(),
        )
        for item in merged
    }
    # Add each webhook finding whose key has not been seen yet.
    for item in webhook_findings.values():
        key = (
            str(item.get("site") or "").strip(),
            str(item.get("check") or "").strip(),
            str(item.get("source") or "").strip(),
        )
        if key in seen:
            continue
        merged.append(dict(item))
        seen.add(key)
    return merged


@router.post("/{tenant_id}/webhook/central")
async def receive_central_webhook(
    tenant_id: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """Receive real-time alert events from HPE Aruba Central."""
    # Unknown tenant: 404.
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    # The tenant must have Aruba settings saved for the webhook to be enabled.
    if not tenant.aruba_config_enc:
        raise HTTPException(status_code=404, detail="Central webhook is not configured")
    # Decrypt those settings; a failure here is a server-side problem (500).
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read Aruba webhook config: {exc}") from exc

    # A webhook API key must be configured; it is what Central sends back for authentication.
    webhook_api_key = str(cfg.get("webhook_api_key") or "").strip()
    if not webhook_api_key:
        raise HTTPException(status_code=404, detail="Central webhook is not configured")
    # Expected Authorization header value built from that key.
    expected_auth = f"Bearer {webhook_api_key}"
    # Constant-time comparison stops attackers guessing the key byte by byte via timing.
    if not authorization or not secrets.compare_digest(authorization, expected_auth):
        raise HTTPException(status_code=401, detail="Invalid webhook authorization")

    # Parse the body as JSON; malformed bodies get 400.
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc
    # Only JSON objects are valid alert events.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")

    # Copy this tenant's cached Central state so we can update it.
    tenant_state = dict(_hub_central_status.get(tenant_id) or {})
    # Map Central site ids to site names (from the poller) so findings show friendly names.
    site_id_map = tenant_state.get("site_id_map") if isinstance(tenant_state.get("site_id_map"), dict) else {}
    site_id = str(payload.get("siteId") or "").strip()
    site_name = str(site_id_map.get(site_id) or site_id or "unknown").strip() or "unknown"
    # Turn the alert into a finding, naming it after the alert name or id.
    finding = ArubaFinding(
        site_name=site_name,
        check_name=str(payload.get("name") or payload.get("alertId") or payload.get("id") or "alert").strip() or "alert",
        status=_webhook_status_from_payload(payload),
        source="alert",
        raw=payload,
    )
    # Dict form of the finding stored in the cache and sent to the UI.
    finding_payload = {
        "site": finding.site_name,
        "check": finding.check_name,
        "status": finding.status,
        "source": finding.source,
        "alert_id": str(payload.get("alertId") or "").strip(),
        "state": str(payload.get("state") or "").strip(),
        "time": payload.get("time"),
        "raw": payload,
    }

    # Alerts are cached by their id so repeated events update rather than duplicate.
    cache_key = str(payload.get("alertId") or payload.get("id") or "").strip()
    if not cache_key:
        raise HTTPException(status_code=400, detail="Webhook payload is missing alertId")
    # Store/replace this alert in the webhook cache, then rebuild the merged findings list.
    webhook_findings = dict(tenant_state.get("webhook_findings") or {})
    webhook_findings[cache_key] = finding_payload
    tenant_state["webhook_findings"] = webhook_findings
    tenant_state["findings"] = _merge_findings(tenant_state.get("findings"), webhook_findings)
    # Save the updated tenant state back into the shared cache.
    _set_hub_central_status(tenant_id, tenant_state)

    # Push the new state to all connected browsers so dashboards update immediately.
    await ws_broadcast(
        {
            "type": "aruba_update",
            "tenant_id": tenant_id,
            "findings": tenant_state.get("findings", []),
            "status": tenant_state.get("status", {}),
            "wireless_clients": tenant_state.get("wireless_clients", {}),
            "hardware_alerts": tenant_state.get("hardware_alerts", []),
            "client_count_status": tenant_state.get("client_count_status", {}),
            "central_sites_config": tenant_state.get("central_sites_config", {}),
            "token_state": tenant_state.get("token_state", {"state": "connected", "detail": ""}),
            "webhook": True,
        }
    )
    # Log receipt and tell Central the event was accepted.
    logger.info("Received Central webhook for tenant %s alert %s", tenant_id, cache_key)
    return {"received": True}
