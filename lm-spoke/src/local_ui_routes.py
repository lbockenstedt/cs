"""Local UI backend routes — answers the /sim/api/* contract for this spoke's
own standalone dashboard, sourced from local state instead of the LM hub's
cross-spoke aggregation cache.

``lm/WebUI/sim-views.js`` (the LM hub's per-spoke Simulations/Clients renderer)
talks to the hub's ``/sim/api/*`` routes, which aggregate cached telemetry
across every spoke in a tenant. This module answers the SAME route shapes
directly from THIS spoke's own ``CSSpoke`` instance, so sim-views.js's
renderers can be reused verbatim for a single spoke's local dashboard —
available whether the spoke is hub-connected or run with ``--standalone``
(mirrors ``CSControlPlane.run_standalone_mode``'s "same surface either way"
design). ``handle_command`` is already documented as drivable identically
from an LM hub command or an HTTP client (see cs_spoke.py's module
docstring), so every handler here just calls straight into it — no logic is
duplicated.

``tenant_id`` / ``{tenant}`` path segments are accepted (sim-views.js always
sends them) but ignored: a single spoke has no tenant concept of its own, so
every response always describes just this one spoke.

Known gap: Aruba Central integration (the source of the original webui-spoke's
Simulations "Checks"/"Hardware"/"Client Count" data) is not yet implemented in
lm-spoke at all — /aggregate/central* honestly returns an empty spoke list
rather than fabricating data, which renders sim-views.js's existing
"No spokes reporting simulation data yet" empty state.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request


def build_local_ui_router(spoke) -> APIRouter:
    """``spoke`` is the CSSpoke instance driving this process (hub-connected
    or standalone) — the same object registered as the hub's "cs" module."""
    router = APIRouter(prefix="/sim/api")

    async def _cmd(cmd: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await spoke.handle_command(cmd, data or {})

    # ── Clients tab ──────────────────────────────────────────────────────────

    @router.get("/aggregate/clients")
    async def aggregate_clients():
        rows = []
        now = time.time()
        for hostname, c in spoke.registry.get_all().items():
            last_seen = c.get("last_seen")
            online = bool(last_seen and (now - last_seen) < 300)
            rows.append({
                "spoke_id": spoke.spoke_id, "spoke_name": spoke.spoke_id,
                "spoke_online": True,
                "hostname": hostname, "id": hostname,
                "platform": c.get("platform") or "—",
                "hw_type": c.get("platform") or "",
                "online": online,
                "connected_ssid": c.get("connected_ssid") or "—",
                "simulation_id": c.get("simulation_id") or "",
                "active_simulations": c.get("active_simulations") or [],
                "last_seen": last_seen if last_seen is not None else "—",
                "error_count": len(c.get("recent_errors") or []),
                "recent_errors": c.get("recent_errors") or [],
            })
        return {"tenant_id": "default", "clients": rows}

    # ── Simulations tab (Checks/Hardware/Client Count sub-tabs) ─────────────
    # Central integration doesn't exist in lm-spoke yet — see module docstring.

    @router.get("/aggregate/central")
    async def aggregate_central():
        return {"spokes": [], "mode": "standalone"}

    @router.get("/aggregate/central-status")
    async def aggregate_central_status():
        return {"spokes": [], "mode": "standalone"}

    # ── Kill switch ──────────────────────────────────────────────────────────

    @router.get("/{tenant}/kill-switch")
    async def get_kill_switch(tenant: str):
        res = await _cmd("CS_GET_KILL_SWITCH")
        return {**res, "spoke_connected": True}

    @router.post("/{tenant}/kill-switch")
    async def set_kill_switch(tenant: str, request: Request):
        body = await request.json()
        return await _cmd("CS_KILL_SWITCH", {"on": bool(body.get("on"))})

    # ── Demo scenarios ───────────────────────────────────────────────────────

    @router.get("/{tenant}/demo/active")
    async def demo_active(tenant: str):
        return await _cmd("CS_GET_DEMO_ACTIVE")

    @router.get("/{tenant}/demo/scenarios")
    async def demo_scenarios(tenant: str):
        return await _cmd("CS_GET_DEMO_SCENARIOS")

    @router.post("/{tenant}/demo/client/{hostname}/scenario")
    async def demo_set(tenant: str, hostname: str, request: Request):
        body = await request.json()
        return await _cmd("CS_DEMO_SCENARIO", {
            "hostname": hostname,
            "scenario": body.get("scenario"),
            "triggered_by": "standalone-ui",
        })

    @router.delete("/{tenant}/demo/client/{hostname}/scenario")
    async def demo_clear(tenant: str, hostname: str):
        return await _cmd("CS_DEMO_CLEAR", {"hostname": hostname})

    # ── Per-client override control panel ───────────────────────────────────

    @router.get("/{tenant}/clients/{hostname}/control")
    async def get_control(tenant: str, hostname: str):
        return await _cmd("CS_GET_CLIENT_OVERRIDES", {"hostname": hostname})

    @router.post("/{tenant}/clients/{hostname}/control")
    async def set_control(tenant: str, hostname: str, request: Request):
        body = await request.json()
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else body
        return await _cmd("CS_SET_CLIENT_OVERRIDES", {"hostname": hostname, "overrides": overrides})

    @router.delete("/{tenant}/clients/{hostname}/control")
    async def clear_control(tenant: str, hostname: str):
        return await _cmd("CS_CLEAR_CLIENT_OVERRIDES", {"hostname": hostname})

    @router.post("/{tenant}/clients/control-all")
    async def control_all(tenant: str, request: Request):
        body = await request.json()
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else body
        return await _cmd("CS_SET_ALL_CLIENT_OVERRIDES", {"overrides": overrides})

    return router
