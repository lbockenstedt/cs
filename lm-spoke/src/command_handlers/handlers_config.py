"""Config command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_config`` looks the command up in
``_CONFIG_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own (``handle_command``
then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import hashlib
import json
import logging
import asyncio
import time
from pathlib import Path

import dhcp_health
import sim_config
import sim_tag_health
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


_CS_CONFIG_META_KEYS = ("config_hash", "full_config")

# Keys whose apply branch reads/rewrites state owned by another key, so when the
# owner is re-applied by a full-config diff the dependants must ride along even
# if unchanged: central_sites_config replaces the local csc that qt_exclude_sims
# / tier_priority are merged into, and the override-text write path depends on
# config_source + the github token.
_CS_CONFIG_COUPLED = {
    "central_sites_config": ("qt_exclude_sims", "tier_priority"),
    "config_source": ("sim_conf_override", "user_conf_override"),
    "github_config": ("sim_conf_override", "user_conf_override"),
}


def cs_value_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def cs_config_diff(patch: Dict[str, Any], applied_hashes: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Subset of ``patch`` whose value differs from the last-applied hash (an
    unseen key counts as changed), plus the coupled dependants of any kept key."""
    if not isinstance(patch, dict):
        return {}
    applied_hashes = applied_hashes or {}
    result = {k: v for k, v in patch.items()
              if cs_value_hash(v) != applied_hashes.get(k)}
    for key in list(result):
        for dep in _CS_CONFIG_COUPLED.get(key, ()):
            if dep in patch and dep not in result:
                result[dep] = patch[dep]
    return result


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class ConfigCommandsMixin:
    """Mixin handling cloud poller settings, hub configuration, and quota controls."""

    _CONFIG_COMMANDS = {
        "CS_GET_REPO_STATUS": "_cmd_get_repo_status",
        "CS_GET_CONFIG": "_cmd_get_config",
        "CS_UPDATE_CONFIG": "_cmd_update_config",
        "UPDATE_CONFIG": "_cmd_update_config",
        "CS_UPDATE_USER_OVERRIDES": "_cmd_update_user_overrides",
        "CS_CONFIG_UPDATE": "_cmd_config_update",
        "CS_GET_HUB_CONFIG": "_cmd_get_hub_config",
        "CS_SET_HUB_CONFIG": "_cmd_set_hub_config",
        "CS_RESET_HUB_CONFIG": "_cmd_reset_hub_config",
        "CS_GET_CENTRAL_CONFIG": "_cmd_get_central_config",
        "CS_SET_CENTRAL_CONFIG": "_cmd_set_central_config",
        "CS_GET_CENTRAL_SITES_CONFIG": "_cmd_get_central_sites_config",
        "CS_SET_CENTRAL_SITES_CONFIG": "_cmd_set_central_sites_config",
        "CS_GET_CENTRAL_AVAILABLE": "_cmd_get_central_available",
        "CS_TEST_CENTRAL": "_cmd_test_central",
        "CS_CENTRAL_BROWSE": "_cmd_central_browse",
        "CS_GET_CENTRAL_ON_PREM_CONFIG": "_cmd_get_central_on_prem_config",
        "CS_SET_CENTRAL_ON_PREM_CONFIG": "_cmd_set_central_on_prem_config",
        "CS_GET_CENTRAL_ON_PREM_SITES_CONFIG": "_cmd_get_central_on_prem_sites_config",
        "CS_SET_CENTRAL_ON_PREM_SITES_CONFIG": "_cmd_set_central_on_prem_sites_config",
        "CS_GET_CENTRAL_ON_PREM_AVAILABLE": "_cmd_get_central_on_prem_available",
        "CS_TEST_CENTRAL_ON_PREM": "_cmd_test_central_on_prem",
        "CS_CENTRAL_ON_PREM_BROWSE": "_cmd_central_on_prem_browse",
        "CS_GET_CENTRAL_ON_PREM_HEALTH": "_cmd_get_central_on_prem_health",
        "CS_GET_MIST_CONFIG": "_cmd_get_mist_config",
        "CS_SET_MIST_CONFIG": "_cmd_set_mist_config",
        "CS_GET_MIST_SITES_CONFIG": "_cmd_get_mist_sites_config",
        "CS_SET_MIST_SITES_CONFIG": "_cmd_set_mist_sites_config",
        "CS_GET_MIST_AVAILABLE": "_cmd_get_mist_available",
        "CS_TEST_MIST": "_cmd_test_mist",
        "CS_MIST_BROWSE": "_cmd_mist_browse",
        "CS_GET_MIST_HEALTH": "_cmd_get_mist_health",
        "CS_GET_HEALTH": "_cmd_get_health",
        "CS_GET_SIM_QUOTA_CATALOG": "_cmd_get_sim_quota_catalog",
        "CS_GET_SIM_QUOTA_STATE": "_cmd_get_sim_quota_state",
        "CS_RESET_SIM_QUOTA": "_cmd_reset_sim_quota",
        "CS_GET_SIM_TAG_HEALTH": "_cmd_get_sim_tag_health",
        "CS_GET_DHCP_HEALTH": "_cmd_get_dhcp_health",
        "CS_GET_POOLS": "_cmd_get_pools",
        "CS_POOL_ADD_EXISTING": "_cmd_pool_add_existing",
        "CS_GET_PXMX_SITE_MAP": "_cmd_get_pxmx_site_map",
        "CS_SET_PXMX_SITE_MAP": "_cmd_set_pxmx_site_map",
    }

    async def _dispatch_config(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the handler for ``cmd`` from ``_CONFIG_COMMANDS``; None if it is not one of ours."""
        handler = self._CONFIG_COMMANDS.get(cmd)
        if handler is None:
            return None
        return await getattr(self, handler)(d)

    # ── repo / update status (Setup → Diagnostics → API Server) ──────────
    # Lets an operator confirm FROM THE UI which branch + build this spoke's
    # checkout is serving to clients — the answer to "did my push reach the
    # spoke?" without touching the CLI. Reads the served scripts dir
    # (clients/linux, the same dir /api/scripts serves) + the tenant's
    # github_config branch. Best-effort; never raises.
    async def _cmd_get_repo_status(self, d):
        import re as _re
        try:
            repo_root = self.settings.config_dir.parent
            linux = repo_root / "clients" / "linux"
            gc = getattr(self, "_github_config", None) or {}

            def _script_ver(fn: str):
                p = linux / fn
                if not p.is_file():
                    return None
                try:
                    for line in p.read_text(errors="replace").splitlines()[:5]:
                        m = _re.match(r"^version=(\S+)", line)
                        if m:
                            return m.group(1)
                except Exception:  # noqa: BLE001
                    return "?"
                return "?"

            served_version = None
            vf = linux / "VERSION"
            if vf.is_file():
                try:
                    served_version = vf.read_text(errors="replace").strip()
                except Exception:  # noqa: BLE001
                    served_version = None
            head = checked_out = ""
            try:
                head = (await self._git("rev-parse", "--short", "HEAD")).strip()
                checked_out = (await self._git("rev-parse", "--abbrev-ref", "HEAD")).strip()
            except Exception:  # noqa: BLE001 — git may be absent / non-repo dir
                pass
            return {"status": "SUCCESS", "repo": {
                "configured_branch": str(gc.get("repo_branch") or "main"),
                "checked_out_branch": checked_out,
                "head": head,
                "source_of_truth": str(gc.get("source_of_truth")
                                       or gc.get("config_source")
                                       or ("github" if gc.get("repo_url") else "hub")),
                "served_version": served_version,
                "scripts": {
                    "simulation.sh": _script_ver("simulation.sh"),
                    "dns_fail.sh": _script_ver("dns_fail.sh"),
                    "dns_latency.sh": _script_ver("dns_latency.sh"),
                },
                "dns_latency_present": (linux / "dns_latency.sh").is_file(),
            }}
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_GET_REPO_STATUS failed: %s", exc)
            return {"status": "SUCCESS", "repo": {"error": str(exc)}}

    # ── config ─────────────────────────────────────────────────────────
    async def _cmd_get_config(self, d):
        # Return the MERGED config (base repo file + hub-managed override
        # layered on top) so the hub's Sim Config editor reads back the
        # effective config, not the raw base. Without the merge, edits saved
        # via CS_CONFIG_UPDATE (which writes hub-*-overrides.conf) would be
        # invisible on Refresh. Mirrors legacy GET /api/config + /api/config/overrides.
        base = Path(__file__).resolve().parent.parent.parent / "configs"
        sim_conf, user_conf = sim_config.load_configs(base)
        return {"status": "SUCCESS", "mode": "local",
                "simulation_conf": sim_config.serialize_ini(sim_conf),
                "user_overrides": sim_config.serialize_ini(user_conf)}

    async def _cmd_update_config(self, d):
        content = d.get("content")
        if content is None:
            return {"status": "ERROR", "message": "missing 'content'"}
        try:
            sim_config.validate_ini_text(content)
        except ValueError as exc:
            return {"status": "ERROR", "message": str(exc)}
        base = Path(__file__).resolve().parent.parent.parent / "configs"
        (base / "simulation.conf").write_text(content, encoding="utf-8")
        self.engine.reload_config()
        # sim config changed → bucket-default wsite / sim flags changed, so a
        # quota's site pool + exclusivity eligibility may shift. Re-reconcile.
        self._trigger_sim_quota_reconcile()
        return {"status": "SUCCESS", "message": "simulation.conf updated"}

    async def _cmd_update_user_overrides(self, d):
        content = d.get("content")
        if content is None:
            return {"status": "ERROR", "message": "missing 'content'"}
        try:
            sim_config.validate_ini_text(content)
        except ValueError as exc:
            return {"status": "ERROR", "message": str(exc)}
        base = Path(__file__).resolve().parent.parent.parent / "configs"
        (base / "user-overrides.conf").write_text(content, encoding="utf-8")
        self.engine.reload_config()
        # user-overrides can re-resolve a client's wsite / sim flags → same
        # site-pool / exclusivity shift as a simulation.conf edit; reconcile.
        self._trigger_sim_quota_reconcile()
        return {"status": "SUCCESS", "message": "user-overrides.conf updated"}

    async def _cmd_config_update(self, d):
        # Hub pushes hub-owned provisioning config (usb_vidpids,
        # usb_ignored_vidpids, usb_auto_provision, template ids, VLAN
        # ranges, reclone concurrency, ... + optional sim/user-overrides
        # INI text). The legacy cs webui-spoke applied these via
        # _apply_hub_config; this spoke MUST do the same or certification
        # pushes are silently dropped: usb_vidpids stays "[]" in settings,
        # the cs_bridge pulls an empty ``vidpids`` via CS_GET_USB_CONFIG
        # every 60s, the agent's _dongle_vidpids returns 0, and
        # auto-provision never fires ("no dongle_vidpids configured").
        return self._handle_cs_config_update(d)

    # ── local Setup-tab config (hub_config / central) ───────────────────
    # This spoke owns these knobs itself now (local_store.py) instead of
    # relaying them from an LM hub tenant store — see that module's
    # docstring. _apply_hub_config below is the SAME logic CS_CONFIG_UPDATE
    # already uses, so a locally-saved hub_config flows to the settings
    # store (and any cs-dialed pxmx agent) exactly like a hub-pushed one.
    async def _cmd_get_hub_config(self, d):
        return {"status": "SUCCESS", **self.local_store.get_hub_config()}

    async def _cmd_set_hub_config(self, d):
        enabled = bool(d.get("hub_config_enabled", False))
        hc = d.get("hub_config") or {}
        self.local_store.set_hub_config(enabled, hc)
        applied = self._apply_hub_config(hc) if enabled else []
        return {"status": "SUCCESS", "applied": applied}

    async def _cmd_reset_hub_config(self, d):
        result = self.local_store.reset_hub_config()
        if result.get("hub_config_enabled"):
            self._apply_hub_config(result.get("hub_config") or {})
        return {"status": "SUCCESS", **result}

    async def _cmd_get_central_config(self, d):
        return {"status": "SUCCESS", "central_config": self.local_store.get_central_config()}

    async def _cmd_set_central_config(self, d):
        self._merge_central_config(d.get("central_config") or {})
        return {"status": "SUCCESS"}

    async def _cmd_get_central_sites_config(self, d):
        return {"status": "SUCCESS", **self.local_store.get_central_sites_config()}

    async def _cmd_set_central_sites_config(self, d):
        cfg = d if isinstance(d, dict) else {}
        # Validate sim_quotas against the sims this tenant's simulation.conf
        # actually offers; drop unknown/invalid entries and surface errors so
        # the UI can report them. The rest of central_sites_config
        # (monitored_checks/hardware_checks/site_mappings) passes through
        # unchanged — sim_quotas is an additive field.
        wipe_blocked = False
        try:
            import sim_quota
            sims = [s["sim_id"] for s in sim_quota.available_sims(self.settings.config_dir)]
            clean, errs = sim_quota.validate_sim_quotas(cfg.get("sim_quotas"), sims)
            if errs:
                logger.warning("CS_SET_CENTRAL_SITES_CONFIG: sim_quotas errors: %s", errs)
            # Anti-blast safeguard: never let a save take sim_quotas from N>0
            # to 0 without an explicit force_sim_quotas_clear. A stale
            # simulation.conf (sims no longer lists the quota sims) makes
            # validate_sim_quotas drop every row — wiping the whole table.
            existing_quotas = (self.local_store.get_central_sites_config() or {}).get("sim_quotas") or []
            clean, wipe_blocked = sim_quota.guard_sim_quota_wipe(existing_quotas, clean, d)
            if wipe_blocked:
                logger.error("CS_SET_CENTRAL_SITES_CONFIG: sim_quotas wipe BLOCKED — "
                             "save would drop %d quota(s) to 0 (stale sim_ids or empty send) "
                             "without force_sim_quotas_clear; existing quotas preserved",
                             len(existing_quotas))
            cfg = {**cfg, "sim_quotas": clean}
        except Exception as exc:  # noqa: BLE001 — never block the save
            logger.warning("sim_quotas validate failed: %s", exc)
        self.local_store.set_central_sites_config(cfg)
        self.central_poller.reload()
        return {"status": "SUCCESS", "sim_quotas_wipe_blocked": wipe_blocked}

    async def _cmd_get_central_available(self, d):
        return await self.central_poller.available_checks()

    async def _cmd_test_central(self, d):
        return await self.central_poller.test_connection()

    async def _cmd_central_browse(self, d):
        return await self.central_poller.browse()

    # ── Aruba Central On-Prem (twin of the Central handlers above — a SECOND
    # Aruba Central instance via the SAME ArubaClient/API, just a separate
    # config + sites-config + status slot). The on-prem poller
    # (self.central_on_prem_poller) reads/writes its own slots so cloud Central
    # and on-prem never step on each other.
    async def _cmd_get_central_on_prem_config(self, d):
        return {"status": "SUCCESS",
                "central_on_prem_config": self.local_store.get_central_on_prem_config()}

    async def _cmd_set_central_on_prem_config(self, d):
        self._merge_central_on_prem_config(d.get("central_on_prem_config") or {})
        return {"status": "SUCCESS"}

    async def _cmd_get_central_on_prem_sites_config(self, d):
        return {"status": "SUCCESS", **self.local_store.get_central_on_prem_sites_config()}

    async def _cmd_set_central_on_prem_sites_config(self, d):
        cfg = d if isinstance(d, dict) else {}
        # Validate sim_quotas against this tenant's simulation.conf sims
        # (same drop-unknown + surface-errors pass as the Central twin). The
        # rows here carry Central On-Prem:-prefixed alert_ids.
        wipe_blocked = False
        try:
            import sim_quota
            sims = [s["sim_id"] for s in sim_quota.available_sims(self.settings.config_dir)]
            clean, errs = sim_quota.validate_sim_quotas(cfg.get("sim_quotas"), sims)
            if errs:
                logger.warning("CS_SET_CENTRAL_ON_PREM_SITES_CONFIG: sim_quotas errors: %s", errs)
            existing_quotas = (self.local_store.get_central_on_prem_sites_config() or {}).get("sim_quotas") or []
            clean, wipe_blocked = sim_quota.guard_sim_quota_wipe(existing_quotas, clean, d)
            if wipe_blocked:
                logger.error("CS_SET_CENTRAL_ON_PREM_SITES_CONFIG: sim_quotas wipe BLOCKED — "
                             "save would drop %d quota(s) to 0 (stale sim_ids or empty send) "
                             "without force_sim_quotas_clear; existing quotas preserved",
                             len(existing_quotas))
            cfg = {**cfg, "sim_quotas": clean}
        except Exception as exc:  # noqa: BLE001 — never block the save
            logger.warning("sim_quotas validate failed (central_on_prem): %s", exc)
        self.local_store.set_central_on_prem_sites_config(cfg)
        self.central_on_prem_poller.reload()
        return {"status": "SUCCESS", "sim_quotas_wipe_blocked": wipe_blocked}

    async def _cmd_get_central_on_prem_available(self, d):
        return await self.central_on_prem_poller.available_checks()

    async def _cmd_test_central_on_prem(self, d):
        return await self.central_on_prem_poller.test_connection()

    async def _cmd_central_on_prem_browse(self, d):
        return await self.central_on_prem_poller.browse()

    async def _cmd_get_central_on_prem_health(self, d):
        # 30-day per-check health for a DISTRIBUTED on-prem Central tenant
        # (twin of CS_GET_HEALTH / CS_GET_MIST_HEALTH). Daily summaries ride
        # in central_on_prem_status already; this serves the on-hover HOURLY
        # breakdown for one check.
        h = getattr(self.central_on_prem_poller, "_health", None)
        if h is None:
            return {"hourly": []}
        site = d.get("site")
        check = d.get("check")
        from central_poller import _CC_SCOPE
        if site and check:
            return {"hourly": h.hourly(_CC_SCOPE, site, check)}
        return {"daily": h.summary(_CC_SCOPE)}

    # ── Juniper Mist API (twin of the Central handlers above) ──────────
    async def _cmd_get_mist_config(self, d):
        return {"status": "SUCCESS", "mist_config": self.local_store.get_mist_config()}

    async def _cmd_set_mist_config(self, d):
        self._merge_mist_config(d.get("mist_config") or {})
        return {"status": "SUCCESS"}

    async def _cmd_get_mist_sites_config(self, d):
        return {"status": "SUCCESS", **self.local_store.get_mist_sites_config()}

    async def _cmd_set_mist_sites_config(self, d):
        cfg = d if isinstance(d, dict) else {}
        # Validate sim_quotas against this tenant's simulation.conf sims
        # (same drop-unknown + surface-errors pass as the Central twin).
        wipe_blocked = False
        try:
            import sim_quota
            sims = [s["sim_id"] for s in sim_quota.available_sims(self.settings.config_dir)]
            clean, errs = sim_quota.validate_sim_quotas(cfg.get("sim_quotas"), sims)
            if errs:
                logger.warning("CS_SET_MIST_SITES_CONFIG: sim_quotas errors: %s", errs)
            existing_quotas = (self.local_store.get_mist_sites_config() or {}).get("sim_quotas") or []
            clean, wipe_blocked = sim_quota.guard_sim_quota_wipe(existing_quotas, clean, d)
            if wipe_blocked:
                logger.error("CS_SET_MIST_SITES_CONFIG: sim_quotas wipe BLOCKED — "
                             "save would drop %d quota(s) to 0 (stale sim_ids or empty send) "
                             "without force_sim_quotas_clear; existing quotas preserved",
                             len(existing_quotas))
            cfg = {**cfg, "sim_quotas": clean}
        except Exception as exc:  # noqa: BLE001 — never block the save
            logger.warning("sim_quotas validate failed (mist): %s", exc)
        self.local_store.set_mist_sites_config(cfg)
        self.mist_poller.reload()
        return {"status": "SUCCESS", "sim_quotas_wipe_blocked": wipe_blocked}

    async def _cmd_get_mist_available(self, d):
        return await self.mist_poller.available_checks()

    async def _cmd_test_mist(self, d):
        return await self.mist_poller.test_connection()

    async def _cmd_mist_browse(self, d):
        return await self.mist_poller.browse()

    async def _cmd_get_mist_health(self, d):
        # 30-day per-check health for a DISTRIBUTED Mist tenant (twin of
        # CS_GET_HEALTH). Daily summaries ride in mist_status already; this
        # serves the on-hover HOURLY breakdown for one check.
        h = getattr(self.mist_poller, "_health", None)
        if h is None:
            return {"hourly": []}
        site = d.get("site")
        check = d.get("check")
        from central_poller import _CC_SCOPE
        if site and check:
            return {"hourly": h.hourly(_CC_SCOPE, site, check)}
        return {"daily": h.summary(_CC_SCOPE)}

    async def _cmd_get_health(self, d):
        # 30-day per-check health for a DISTRIBUTED tenant. Daily summaries ride
        # in central_status already; this serves the on-hover HOURLY breakdown
        # for one check (site+check in the payload).
        h = getattr(self.central_poller, "_health", None)
        if h is None:
            return {"hourly": []}
        site = d.get("site")
        check = d.get("check")
        from central_poller import _CC_SCOPE
        if site and check:
            return {"hourly": h.hourly(_CC_SCOPE, site, check)}
        return {"daily": h.summary(_CC_SCOPE)}

    async def _cmd_get_sim_quota_catalog(self, d):
        # The Sim-Quota UI (Config → Sim Quotas) renders against this: the
        # sims/sites derived from this tenant's simulation.conf + the global
        # suggested alert→sim linkage. Sims come from simulation.conf, not
        # a hardcoded list, so a tenant that adds a flag to its buckets sees
        # it here automatically.
        try:
            import sim_quota
            csc = self.local_store.get_central_sites_config() or {}
            cat = sim_quota.sim_quota_catalog(
                self.settings.config_dir, csc.get("site_mappings"))
            return {"status": "SUCCESS", **cat}
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_GET_SIM_QUOTA_CATALOG failed: %s", exc)
            return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}",
                    "sims": [], "sites": [], "suggested": {}, "meta": {}}

    async def _cmd_get_sim_quota_state(self, d):
        # Engine ledger snapshot for the quota-state view (Chunk 4): which
        # clients are currently assigned to each effective quota. The
        # monitored_checks slice from central_sites_config is included so the
        # UI can join alert/insight IDs to their friendly names (a quota row
        # stores only the bare id).
        eng = getattr(self, "sim_quota_engine", None)
        snap = eng.snapshot() if eng is not None else {}
        placement_warnings = eng.placement_warnings() if eng is not None else []
        quota_diag = eng.quota_diagnostics() if eng is not None else []
        csc = self.local_store.get_central_sites_config()
        monitored = csc.get("monitored_checks") or []
        # Live per-check firing status from the Central poller's last cycle —
        # the same ``{site: {check_id: {status, message}}}`` the dashboard
        # Checks table renders via ``csStatusBadge``. Surfaced here so the
        # Engine State view can show whether each alert/insight is currently
        # firing WITHOUT a second API query: it reuses the spoke's in-memory
        # ``central_status`` (refreshed every poll loop). Empty when Central
        # isn't configured or no poll has run yet. INVERTED semantics live in
        # the poller: status "ok" == the expected error IS present == firing.
        cs = getattr(self, "central_status", None) or {}
        return {"status": "SUCCESS",
                "effective": self.local_store.get_effective_sim_quotas(),
                "ledger": snap,
                "monitored_checks": monitored,
                "placement_warnings": placement_warnings,
                "diagnostics": quota_diag,
                "pool": eng.pool_counts() if eng is not None else {},
                "check_status": cs.get("status") or {}}

    async def _cmd_reset_sim_quota(self, d):
        # Clear the engine ledger + engine-set overrides and reconcile fresh —
        # a clean re-shuffle (operator "Reset & Reshuffle" button).
        eng = getattr(self, "sim_quota_engine", None)
        if eng is None:
            return {"status": "ERROR", "message": "sim quota engine unavailable"}
        actions = await eng.reset()
        return {"status": "SUCCESS", "actions": actions}

    async def _cmd_get_sim_tag_health(self, d):
        # Why a Proxmox server shows no / stale sim- tags; see sim_tag_health.
        import socket
        from token_store import norm_hostname
        deploy = getattr(self, "deploy", None)
        states = (getattr(deploy, "proxmox_states", {}) or {}) if deploy else {}
        skips = dict(getattr(self, "_sim_tag_skips", {}) or {})
        cp = getattr(self, "control_plane", None)
        agents = (getattr(cp, "connected_agents", {}) or {}) if cp else {}
        connected = {str((a or {}).get("hostname") or "").strip() for a in agents.values()} - {""}
        connected_norm = {norm_hostname(h) for h in connected}
        reg = getattr(self, "registry", None)
        try:
            clients = sim_tag_health.client_index((reg.get_all() if reg else {}) or {})
        except Exception:  # noqa: BLE001
            clients = {}
        pointing = await self._agent_pointing(agents)
        hosts = [
            sim_tag_health.host_report(
                hn, st, clients,
                agent_connected=hn in connected or norm_hostname(hn) in connected_norm,
                skip_reason=skips.get(hn),
                points_at=pointing.get(norm_hostname(hn)))
            for hn, st in states.items()
        ]
        return {"status": "SUCCESS",
                "connected_agents": sorted(connected),
                "live_window_s": sim_tag_health.TAG_LIVE_WINDOW_S,
                "spoke_self": {
                    "spoke_id": getattr(self, "spoke_id", "") or "",
                    "hostname": socket.gethostname(),
                    "hub_url": str(getattr(getattr(self, "settings", None), "hub_url", "")
                                   or getattr(self, "hub_url", "") or ""),
                },
                "hosts": sorted(hosts, key=lambda h: h["hostname"])}

    async def _agent_pointing(self, agents: Dict[str, Any]) -> Dict[str, str]:
        """normalized hostname -> where that connected agent says it is pointed.

        Each pxmx host should dial its OWN cs spoke; when telemetry lands on the
        wrong spoke (or nowhere), the agent's pinned URL settles it. Only this
        spoke can ask its own agents, so the fleet picture comes from asking
        every spoke: a host that appears under none has an agent connected nowhere.
        """
        from token_store import norm_hostname
        pointing: Dict[str, str] = {}
        for agent_id, info in agents.items():
            hostname = str((info or {}).get("hostname") or "").strip()
            if not hostname:
                continue
            try:
                reply = await self.control_plane.send_to_agent(
                    "PXMX_GET_IDENTITY", {}, agent_id=agent_id, timeout=10.0)
                where = sim_tag_health.pointing_from_identity(reply)
                if where is not None:
                    pointing[norm_hostname(hostname)] = where
            except Exception as exc:  # noqa: BLE001 — diagnostics only
                pointing[norm_hostname(hostname)] = f"(unknown: {exc})"
        return pointing

    async def _cmd_get_dhcp_health(self, d):
        # Kea sim-DHCP diagnostics; see dhcp_health for what each signal means.
        return dhcp_health.collect()

    async def _cmd_get_pools(self, d):
        # Proxmox resource pools available across this spoke's pxmx agents,
        # for the sim-client Pool dropdown. Unioned across agents: pools are
        # cluster-wide, but a fleet of STANDALONE nodes each has its own set,
        # and an operator picking a pool that only exists on some hosts would
        # get clone failures on the rest — so the response also reports WHICH
        # hosts have each pool, and the UI can flag a partial one.
        by_pool: Dict[str, list] = {}
        hosts_ok, hosts_err = [], {}
        try:
            agents = self._get_agents().get("agents", [])
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "message": f"agent list failed: {exc}",
                    "pools": [], "by_pool": {}}
        for a in agents:
            aid = a.get("agent_id")
            hn = str(a.get("hostname") or aid or "?")
            if not aid:
                continue
            try:
                r = await self.control_plane.send_to_agent(
                    "PXMX_LIST_POOLS", {}, agent_id=aid, timeout=15.0)
            except Exception as exc:  # noqa: BLE001 — one agent must not blank the list
                hosts_err[hn] = str(exc)
                continue
            d = (r or {}).get("payload", {}).get("data", r) if isinstance(r, dict) else {}
            if not isinstance(d, dict) or d.get("status") == "ERROR":
                hosts_err[hn] = str((d or {}).get("message") or "error")
                continue
            hosts_ok.append(hn)
            # The agent's PXMX_LIST_POOLS returns [{poolid, comment}, ...]
            # (vm_inventory.list_pools, via the Proxmox API). Tolerate a bare
            # string too so an older agent build still populates the dropdown.
            for p in (d.get("pools") or []):
                pid = (str(p.get("poolid") or "").strip()
                       if isinstance(p, dict) else str(p or "").strip())
                if pid:
                    by_pool.setdefault(pid, []).append(hn)
        return {"status": "SUCCESS",
                "pools": sorted(by_pool),
                "by_pool": {k: sorted(v) for k, v in by_pool.items()},
                "hosts": sorted(hosts_ok), "errors": hosts_err,
                "selected": str(self.settings.get("sim_pool", "") or "").strip()}

    async def _cmd_pool_add_existing(self, d):
        # Retrofit: put already-provisioned sim VMs into the configured pool
        # (new clones get it via qm clone --pool). Fans out to every agent;
        # the agent guards to the sim VMID range so this can never sweep a
        # non-sim VM into the pool.
        pool = str((data or {}).get("pool")
                   or self.settings.get("sim_pool", "") or "").strip()
        if not pool:
            return {"status": "ERROR", "message": "no pool configured"}
        added, errors = 0, {}
        try:
            agents = self._get_agents().get("agents", [])
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "message": f"agent list failed: {exc}"}
        for a in agents:
            aid = a.get("agent_id")
            hn = str(a.get("hostname") or aid or "?")
            if not aid:
                continue
            try:
                r = await self.control_plane.send_to_agent(
                    "PXMX_POOL_ADD_VMS", {"pool": pool}, agent_id=aid, timeout=60.0)
            except Exception as exc:  # noqa: BLE001
                errors[hn] = str(exc)
                continue
            d = (r or {}).get("payload", {}).get("data", r) if isinstance(r, dict) else {}
            if isinstance(d, dict) and d.get("status") == "SUCCESS":
                added += int(d.get("added") or 0)
            else:
                errors[hn] = str((d or {}).get("message") or "error")
        return {"status": "SUCCESS" if not errors else "PARTIAL",
                "pool": pool, "added": added, "errors": errors}

    async def _cmd_get_pxmx_site_map(self, d):
        # Operator-assigned pxmx server → site map (Config → PXMX Sites). The
        # engine resolves a client's site via its hosting server's entry.
        # Also return the currently-connected pxmx agents so the UI can list
        # assignable servers + flag servers whose agent has dropped.
        agents = []
        try:
            agents = self._get_agents().get("agents", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_GET_PXMX_SITE_MAP: agents list failed: %s", exc)
        # _get_agents() lists only CONNECTED (+ pending) agents, so a server
        # whose agent is down and which has never been assigned had no row
        # in the editor at all — it could not be given a site until it came
        # back. Site assignment is operator intent about where a box
        # physically IS; it does not need the agent online, and the engine
        # reads the map (not the agent) when placing clients. So union in
        # every host the spoke still knows from telemetry: deploy
        # .proxmox_states retains stale hosts deliberately (see
        # proxmox_deploy.relay_payload) precisely so a briefly-offline
        # server keeps its identity. Marked status=offline so the editor can
        # flag them rather than imply they are live.
        try:
            _seen = {a.get("agent_id") or a.get("hostname") for a in agents}
            _states = getattr(getattr(self, "deploy", None), "proxmox_states", {}) or {}
            for _hn, _st in _states.items():
                if not _hn or _hn in _seen:
                    continue
                _seen.add(_hn)
                agents.append({
                    "agent_id":  _hn,
                    "hostname":  _hn,
                    "last_seen": (_st or {}).get("last_seen", 0),
                    "version":   (_st or {}).get("version", "unknown"),
                    "status":    "offline",
                })
        except Exception as exc:  # noqa: BLE001 — never fail the map read
            logger.warning("CS_GET_PXMX_SITE_MAP: offline-host merge failed: %s", exc)
        return {"status": "SUCCESS",
                "pxmx_site_map": self.local_store.get_pxmx_site_map(),
                "agents": agents}

    async def _cmd_set_pxmx_site_map(self, d):
        # Validate each assigned site against the sites this tenant's
        # simulation.conf + Central site_mappings actually offer; drop
        # unknown sites (keep the host mapping so the operator can fix the
        # typo in-place) and surface errors. Unknown HOSTS are kept too — a
        # server may be temporarily disconnected but still assigned.
        raw = d.get("pxmx_site_map") if isinstance(d, dict) else None
        raw = raw if isinstance(raw, dict) else (d if isinstance(d, dict) else {})
        errs: list = []
        try:
            import sim_quota
            csc = self.local_store.get_central_sites_config() or {}
            valid = set(sim_quota.available_sites(
                self.settings.config_dir, csc.get("site_mappings")))
            valid.add("Tenant-Wide Pool")  # a server may be in the tenant pool
            # Site Links are the source of truth for wsites (the editor's
            # dropdown offers exactly these); simulation.conf no longer
            # carries wsite entries, so without them every site pool was
            # rejected as unknown.
            for lk in (csc.get("site_links") or []):
                if not isinstance(lk, dict):
                    continue
                for f in ("wsite", "name", "central_site"):
                    v = str(lk.get(f) or "").strip()
                    if v:
                        valid.add(v)
            clean = {}
            for host, site in raw.items():
                h = str(host).strip()
                s = str(site or "").strip()
                if not h:
                    continue
                if s and valid and s not in valid:
                    errs.append(f"{h}: unknown site '{s}'")
                clean[h] = s
        except Exception as exc:  # noqa: BLE001 — never block the save
            logger.warning("CS_SET_PXMX_SITE_MAP: validate failed: %s", exc)
            clean = {str(k).strip(): str(v or "").strip()
                     for k, v in raw.items() if str(k).strip()}
        saved = self.local_store.set_pxmx_site_map(clean)
        # Re-resolve sites on the next sweep — the engine reads the map at
        # the top of each reconcile, so just nudge it now for promptness.
        self._trigger_sim_quota_reconcile()
        return {"status": "SUCCESS", "pxmx_site_map": saved,
                "errors": errs}

    # ── hub-pushed config (CS_CONFIG_UPDATE) ───────────────────────────────
    # Keys the hub sends that map 1:1 to a CSSettings key (consumed by
    # ``CSSettings.usb_config_payload`` → cs_bridge → agent usb_config).
    _HUB_DIRECT_KEYS = (
        "usb_vidpids", "usb_ignored_vidpids",
        "t1_pci_vidpids", "t3_pci_vidpids", "t1_exclude_hosts", "t3_exclude_hosts",
        "usb_auto_provision",
        "usb_missing_timeout", "usb_max_slots", "vm_image_1_pct", "sim_pool",
        "reclone_concurrency", "l1_vlan_start", "l1_vlan_end",
        "vmid_start", "vmid_end", "vm_set_override", "use_all_dongles",
        "guest_agent_watchdog_enabled", "guest_agent_grace_minutes",
        "guest_agent_check_interval_minutes", "guest_agent_reboot_after_minutes",
        "guest_agent_reclone_after_minutes", "watchdog_reboot_enabled",
        "cpu_provision_threshold", "cpu_delete_threshold",
        "mem_provision_threshold", "mem_delete_threshold",
        "protected_vmids",
    )
    # Hub keys that must be renamed to land in their CSSettings counterpart
    # (the hub UI/label uses ``vm_image_*``; the settings store + agent read
    # ``image*_template_*``). Without this remap the template IDs never reach
    # the agent even after certification is unblocked.
    _HUB_KEY_REMAP = {
        "vm_image_1_template_id":  "image1_template_id",
        "vm_image_2_template_id":  "image2_template_id",
        "vm_image_1_template_spec": "image1_template_spec",
        "vm_image_2_template_spec": "image2_template_spec",
    }

    def _merge_central_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Sentinel-merge a Central API config patch into local_store and rebuild
        the poller's ArubaClient. Shared by CS_SET_CENTRAL_CONFIG (standalone
        local UI) and the hub-pushed CS_CONFIG_UPDATE path (_apply_hub_config) so
        BOTH entry points persist creds AND reload the client — mirrors the source
        webui-spoke _apply_hub_config central_config handling (server.py).

        Sentinel rule: an empty/None value KEEPS the stored value (so a partial
        save — e.g. changing only Mode, or a hub push that omits unchanged
        secrets — never wipes creds). A new key with an empty value is still
        written (first-time provisioning of a placeholder field)."""
        current = self.local_store.get_central_config()
        merged = dict(current)
        for k, v in (cfg or {}).items():
            if v not in (None, ""):
                merged[k] = v
            elif k not in current:
                merged[k] = v
        self.local_store.set_central_config(merged)
        self.central_poller.reload()
        return merged

    def _merge_mist_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Sentinel-merge a Mist API config patch into local_store and rebuild
        the poller's MistClient. Twin of ``_merge_central_config``: shared by
        CS_SET_MIST_CONFIG (standalone local UI) and the hub-pushed
        CS_CONFIG_UPDATE path (_apply_hub_config) so BOTH entry points persist
        creds AND reload the client.

        Sentinel rule: an empty/None value KEEPS the stored value (so a partial
        save — e.g. changing only the region host, or a hub push that omits
        unchanged secrets — never wipes the token). A new key with an empty
        value is still written (first-time provisioning of a placeholder field)."""
        current = self.local_store.get_mist_config()
        merged = dict(current)
        for k, v in (cfg or {}).items():
            if v not in (None, ""):
                merged[k] = v
            elif k not in current:
                merged[k] = v
        self.local_store.set_mist_config(merged)
        self.mist_poller.reload()
        return merged

    def _merge_central_on_prem_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Sentinel-merge a Central On-Prem API config patch into local_store and
        rebuild the on-prem poller's ArubaClient. Twin of ``_merge_central_config``:
        shared by CS_SET_CENTRAL_ON_PREM_CONFIG (standalone local UI) and the
        hub-pushed CS_CONFIG_UPDATE path (_apply_hub_config) so BOTH entry points
        persist creds AND reload the on-prem client. Same ArubaClient/API as cloud
        Central — only the config slot + the poller reloaded differ.

        Sentinel rule: an empty/None value KEEPS the stored value (so a partial
        save — e.g. changing only Mode, or a hub push that omits unchanged
        secrets — never wipes creds). A new key with an empty value is still
        written (first-time provisioning of a placeholder field)."""
        current = self.local_store.get_central_on_prem_config()
        merged = dict(current)
        for k, v in (cfg or {}).items():
            if v not in (None, ""):
                merged[k] = v
            elif k not in current:
                merged[k] = v
        self.local_store.set_central_on_prem_config(merged)
        self.central_on_prem_poller.reload()
        return merged

    def _handle_cs_config_update(self, d: Any) -> Dict[str, Any]:
        """CS_CONFIG_UPDATE entry point: partial patches apply as-is; a hub
        ``full_config`` push (reconnect / CS_CONFIG_REQUEST / hourly backstop /
        drift repair) applies ONLY keys whose value changed since this process
        last applied them, so a periodic full refresh never re-fires the
        update_now / quota-reconcile / repo-sync side effects for unchanged
        config. The hub's ``config_hash`` from the last full push is reported
        back in CS_TELEMETRY (``cs_config_hash``) for drift detection."""
        d = d if isinstance(d, dict) else {}
        full = bool(d.get("full_config"))
        cfg_hash = d.get("config_hash")
        patch = {k: v for k, v in d.items() if k not in _CS_CONFIG_META_KEYS}
        to_apply = cs_config_diff(patch, self._cs_applied_hashes()) if full else patch
        applied = self._apply_hub_config(to_apply) if to_apply else []
        hashes = self._cs_applied_hashes()
        for k, v in patch.items():
            hashes[k] = cs_value_hash(v)
        if full and isinstance(cfg_hash, str):
            self._cs_config_hash = cfg_hash
        if full:
            logger.info("CS_CONFIG_UPDATE[full]: %d key(s) received, %d changed",
                        len(patch), len(to_apply))
        return {"status": "SUCCESS", "applied": applied,
                "config_hash": getattr(self, "_cs_config_hash", "") or ""}

    def _cs_applied_hashes(self) -> Dict[str, str]:
        h = getattr(self, "_cs_applied_value_hashes", None)
        if h is None:
            h = {}
            self._cs_applied_value_hashes = h
        return h

    # Simple hub keys stored as-is on local_store via ``set_<key>``. The second
    # field is the required type (the push is ignored otherwise), None to store
    # any value, or a function that coerces the value first.
    #  - effective_sim_quotas: global defaults merged with this tenant's
    #    overrides (enabled-only), the SimQuotaEngine's target set.
    #  - sim_weights / stack_cap / stack_rotation_s: sim-stacking knobs
    #    (SimQuotaEngine._reconcile_stacked); randomizable_sims is the on/off gate.
    #  - harvest_cooldown_s: anti-flap; see SimQuotaEngine._in_harvest_cooldown.
    #  - sim_knob_overrides: hub-learned [simulation] knob floors, layered onto
    #    the served config by client_api (local_store.get_sim_knob_overrides).
    #  - site_source .. ssid_weights: pool / SSID config
    #    (docs/simulation-pool-and-quota-design.md).
    _HUB_STORE_KEYS = (
        ("effective_sim_quotas", list),
        ("sim_shareable", dict),
        ("sim_weights", dict),
        ("stack_cap", None),
        ("stack_rotation_s", None),
        ("harvest_cooldown_s", None),
        ("sim_knob_overrides", dict),
        ("site_source", lambda v: str(v or "pxmx")),
        ("randomizable_sims", list),
        ("random_pool", dict),
        ("ambient_pct", None),
        ("ambient_control", None),
        ("ambient_weights", dict),
        ("ambient_site_weights", dict),
        ("ssid_matrix", list),
        ("ssid_placement", dict),
        ("ssid_weights", list),
        ("ignored_hostnames", list),
    )
    _TIER_PRIORITIES = ("t1_first", "t2_first", "t1_only", "t2_only")
    # Keys whose change means the SimQuotaEngine must re-reconcile: its target
    # list, the site/quota configs, the INI text (bucket-default wsite + sim
    # flags shift a quota's site pool and eligibility), and the csc merges.
    _RECONCILE_KEYS = (
        "effective_sim_quotas", "central_sites_config", "mist_sites_config",
        "sim_conf_override", "user_conf_override", "qt_exclude_sims", "tier_priority",
    )

    def _apply_hub_config(self, patch: Dict[str, Any]) -> list:
        """Apply a hub-pushed CS_CONFIG_UPDATE patch to the cs settings store.

        Mirrors the legacy webui-spoke ``_apply_hub_config`` for the keys this
        spoke consumes (the ``usb_config_payload`` knobs + the sim/user-override
        INI files). Hub keys with no CSSettings equivalent (repo_branch,
        reclone_schedule_*, vm_silent_timeout) are ignored here — they are
        legacy-only and this spoke has no consumer for them.
        Returns the list of applied keys (for the hub log / reply).
        """
        if not isinstance(patch, dict) or not patch:
            return []
        applied: list = []
        self._apply_poller_configs(patch, applied)
        self._apply_store_keys(patch, applied)
        self._apply_csc_merges(patch, applied)
        update = self._settings_update_from_patch(patch, applied)
        self._apply_relay_timeouts(patch, applied)
        # GitHub repo/token (Source of Truth = GitHub) — held in memory only.
        if "github_config" in patch:
            gc = patch.get("github_config")
            self._github_config = dict(gc) if isinstance(gc, dict) else {}
            applied.append("github_config")
        self._apply_conf_overrides(patch, applied)
        self._apply_config_source(patch, applied)
        if applied:
            self.settings.update(update)
        logger.info("CS_CONFIG_UPDATE: applied %s",
                    ", ".join(applied) if applied else "no changes")
        # One reconcile per push; the reconcile lock serializes it with the
        # periodic 60s sweep.
        if any(k in a for a in applied for k in self._RECONCILE_KEYS):
            self._trigger_sim_quota_reconcile()
        # RepoSync: when github_config (creds/branch) or config_source changed,
        # pull the tenant's branch now rather than waiting for the loop interval
        # (e.g. moving a fresh install off main onto the tenant branch at
        # save-time). No-op unless source=github + creds set.
        if "github_config" in patch or "config_source" in patch:
            rs = getattr(self, "repo_sync", None)
            if rs is not None:
                rs.trigger()
        return applied

    def _apply_poller_configs(self, patch: Dict[str, Any], applied: list) -> None:
        """Central / Mist / Central On-Prem creds and site configs, plus hub processing modes.

        Creds (Setup -> <provider> API -> Save): WITHOUT applying them the push is
        silently dropped, the poller keeps _client=None, and the site dropdown and
        Sites/Alerts/Clients tabs stay empty. Site configs (monitored_checks,
        hardware_checks, site_mappings, sim_quotas) are stored and the poller
        reloaded so a hub-side Config -> Sim Quotas save reaches this spoke.
        """
        self._apply_creds(patch, "central_config", self._merge_central_config, applied)
        # Hub processing modes: a "centralized" feature is polled by the HUB, so
        # its spoke poller goes idle (pollers check local_store.hub_polls each
        # cycle). Clear the relayed status now rather than waiting up to a poll
        # interval for the hub dashboard to de-dupe.
        pm = patch.get("processing_modes")
        if "processing_modes" in patch and isinstance(pm, dict):
            self.local_store.set_processing_modes(pm)
            for feature, attr in (("central_api", "central_status"),
                                  ("central_on_prem_api", "central_on_prem_status"),
                                  ("mist_api", "mist_status")):
                if self.local_store.hub_polls(feature):
                    setattr(self, attr, {})
            applied.append("processing_modes")
        self._apply_sites(patch, "central_sites_config", "central_poller", applied)
        self._apply_creds(patch, "mist_config", self._merge_mist_config, applied)
        self._apply_sites(patch, "mist_sites_config", "mist_poller", applied)
        self._apply_creds(patch, "central_on_prem_config",
                          self._merge_central_on_prem_config, applied)
        self._apply_sites(patch, "central_on_prem_sites_config",
                          "central_on_prem_poller", applied)

    @staticmethod
    def _apply_creds(patch, key, merge, applied) -> None:
        if key in patch:
            value = patch.get(key)
            merge(value if isinstance(value, dict) else {})
            applied.append(key)

    def _apply_sites(self, patch, key, poller_attr, applied) -> None:
        value = patch.get(key)
        if key in patch and isinstance(value, dict):
            getattr(self.local_store, f"set_{key}")(value)
            getattr(self, poller_attr).reload()
            applied.append(key)

    def _apply_store_keys(self, patch: Dict[str, Any], applied: list) -> None:
        for key, kind in self._HUB_STORE_KEYS:
            if key not in patch:
                continue
            value = patch[key]
            if isinstance(kind, type):
                if not isinstance(value, kind):
                    continue
            elif kind is not None:
                value = kind(value)
            getattr(self.local_store, f"set_{key}")(value)
            applied.append(key)

    def _apply_csc_merges(self, patch: Dict[str, Any], applied: list) -> None:
        """Keys merged INTO the local central_sites_config (read-modify-write) so
        the SimQuotaEngine reads them from one place; the rest of csc is kept.

        qt_exclude_sims: dongle-quarantine exclusion sims (hub-resolved: tenant
        csc override else the platform default). The engine defaults to the
        locked set when absent.

        tier_priority: the default tier a quota gets when it does not pin one
        (T1 = dedicated PCI radios, T2 = USB dongles). It does NOT gate the
        ambient spread; a quota preempts a background T1 when it needs one.
        Unknown values are dropped so the engine's 't1_first' default stands.
        """
        qt = patch.get("qt_exclude_sims")
        if "qt_exclude_sims" in patch and isinstance(qt, list):
            csc = self.local_store.get_central_sites_config() or {}
            csc["qt_exclude_sims"] = [str(s) for s in qt if str(s).strip()]
            self.local_store.set_central_sites_config(csc)
            applied.append("qt_exclude_sims")
        tp = str(patch.get("tier_priority") or "").strip().lower()
        if "tier_priority" in patch and tp in self._TIER_PRIORITIES:
            csc = self.local_store.get_central_sites_config() or {}
            csc["tier_priority"] = tp
            self.local_store.set_central_sites_config(csc)
            applied.append("tier_priority")

    def _settings_update_from_patch(self, patch: Dict[str, Any], applied: list) -> Dict[str, Any]:
        """CSSettings keys from the patch: direct keys, renamed keys, and the
        N-image ``vm_image_{i}_*`` keys from the VM Images UI."""
        update: Dict[str, Any] = {"hub_managed": True}
        for key in self._HUB_DIRECT_KEYS:
            if key in patch:
                update[key] = patch[key]
                applied.append(key)
        for hub_key, settings_key in self._HUB_KEY_REMAP.items():
            if hub_key in patch:
                update[settings_key] = patch[hub_key]
                applied.append(f"{hub_key}->{settings_key}")
        # vm_image_count and vm_image_{i}_pct are direct settings keys;
        # vm_image_{i}_template_id / _template_spec map to image{i}_*. (i=1/2 may
        # ALSO be covered above; re-applying the same value is harmless.)
        if "vm_image_count" in patch:
            update["vm_image_count"] = patch["vm_image_count"]
            applied.append("vm_image_count")
        for key, value in patch.items():
            index, _, field = key[len("vm_image_"):].partition("_")
            if not key.startswith("vm_image_") or not field or not index.isdigit():
                continue
            if field == "pct":
                update[key] = value
                applied.append(key)
            elif field in ("template_id", "template_spec"):
                settings_key = f"image{index}_{field}"
                update[settings_key] = value
                applied.append(f"{key}->{settings_key}")
        return update

    def _apply_relay_timeouts(self, patch: Dict[str, Any], applied: list) -> None:
        """Spoke-side send_to_agent long-op / fast windows (Setup -> General).
        Not a CSSettings/agent key: read by the SPOKE_RELAY forward."""
        for key, attr in (("agent_relay_timeout_long_s", "_relay_timeout_long"),
                          ("agent_relay_timeout_fast_s", "_relay_timeout_fast")):
            if key in patch:
                try:
                    setattr(self, attr, max(1.0, float(patch[key])))
                    applied.append(key)
                except (TypeError, ValueError):
                    pass

    def _config_source_for(self, patch: Dict[str, Any]) -> str:
        """Effective Source of Truth for this write: the patch value, else the
        persisted flag, else 'github'."""
        if "config_source" in patch:
            return "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
        try:
            return (self.settings.config_dir / "hub-config-source").read_text(
                encoding="utf-8").strip().lower()
        except Exception:  # noqa: BLE001
            return "github"

    def _apply_conf_overrides(self, patch: Dict[str, Any], applied: list) -> None:
        """Write simulation.conf / user-overrides.conf INI text from the hub.

        - Source=GitHub WITH a token: write the REPO file and commit+push it so
          GitHub stays authoritative (the edit survives the next repo sync); the
          stale hub override is removed so load_configs doesn't double-apply it.
        - otherwise: write configs/hub-*-overrides.conf (hub-managed override).
          None clears the override so the base file applies.

        STANDALONE-ONLY push: it fires only for a spoke with no hub (_source stays
        'github' + its own token). A hub-managed spoke gets config_source='hub'
        AND a token-less github_config, so the edit lands as a hub override and
        the hub alone does the commit. Do NOT let an attached spoke push.
        """
        overrides = [o for o in (
            ("sim_conf_override", "hub-sim-overrides.conf", "simulation.conf"),
            ("user_conf_override", "hub-user-overrides.conf", "user-overrides.conf"),
        ) if o[0] in patch]
        if not overrides:
            return
        cfg_dir = self.settings.config_dir
        source = self._config_source_for(patch)
        has_token = bool(str((self._github_config or {}).get("github_token") or "").strip())
        push_map = {}  # repo-relative path -> content
        for override_key, hub_filename, repo_filename in overrides:
            text = patch[override_key]
            if source == "github" and text is not None and not has_token:
                # The token is in memory only, so a spoke restart wipes it until
                # the hub re-delivers github_config. The hub accepted the save,
                # but we can't push: warn LOUDLY instead of a silent revert on
                # the next repo sync, and fall through to the hub-override write
                # so the edit at least applies locally.
                logger.warning(
                    "CS_CONFIG_UPDATE[%s]: %s received with source=github but no "
                    "github_token in memory (spoke restarted since the key was "
                    "delivered?) — will NOT push; the repo file reverts on the next "
                    "sync. Re-save the GitHub credentials (Setup → Sim Config → "
                    "GitHub) to re-deliver the token.",
                    self.spoke_id, repo_filename)
            if source == "github" and has_token and text is not None:
                try:
                    _atomic_write(cfg_dir / repo_filename, str(text))
                    hub_path = cfg_dir / hub_filename  # drop stale hub override
                    if hub_path.exists():
                        hub_path.unlink()
                    push_map[f"configs/{repo_filename}"] = str(text)
                    applied.append(f"{repo_filename}:github")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CS_CONFIG_UPDATE[%s]: %s (github) write failed: %s",
                                   self.spoke_id, repo_filename, exc)
                continue
            override_path = cfg_dir / hub_filename
            try:
                if text is None:
                    if override_path.exists():
                        override_path.unlink()
                    applied.append(f"{override_key}:cleared")
                else:
                    _atomic_write(override_path, str(text))
                    applied.append(f"{override_key}:updated")
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE: %s write failed: %s", override_path, exc)
        if push_map:
            self._schedule_github_push(push_map)
        # Clients only pick up the new INI when update.sh runs, which happens
        # ONLY on ``update_now`` or a VERSION bump. Without this the spoke's
        # override file changes but every client's simulation.conf stays stale.
        # update.sh is idempotent (diffs before mv).
        self._schedule_client_refresh()

    def _schedule_github_push(self, push_map: Dict[str, str]) -> None:
        """Fire-and-forget git commit+push (_apply_hub_config is sync)."""
        try:
            task = asyncio.get_event_loop().create_task(
                self._push_files_to_github(push_map, "WebUI: update simulation config"))
            self._gh_push_tasks.add(task)

            def _push_done(t, _set=self._gh_push_tasks) -> None:
                _set.discard(t)
                if t.cancelled():
                    logger.warning("github push[%s]: task cancelled before completion",
                                   self.spoke_id)
                    return
                exc = t.exception()  # defensive — _push_files_to_github catches its own
                if exc:
                    logger.warning("github push[%s]: task raised %r", self.spoke_id, exc)
            task.add_done_callback(_push_done)
            logger.info("CS_CONFIG_UPDATE[%s]: scheduled github push for %s",
                        self.spoke_id, list(push_map))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_CONFIG_UPDATE[%s]: github push schedule failed: %s",
                           self.spoke_id, exc)

    def _schedule_client_refresh(self) -> None:
        """Fire-and-forget ``update_now`` to every client (kill_switch pattern)."""
        try:
            task = asyncio.get_event_loop().create_task(self._push_config_refresh_to_clients())
            self._gh_push_tasks.add(task)
            task.add_done_callback(lambda t, _set=self._gh_push_tasks: _set.discard(t))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_CONFIG_UPDATE[%s]: update_now schedule failed: %s",
                           self.spoke_id, exc)

    def _apply_config_source(self, patch: Dict[str, Any], applied: list) -> None:
        """Persist the Config Source of Truth ('hub' | 'github') as a flag file.
        In 'hub' mode sim_config.load_configs uses the hub override files as the
        WHOLE config and ignores the repo base, so a repo pull can never revert
        hub edits. 'github' (default) keeps the repo-base merge."""
        if "config_source" not in patch:
            return
        src = "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
        try:
            (self.settings.config_dir / "hub-config-source").write_text(src, encoding="utf-8")
            applied.append(f"config_source:{src}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS_CONFIG_UPDATE: config_source write failed: %s", exc)
