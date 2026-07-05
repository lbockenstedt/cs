"""Demo scenario API routes (moved verbatim from server.py); logic stays in server."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import (
    DEMO_SCENARIOS,
    SpokeUser,
    require_auth,
    _apply_demo_scenario,
    _clear_demo_scenario,
    _demo_active,
    _demo_active_summary,
)

router = APIRouter()


class DemoScenarioRequest(BaseModel):
    scenario: str  # e.g. "dns_fail", "dhcp_fail", "normal"


@router.post("/api/demo/client/{hostname}/scenario")
async def api_demo_set_scenario(
    hostname: str,
    body: DemoScenarioRequest,
    user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Trigger a named demo scenario on a client.

    Called via hub WebSocket relay or directly by an admin.
    The override is in-memory and expires after 120 minutes or on reboot.
    """
    payload = await _apply_demo_scenario(hostname, body.scenario, triggered_by=user.username)
    entry = _demo_active.get(hostname)
    return {
        "ok": True,
        "hostname": hostname,
        "scenario": body.scenario,
        "minutes_remaining": round(entry["minutes_remaining"] if entry and "minutes_remaining" in entry else 0),
        "client": payload,
    }


@router.delete("/api/demo/client/{hostname}/scenario")
async def api_demo_clear_scenario(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Clear the demo scenario override for a specific client."""
    payload = await _clear_demo_scenario(hostname)
    return {"ok": True, "hostname": hostname, "cleared": True, "client": payload}


@router.get("/api/demo/active")
async def api_demo_active(_user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Return all currently active demo scenario overrides."""
    return {"active": _demo_active_summary()}


@router.get("/api/demo/scenarios")
async def api_demo_scenarios(_user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Return the available scenario names and their flag definitions."""
    return {"scenarios": DEMO_SCENARIOS}
