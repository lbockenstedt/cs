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

Central API data comes from CentralPoller (central_poller.py), driven by this
spoke's own local_store.py config (no LM hub tenant store needed) — see that
module's docstring. Auto-provisioning config (hub-config) is likewise stored
locally and applied via the SAME _apply_hub_config path CS_CONFIG_UPDATE uses.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

import sim_config

logger = logging.getLogger("CSLocalUI")

# Matches client_api.py's CONFIGS_DIR / cs_spoke.py's inline equivalent — kept
# as a separate constant (not imported from client_api.py) to avoid a circular
# import, since client_api.py imports build_local_ui_router from this module.
_CONFIGS_DIR = Path(__file__).resolve().parent.parent.parent / "configs"


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
        # Tier join (client → VM → USB dongle): a client whose Proxmox VM has a
        # dongle assigned — or that reported its own USB WiFi adapter — is T2.
        # has_usb is what sim-views.js's csClassifyClient reads; without it every
        # row falls through to the T1 default (the "everything shows T1" bug).
        # Mirrors control_plane.py's hub-telemetry path so the local dashboard
        # and hub Clients views agree.
        deploy = getattr(spoke, "deploy", None)
        if deploy is not None:
            usb_vmids, name_to_vmid = deploy.usb_vmid_index()
            tier_index = deploy.vm_tier_index()
        else:
            usb_vmids, name_to_vmid, tier_index = set(), {}, {}
        # Load sim configs once (mtime-cached) so each client's authoritative
        # Site/PHY/Sim-ID resolves from its hostname — mirrors control_plane's
        # hub-telemetry path so the local and hub Clients views agree.
        try:
            _sim_conf, _user_conf = sim_config.load_configs(_CONFIGS_DIR)
        except Exception as _e:  # noqa: BLE001 — degrade to reported values
            _sim_conf = _user_conf = None
            logger.debug("aggregate_clients: config load failed: %s", _e)
        tier_updates: Dict[str, Dict[str, Any]] = {}
        for hostname, c in spoke.registry.get_all().items():
            last_seen = c.get("last_seen")
            eff_sim_id, eff_cfg = sim_config.effective_client_fields(
                hostname, _sim_conf, _user_conf,
                c.get("simulation_id") or "", c.get("config"))
            online = bool(last_seen and (now - last_seen) < 300)
            if deploy is not None:
                vmid, has_usb = deploy.client_has_usb(
                    hostname, c, usb_vmids, name_to_vmid)
            else:
                vmid, has_usb = None, False
            tier = tier_index.get(str(vmid)) if vmid else None
            if vmid and tier:
                tier_updates[hostname] = {"tier": tier, "has_usb": has_usb}
            if vmid is None:
                # Host/agent offline or VM aged out: fall back to the last-known
                # authoritative tier/has_usb persisted while it WAS reporting, so
                # the row keeps T2 (etc.) instead of dropping to T1. Mirrors
                # control_plane.py's hub-telemetry path.
                tier = tier or c.get("tier")
                has_usb = has_usb or bool(c.get("last_known_has_usb"))
            rows.append({
                "spoke_id": spoke.spoke_id, "spoke_name": spoke.spoke_id,
                "spoke_online": True,
                "hostname": hostname, "id": hostname,
                "platform": c.get("platform") or "—",
                "hw_type": c.get("platform") or "",
                "online": online,
                "connected_ssid": c.get("connected_ssid") or "—",
                "simulation_id": eff_sim_id,
                "active_simulations": c.get("active_simulations") or [],
                "last_seen": last_seen if last_seen is not None else "—",
                "error_count": len(c.get("recent_errors") or []),
                "recent_errors": c.get("recent_errors") or [],
                "vmid": vmid,
                "has_usb": has_usb,
                "tier": tier,
                # Carry the persisted per-client sim overrides + config so the
                # local dashboard's per-sim override buttons reflect what's SET
                # (not just what's running) and STAY across refreshes — mirrors
                # the hub-telemetry path (control_plane.py) so the local Clients
                # view and the hub's agree.
                "config": eff_cfg,
                "overrides": c.get("overrides") or {},
            })
        if tier_updates:
            try:
                await spoke.registry.record_tiers_batch(tier_updates)
            except Exception as e:  # noqa: BLE001
                logger.debug("record_tiers_batch failed: %s", e)
        return {"tenant_id": "default", "clients": rows}

    # ── Simulations tab (Checks/Hardware/Client Count sub-tabs) ─────────────
    # Real data from CentralPoller now (see module docstring) — empty
    # central_status (no Central configured yet) still renders sim-views.js's
    # own "No spokes reporting simulation data yet" empty state gracefully.

    def _central_spoke_entry() -> Dict[str, Any]:
        return {
            "spoke_id": spoke.spoke_id, "spoke_name": spoke.spoke_id,
            "spoke_online": True,
            "central_status": spoke.central_status or {},
        }

    @router.get("/aggregate/central")
    async def aggregate_central():
        if not spoke.central_status:
            return {"spokes": [], "mode": "standalone"}
        return {"spokes": [_central_spoke_entry()], "mode": "standalone"}

    @router.get("/aggregate/central-status")
    async def aggregate_central_status():
        cc = spoke.local_store.get_central_config()
        data: Dict[str, Any] = (
            {"spokes": [_central_spoke_entry()], "mode": "standalone"}
            if spoke.central_status else {"spokes": [], "mode": "standalone"}
        )
        # Merge hub-owned central config (mode + cluster creds) so the Setup →
        # Central API tab's form populates (mirrors the hub's get_central_status).
        data["hub_central_config"] = {k: v for k, v in cc.items() if k != "mode"}
        if cc.get("mode"):
            data["mode"] = cc["mode"]
        return data

    @router.get("/aggregate/central-browse")
    async def aggregate_central_browse():
        """Full Central inventory (all sites/alerts/insights/clients) for the
        standalone spoke webui — parity with the hub's
        /sim/api/aggregate/central-browse. Calls the spoke's browse directly (this
        IS the spoke), independent of site_mappings."""
        return await spoke.central_poller.browse()

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

    # "Purge Clients" — registered BEFORE the {hostname}/control routes so the
    # {hostname} path param doesn't swallow a bare collection DELETE. Mirrors
    # the hub's DELETE /sim/api/{tenant}/clients → CS_PURGE_CLIENTS.
    @router.delete("/{tenant}/clients")
    async def purge_clients(tenant: str):
        return await _cmd("CS_PURGE_CLIENTS", {})

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

    # ── API Server tab ───────────────────────────────────────────────────────

    @router.get("/aggregate/api-server")
    async def aggregate_api_server():
        ks = await _cmd("CS_GET_KILL_SWITCH")
        return {"spokes": [{
            "spoke_id": spoke.spoke_id,
            "spoke_name": spoke.spoke_id,
            "spoke_hostname": spoke.spoke_id,
            "spoke_online": True,
            "api_server": {
                "health": {
                    "status": "ok",
                    "clients": spoke.registry.count() if spoke.registry is not None else 0,
                    "repo_synced": True,
                    "repo_error": None,
                    "version": spoke.get_version(),
                },
                "services": {
                    "simulation_engine": "killed" if ks.get("kill_switch") else "running",
                    "client_registry": "running",
                },
            },
        }]}

    # ── Config tab (simulation.conf + user-overrides.conf editors) ──────────
    # Maps directly onto cs_spoke.py's existing CS_GET_CONFIG/CS_UPDATE_CONFIG/
    # CS_UPDATE_USER_OVERRIDES commands — no new logic, just HTTP shape glue.
    # Skips the hub's config-push / per-tenant hub-config cards entirely (both
    # are multi-spoke/tenant hub-admin concepts that don't apply to a single
    # standalone spoke).

    @router.get("/{tenant}/config/simulation-conf-parsed")
    async def config_sim_conf_parsed(tenant: str):
        res = await _cmd("CS_GET_CONFIG")
        if res.get("status") != "SUCCESS":
            return res
        raw = res.get("simulation_conf") or ""
        # Re-parse the MERGED text CS_GET_CONFIG returned (base file + any
        # hub-applied override) so edits already saved show up as sections.
        # sim_config.sections_dict is the same helper client_api.py's
        # /api/config/parsed uses (that route reads the base file only —
        # this one needs the merged text CS_GET_CONFIG already computed).
        parser = sim_config._new_parser()
        parser.read_string(raw)
        return {"fetched_at": time.time(), "source": "spoke",
                "sections": sim_config.sections_dict(parser), "raw": raw}

    @router.put("/{tenant}/config/simulation-conf")
    async def config_sim_conf_put(tenant: str, request: Request):
        body = await request.json()
        res = await _cmd("CS_UPDATE_CONFIG", {"content": body.get("content") or ""})
        return {**res, "synced_spokes": 1 if res.get("status") == "SUCCESS" else 0}

    @router.get("/{tenant}/config/user-overrides-conf")
    async def config_user_overrides_get(tenant: str):
        # Reads the file directly — same approach as client_api.py's existing
        # /api/config/overrides — rather than round-tripping through
        # CS_GET_CONFIG's configparser serialize, which risks reformatting
        # comments/whitespace on a file this editor is about to show verbatim.
        path = _CONFIGS_DIR / "user-overrides.conf"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"content": content, "fetched_at": time.time(), "source": "spoke"}

    @router.put("/{tenant}/config/user-overrides-conf")
    async def config_user_overrides_put(tenant: str, request: Request):
        body = await request.json()
        res = await _cmd("CS_UPDATE_USER_OVERRIDES", {"content": body.get("content") or ""})
        return {**res, "synced_spokes": 1 if res.get("status") == "SUCCESS" else 0}

    # ── Config Source of Truth (Hub | GitHub) ───────────────────────────────
    # Mirrors the hub's /sim/api/{tenant}/config/source (routes.py). Single-tenant
    # here: {tenant} is kept for route-shape parity but ignored — the value lives
    # in the spoke's <configs>/hub-config-source flag file (written via the same
    # CS_CONFIG_UPDATE path the hub uses, read by sim_config.load_configs).
    @router.get("/{tenant}/config/source")
    async def get_config_source(tenant: str):
        try:
            src = (_CONFIGS_DIR / "hub-config-source").read_text(encoding="utf-8").strip().lower()
        except Exception:
            src = "github"
        source = "hub" if src == "hub" else "github"
        gh = getattr(spoke, "_github_config", None) or {}
        has_token = bool(str(gh.get("github_token") or "").strip())
        return {"source": source, "has_token": has_token,
                "writable": (source == "hub") or has_token,
                "repo_url": gh.get("repo_url", ""), "repo_branch": gh.get("repo_branch", "")}

    @router.post("/{tenant}/config/source")
    async def set_config_source(tenant: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        source = "hub" if str((body or {}).get("source")) == "hub" else "github"
        res = await _cmd("CS_CONFIG_UPDATE", {"config_source": source})
        ok = isinstance(res, dict) and res.get("status") == "SUCCESS"
        return {"saved": bool(ok), "source": source, "pushed_to_spokes": 1 if ok else 0}

    # ── Setup tab: hub-config (auto-provisioning knobs) ──────────────────────
    # csHubConfigCard/csSaveHubConfig/csResetHubConfig (sim-views.js) reused
    # as-is — same route shapes as the hub's /tenant/{tenant}/hub-config.

    @router.get("/tenant/{tenant}/hub-config")
    async def get_hub_config(tenant: str):
        return await _cmd("CS_GET_HUB_CONFIG")

    @router.put("/tenant/{tenant}/hub-config")
    async def set_hub_config(tenant: str, request: Request):
        body = await request.json()
        res = await _cmd("CS_SET_HUB_CONFIG", body)
        return {"saved": res.get("status") == "SUCCESS", "pushed_to_spokes": 1}

    @router.post("/tenant/{tenant}/hub-config/reset")
    async def reset_hub_config(tenant: str):
        return await _cmd("CS_RESET_HUB_CONFIG")

    # ── Setup → Central API tab ──────────────────────────────────────────────
    # csRenderSetupCentralApi (sim-views.js) reused as-is.

    @router.post("/aggregate/central")
    async def save_central(request: Request):
        body = await request.json()
        mode = body.get("mode")
        hub_cc = body.get("hub_central_config") or {}
        cfg = dict(hub_cc)
        if mode:
            cfg["mode"] = mode
        res = await _cmd("CS_SET_CENTRAL_CONFIG", {"central_config": cfg})
        return {"saved": res.get("status") == "SUCCESS", "pushed_to_spokes": 1}

    @router.get("/{tenant}/central-sites-config")
    async def get_central_sites(tenant: str):
        return await _cmd("CS_GET_CENTRAL_SITES_CONFIG")

    @router.post("/{tenant}/central-sites-config")
    async def set_central_sites(tenant: str, request: Request):
        body = await request.json()
        res = await _cmd("CS_SET_CENTRAL_SITES_CONFIG", body if isinstance(body, dict) else {})
        return {"saved": res.get("status") == "SUCCESS", "pushed_to_spokes": 1}

    @router.get("/{tenant}/central/available")
    async def get_central_available(tenant: str):
        return await _cmd("CS_GET_CENTRAL_AVAILABLE")

    @router.get("/{tenant}/sim-quota-catalog")
    async def get_sim_quota_catalog(tenant: str):
        return await _cmd("CS_GET_SIM_QUOTA_CATALOG")

    @router.get("/{tenant}/sim-quota-state")
    async def get_sim_quota_state(tenant: str):
        # Live engine ledger for the Config → Quota State view (which clients
        # are currently assigned to each effective quota + target vs. assigned).
        return await _cmd("CS_GET_SIM_QUOTA_STATE")

    @router.get("/{tenant}/pxmx-site-map")
    async def get_pxmx_site_map(tenant: str):
        return await _cmd("CS_GET_PXMX_SITE_MAP")

    @router.post("/{tenant}/pxmx-site-map")
    async def set_pxmx_site_map(tenant: str, request: Request):
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        if "pxmx_site_map" not in payload and isinstance(body, dict):
            # Allow the UI to POST just the mapping object.
            payload = {"pxmx_site_map": body}
        res = await _cmd("CS_SET_PXMX_SITE_MAP", payload)
        return {"saved": res.get("status") == "SUCCESS",
                "pxmx_site_map": res.get("pxmx_site_map", {}),
                "errors": res.get("errors", [])}

    @router.get("/{tenant}/agents")
    async def get_agents(tenant: str):
        return await _cmd("GET_AGENTS", {})

    @router.post("/{tenant}/test-central")
    async def test_central(tenant: str):
        return await _cmd("CS_TEST_CENTRAL")

    return router
