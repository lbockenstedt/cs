"""Demo scenario API routes (moved verbatim from server.py); logic stays in server."""
# Postpone annotation evaluation so type hints are never evaluated at runtime.
from __future__ import annotations

# Any is used for loosely-typed JSON response dicts.
from typing import Any

# FastAPI routing and dependency injection (used for the auth check).
from fastapi import APIRouter, Depends
# pydantic validates the JSON request body.
from pydantic import BaseModel

# Demo-scenario logic and auth helpers live in server.py; this file only defines the routes.
from server import (
    DEMO_SCENARIOS,
    SpokeUser,
    require_auth,
    _apply_demo_scenario,
    _clear_demo_scenario,
    _demo_active,
    _demo_active_summary,
)

# The router object every endpoint below is registered on.
router = APIRouter()


class DemoScenarioRequest(BaseModel):
    """Request body for starting a demo scenario."""
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
    # Apply the scenario and record who triggered it.
    payload = await _apply_demo_scenario(hostname, body.scenario, triggered_by=user.username)
    # Look up the active entry so the reply can say how long the override lasts.
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
    # Remove the override and return the client's refreshed state.
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
