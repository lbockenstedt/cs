"""Config command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_config`` scans only its own command group and
returns the result dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
import asyncio
from pathlib import Path

import sim_config
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


class ConfigCommandsMixin:
    async def _dispatch_config(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:

        # ── repo / update status (Setup → Diagnostics → API Server) ──────────
        # Lets an operator confirm FROM THE UI which branch + build this spoke's
        # checkout is serving to clients — the answer to "did my push reach the
        # spoke?" without touching the CLI. Reads the served scripts dir
        # (clients/linux, the same dir /api/scripts serves) + the tenant's
        # github_config branch. Best-effort; never raises.
        if cmd in ("CS_GET_REPO_STATUS",):
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
        if cmd in ("CS_GET_CONFIG",):
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

        if cmd in ("CS_UPDATE_CONFIG", "UPDATE_CONFIG"):
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

        if cmd in ("CS_UPDATE_USER_OVERRIDES",):
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

        if cmd == "CS_CONFIG_UPDATE":
            # Hub pushes hub-owned provisioning config (usb_vidpids,
            # usb_ignored_vidpids, usb_auto_provision, template ids, VLAN
            # ranges, reclone concurrency, ... + optional sim/user-overrides
            # INI text). The legacy cs webui-spoke applied these via
            # _apply_hub_config; this spoke MUST do the same or certification
            # pushes are silently dropped: usb_vidpids stays "[]" in settings,
            # the cs_bridge pulls an empty ``vidpids`` via CS_GET_USB_CONFIG
            # every 60s, the agent's _dongle_vidpids returns 0, and
            # auto-provision never fires ("no dongle_vidpids configured").
            applied = self._apply_hub_config(d if isinstance(d, dict) else {})
            return {"status": "SUCCESS", "applied": applied}

        # ── local Setup-tab config (hub_config / central) ───────────────────
        # This spoke owns these knobs itself now (local_store.py) instead of
        # relaying them from an LM hub tenant store — see that module's
        # docstring. _apply_hub_config below is the SAME logic CS_CONFIG_UPDATE
        # already uses, so a locally-saved hub_config flows to the settings
        # store (and any cs-dialed pxmx agent) exactly like a hub-pushed one.
        if cmd in ("CS_GET_HUB_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_hub_config()}

        if cmd in ("CS_SET_HUB_CONFIG",):
            enabled = bool(d.get("hub_config_enabled", False))
            hc = d.get("hub_config") or {}
            self.local_store.set_hub_config(enabled, hc)
            applied = self._apply_hub_config(hc) if enabled else []
            return {"status": "SUCCESS", "applied": applied}

        if cmd in ("CS_RESET_HUB_CONFIG",):
            result = self.local_store.reset_hub_config()
            if result.get("hub_config_enabled"):
                self._apply_hub_config(result.get("hub_config") or {})
            return {"status": "SUCCESS", **result}

        if cmd in ("CS_GET_CENTRAL_CONFIG",):
            return {"status": "SUCCESS", "central_config": self.local_store.get_central_config()}

        if cmd in ("CS_SET_CENTRAL_CONFIG",):
            self._merge_central_config(d.get("central_config") or {})
            return {"status": "SUCCESS"}

        if cmd in ("CS_GET_CENTRAL_SITES_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_central_sites_config()}

        if cmd in ("CS_SET_CENTRAL_SITES_CONFIG",):
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

        if cmd in ("CS_GET_CENTRAL_AVAILABLE",):
            return await self.central_poller.available_checks()

        if cmd in ("CS_TEST_CENTRAL",):
            return await self.central_poller.test_connection()

        if cmd in ("CS_CENTRAL_BROWSE",):
            return await self.central_poller.browse()

        # ── Aruba Central On-Prem (twin of the Central handlers above — a SECOND
        # Aruba Central instance via the SAME ArubaClient/API, just a separate
        # config + sites-config + status slot). The on-prem poller
        # (self.central_on_prem_poller) reads/writes its own slots so cloud Central
        # and on-prem never step on each other.
        if cmd in ("CS_GET_CENTRAL_ON_PREM_CONFIG",):
            return {"status": "SUCCESS",
                    "central_on_prem_config": self.local_store.get_central_on_prem_config()}

        if cmd in ("CS_SET_CENTRAL_ON_PREM_CONFIG",):
            self._merge_central_on_prem_config(d.get("central_on_prem_config") or {})
            return {"status": "SUCCESS"}

        if cmd in ("CS_GET_CENTRAL_ON_PREM_SITES_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_central_on_prem_sites_config()}

        if cmd in ("CS_SET_CENTRAL_ON_PREM_SITES_CONFIG",):
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

        if cmd in ("CS_GET_CENTRAL_ON_PREM_AVAILABLE",):
            return await self.central_on_prem_poller.available_checks()

        if cmd in ("CS_TEST_CENTRAL_ON_PREM",):
            return await self.central_on_prem_poller.test_connection()

        if cmd in ("CS_CENTRAL_ON_PREM_BROWSE",):
            return await self.central_on_prem_poller.browse()

        if cmd == "CS_GET_CENTRAL_ON_PREM_HEALTH":
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
        if cmd in ("CS_GET_MIST_CONFIG",):
            return {"status": "SUCCESS", "mist_config": self.local_store.get_mist_config()}

        if cmd in ("CS_SET_MIST_CONFIG",):
            self._merge_mist_config(d.get("mist_config") or {})
            return {"status": "SUCCESS"}

        if cmd in ("CS_GET_MIST_SITES_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_mist_sites_config()}

        if cmd in ("CS_SET_MIST_SITES_CONFIG",):
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

        if cmd in ("CS_GET_MIST_AVAILABLE",):
            return await self.mist_poller.available_checks()

        if cmd in ("CS_TEST_MIST",):
            return await self.mist_poller.test_connection()

        if cmd in ("CS_MIST_BROWSE",):
            return await self.mist_poller.browse()

        if cmd == "CS_GET_MIST_HEALTH":
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

        if cmd == "CS_GET_HEALTH":
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

        if cmd == "CS_GET_SIM_QUOTA_CATALOG":
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

        if cmd == "CS_GET_SIM_QUOTA_STATE":
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

        if cmd == "CS_RESET_SIM_QUOTA":
            # Clear the engine ledger + engine-set overrides and reconcile fresh —
            # a clean re-shuffle (operator "Reset & Reshuffle" button).
            eng = getattr(self, "sim_quota_engine", None)
            if eng is None:
                return {"status": "ERROR", "message": "sim quota engine unavailable"}
            actions = await eng.reset()
            return {"status": "SUCCESS", "actions": actions}

        if cmd == "CS_GET_DHCP_HEALTH":
            # Everything needed to answer "why is the sim network not handing
            # out IPs" WITHOUT ssh. Built from a real incident: the config was
            # valid and the NIC was correct, but AppArmor denied Kea its
            # runtime files, so it crash-looped with no lease file. Diagnosing
            # that took five round trips because each signal lived somewhere
            # different (systemctl / journal / dmesg / the conf / ip addr).
            # Collect them together and let the UI render the verdict.
            import json as _json
            import os as _os
            import re as _re
            import subprocess as _sp

            def _run(argv, timeout=8):
                try:
                    r = _sp.run(argv, capture_output=True, text=True, timeout=timeout)
                    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
                except Exception as exc:  # noqa: BLE001
                    return -1, "", str(exc)

            out: Dict[str, Any] = {"status": "SUCCESS", "units": {}, "notes": []}

            # ── units ────────────────────────────────────────────────────────
            for unit in ("kea-dhcp4-sim", "kea-ctrl-agent-sim"):
                props = {}
                rc, so, _ = _run(["systemctl", "show", unit, "--no-pager",
                                  "-p", "LoadState,ActiveState,SubState,Result,"
                                        "NRestarts,ExecMainStatus,UnitFileState"])
                for line in so.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        props[k] = v
                out["units"][unit] = props

            # ── the config Kea is actually running ───────────────────────────
            conf = "/etc/kea/kea-dhcp4-sim.conf"
            out["conf_path"] = conf
            out["conf_exists"] = _os.path.isfile(conf)
            want_ifaces, lease_path, subnet, pool = [], "", "", ""
            if out["conf_exists"]:
                try:
                    with open(conf) as fh:
                        raw = fh.read()
                    cfg = _json.loads(_re.sub(r"^\s*//.*$", "", raw, flags=_re.M))
                    d4 = (cfg or {}).get("Dhcp4", {}) or {}
                    want_ifaces = ((d4.get("interfaces-config") or {}).get("interfaces") or [])
                    lease_path = ((d4.get("lease-database") or {}).get("name") or "")
                    subs = d4.get("subnet4") or []
                    if subs:
                        subnet = subs[0].get("subnet", "")
                        pools = subs[0].get("pools") or []
                        pool = (pools[0] or {}).get("pool", "") if pools else ""
                except Exception as exc:  # noqa: BLE001
                    out["notes"].append(f"could not parse {conf}: {exc}")
            out.update({"interfaces_configured": want_ifaces, "subnet": subnet,
                        "pool": pool, "lease_file": lease_path})

            # ── does that interface exist, and does it hold the server IP? ───
            rc, so, _ = _run(["ip", "-o", "-4", "addr", "show"])
            present = {}
            for line in so.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    present.setdefault(parts[1], []).append(parts[3])
            out["interfaces_present"] = present
            out["interface_ok"] = bool(want_ifaces) and all(i in present for i in want_ifaces)
            # A renamed NIC is the quiet killer: the conf still names ens19, the
            # box now calls it ens20, Kea binds nothing and serves nobody with
            # no error beyond "listening on interface ens19". Surface both the
            # mismatch AND the likely replacement so the UI can name the fix.
            # Deliberately NOT auto-rebound here — picking the wrong NIC would
            # put a DHCP server on the production LAN.
            out["interface_missing"] = [i for i in want_ifaces if i not in present]
            gw = ""
            for _sub in ([subnet] if subnet else []):
                gw = _sub.split("/")[0].rsplit(".", 1)[0] + ".1" if "." in _sub else ""
            out["sim_gateway"] = gw
            holder = [i for i, addrs in present.items()
                      if gw and any(a.startswith(gw + "/") for a in addrs)]
            out["interface_holding_gateway"] = holder
            # Same rule the installer uses at install time: first non-loopback
            # NIC is the uplink, second is the sim network.
            rc_l, so_l, _ = _run(["ip", "-o", "link", "show"])
            order = []
            for line in so_l.splitlines():
                try:
                    nm = line.split(":")[1].strip().split("@")[0]
                except Exception:  # noqa: BLE001
                    continue
                if nm and nm != "lo" and nm not in order:
                    order.append(nm)
            out["nic_order"] = order
            # Best signal for "which NIC is the sim network": the one with NO
            # IPv4 address. The uplink always has one; the sim NIC only gets
            # 169.253.1.1 from us, so before/without that it is bare. That is
            # more reliable than "second by index", which depends on enumeration
            # order surviving a rebuild. Preference order: the NIC already
            # holding the sim gateway (definitive) -> the address-less NIC ->
            # index 1 (what the installer used historically).
            bare = [n for n in order if not present.get(n)]
            out["interfaces_without_ip"] = bare
            out["interface_suggested"] = (holder[0] if holder
                                          else (bare[0] if bare
                                                else (order[1] if len(order) > 1 else "")))

            # ── lease DB: the thing that proves it is actually serving ───────
            lease_info = {"exists": False, "leases": 0, "size": 0, "mtime": 0,
                          "readable": True}
            if lease_path:
                try:
                    st = _os.stat(lease_path)
                    lease_info.update({"exists": True, "size": st.st_size,
                                       "mtime": int(st.st_mtime)})
                    with open(lease_path) as fh:
                        # header + one row per lease
                        lease_info["leases"] = max(0, sum(1 for _ in fh) - 1)
                except FileNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    # The file is 0640 _kea:_kea, so the spoke user often cannot
                    # read it. Say UNKNOWN — reporting 0 here reads as "running
                    # but nobody has leased yet", which is a different (and
                    # much less alarming) statement than "I cannot tell".
                    lease_info["readable"] = False
                    lease_info["leases"] = None
                    out["notes"].append(f"lease count unavailable: {exc}")
            out["lease_db"] = lease_info

            # ── config test (catches a bad conf independent of the unit) ─────
            rc, so, se = _run(["kea-dhcp4", "-t", conf], timeout=15)
            out["config_test"] = {"ok": rc == 0, "detail": (se or so)[-1500:]}

            # ── last fatal line + AppArmor denials ───────────────────────────
            # Both need privilege the spoke user may not have; say so explicitly
            # rather than render an empty panel that looks like "no problems".
            rc, so, se = _run(["journalctl", "-u", "kea-dhcp4-sim", "--no-pager",
                               "-n", "60", "-o", "cat"])
            if rc == 0 and so:
                fatal = [l for l in so.splitlines()
                         if "ERROR" in l or "Fatal" in l or "DENIED" in l]
                # Distinguish real errors from tail-context. Without this the UI
                # renders DHCP4_LEASE_ALLOC / DHCPACK success lines under a
                # heading that says "Last errors", which reads as a fault on a
                # spoke that is working perfectly.
                out["last_errors"] = fatal[-6:] or so.splitlines()[-3:]
                out["last_errors_are_fatal"] = bool(fatal)
            else:
                out["last_errors"] = []
                out["notes"].append(
                    "journal not readable by the spoke user — add it to the "
                    "systemd-journal group to surface Kea's own errors here")

            rc, so, se = _run(["dmesg"], timeout=10)
            if rc == 0 and so:
                den = [l for l in so.splitlines()
                       if "apparmor" in l.lower() and "kea" in l.lower() and "DENIED" in l]
                # dmesg is a RING BUFFER: a denial from hours ago stays in it
                # forever and reads as if it were happening now. That made a
                # recovered spoke look broken. The leading "[   1105.38]" is
                # seconds since boot, so age = uptime - that. Report the age and
                # whether any denial is RECENT; the UI greys out old ones.
                try:
                    with open("/proc/uptime") as _u:
                        _up = float(_u.read().split()[0])
                except Exception:  # noqa: BLE001
                    _up = 0.0
                _rows, _recent = [], False
                for _l in den[-8:]:
                    _age = None
                    _m = _re.match(r"^\[\s*(\d+)\.\d+\]", _l)
                    if _m and _up:
                        _age = max(0, int(_up - int(_m.group(1))))
                        if _age <= 900:      # within 15 min = still happening
                            _recent = True
                    _rows.append({"line": _l, "age_s": _age})
                out["apparmor_denials"] = [r["line"] for r in _rows]   # back-comn't
                out["apparmor_denial_rows"] = _rows
                out["apparmor_recent"] = _recent
            else:
                out["apparmor_denials"] = []
                out["notes"].append(
                    "dmesg not readable (kernel.dmesg_restrict) — AppArmor "
                    "denials cannot be shown; run: dmesg | grep -i 'apparmor.*kea'")
            return out

        if cmd == "CS_GET_PXMX_SITE_MAP":
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

        if cmd == "CS_SET_PXMX_SITE_MAP":
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
        return None

    # ── hub-pushed config (CS_CONFIG_UPDATE) ───────────────────────────────
    # Keys the hub sends that map 1:1 to a CSSettings key (consumed by
    # ``CSSettings.usb_config_payload`` → cs_bridge → agent usb_config).
    _HUB_DIRECT_KEYS = (
        "usb_vidpids", "usb_ignored_vidpids",
        "t1_pci_vidpids", "t3_pci_vidpids", "usb_auto_provision",
        "usb_missing_timeout", "usb_max_slots", "vm_image_1_pct",
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

    def _apply_hub_config(self, patch: Dict[str, Any]) -> list:
        """Apply a hub-pushed CS_CONFIG_UPDATE patch to the cs settings store.

        Mirrors the legacy webui-spoke ``_apply_hub_config`` for the keys this
        spoke consumes (the ``usb_config_payload`` knobs + the sim/user-override
        INI files). Hub keys with no CSSettings equivalent (repo_branch,
        reclone_schedule_*, vm_silent_timeout, ignored_hostnames) are ignored
        here — they are legacy-only and this spoke has no consumer for them.
        Returns the list of applied keys (for the hub log / reply).
        """
        if not isinstance(patch, dict) or not patch:
            return []
        update: Dict[str, Any] = {"hub_managed": True}
        applied: list = []
        # Aruba Central creds pushed from the hub (Setup -> Central API -> Save).
        # WITHOUT this branch the push is silently dropped: local_store never gets
        # the creds and the poller keeps _client=None, so browse() returns zero
        # sites and the Central-site dropdown / Sites-Alerts-Clients tabs stay
        # empty. The source webui-spoke applies central_config in _apply_hub_config
        # too (server.py) — this mirrors it via the shared sentinel-merge helper.
        if "central_config" in patch:
            cc = patch.get("central_config")
            self._merge_central_config(cc if isinstance(cc, dict) else {})
            applied.append("central_config")
        # Hub-pushed central_sites_config (monitored_checks/hardware_checks/
        # site_mappings + sim_quotas): apply to local_store + reload the poller
        # so a hub-side Config → Sim Quotas / Central save reaches this spoke.
        if "central_sites_config" in patch:
            csc = patch.get("central_sites_config")
            if isinstance(csc, dict):
                self.local_store.set_central_sites_config(csc)
                self.central_poller.reload()
                applied.append("central_sites_config")
        # Juniper Mist creds pushed from the hub (Setup -> Mist API -> Save).
        # Twin of the central_config branch above: WITHOUT it the push is
        # silently dropped and the Mist poller keeps _client=None, so the Mist
        # site dropdown / Sites-Alerts-Clients tabs stay empty.
        if "mist_config" in patch:
            mc = patch.get("mist_config")
            self._merge_mist_config(mc if isinstance(mc, dict) else {})
            applied.append("mist_config")
        # Hub-pushed mist_sites_config (monitored_checks/hardware_checks/
        # site_mappings + sim_quotas): apply to local_store + reload the poller
        # so a hub-side Config -> Sim Quotas / Mist save reaches this spoke.
        if "mist_sites_config" in patch:
            msc = patch.get("mist_sites_config")
            if isinstance(msc, dict):
                self.local_store.set_mist_sites_config(msc)
                self.mist_poller.reload()
                applied.append("mist_sites_config")
        # Aruba Central On-Prem creds pushed from the hub (Setup -> Central On-Prem
        # API -> Save). Twin of the central_config branch above (same ArubaClient,
        # separate config slot + on-prem poller): WITHOUT it the push is silently
        # dropped and the on-prem poller keeps _client=None, so the on-prem site
        # dropdown / Sites-Alerts-Clients tabs stay empty.
        if "central_on_prem_config" in patch:
            opc = patch.get("central_on_prem_config")
            self._merge_central_on_prem_config(opc if isinstance(opc, dict) else {})
            applied.append("central_on_prem_config")
        # Hub-pushed central_on_prem_sites_config (monitored_checks/hardware_checks/
        # site_mappings + sim_quotas with Central On-Prem:-prefixed ids): apply to
        # local_store + reload the on-prem poller so a hub-side Config -> Sim Quotas
        # / Central On-Prem save reaches this spoke.
        if "central_on_prem_sites_config" in patch:
            opc = patch.get("central_on_prem_sites_config")
            if isinstance(opc, dict):
                self.local_store.set_central_on_prem_sites_config(opc)
                self.central_on_prem_poller.reload()
                applied.append("central_on_prem_sites_config")
        # Hub-pushed effective sim quotas (global defaults merged with this
        # tenant's overrides, enabled-only) — the SimQuotaEngine's input. Persist
        # + trigger a reconcile so the engine picks up the new target set.
        if "effective_sim_quotas" in patch:
            eff = patch.get("effective_sim_quotas")
            if isinstance(eff, list):
                self.local_store.set_effective_sim_quotas(eff)
                applied.append("effective_sim_quotas")
        if "sim_shareable" in patch:
            sh = patch.get("sim_shareable")
            if isinstance(sh, dict):
                self.local_store.set_sim_shareable(sh)
                applied.append("sim_shareable")
        # Sim-stacking knobs (weighted multi-sim fill of the spare pool) — the
        # SimQuotaEngine._reconcile_stacked inputs. randomizable_sims (below) is
        # the on/off gate; these tune breadth + depth + churn.
        if "sim_weights" in patch:
            sw = patch.get("sim_weights")
            if isinstance(sw, dict):
                self.local_store.set_sim_weights(sw)
                applied.append("sim_weights")
        if "stack_cap" in patch:
            self.local_store.set_stack_cap(patch.get("stack_cap"))
            applied.append("stack_cap")
        if "stack_rotation_s" in patch:
            self.local_store.set_stack_rotation_s(patch.get("stack_rotation_s"))
            applied.append("stack_rotation_s")
        # Harvest cooldown (anti-flap): a client can't be re-harvested for this
        # long after an alert-sim assignment. See SimQuotaEngine._in_harvest_cooldown.
        if "harvest_cooldown_s" in patch:
            self.local_store.set_harvest_cooldown_s(patch.get("harvest_cooldown_s"))
            applied.append("harvest_cooldown_s")
        # Knob-floor learner: the hub-computed [simulation] intensity knob values
        # (e.g. {"dns_fail_rate": 400}). Layered onto the served config at serve
        # time by client_api — see local_store.get_sim_knob_overrides.
        if "sim_knob_overrides" in patch:
            ko = patch.get("sim_knob_overrides")
            if isinstance(ko, dict):
                self.local_store.set_sim_knob_overrides(ko)
                applied.append("sim_knob_overrides")
        # Pool / SSID config (docs/simulation-pool-and-quota-design.md).
        if "site_source" in patch:
            self.local_store.set_site_source(str(patch.get("site_source") or "pxmx"))
            applied.append("site_source")
        if "randomizable_sims" in patch:
            rs = patch.get("randomizable_sims")
            if isinstance(rs, list):
                self.local_store.set_randomizable_sims(rs)
                applied.append("randomizable_sims")
        if "random_pool" in patch:
            rp = patch.get("random_pool")
            if isinstance(rp, dict):
                self.local_store.set_random_pool(rp)
                applied.append("random_pool")
        if "ambient_pct" in patch:
            self.local_store.set_ambient_pct(patch.get("ambient_pct"))
            applied.append("ambient_pct")
        if "ambient_control" in patch:
            self.local_store.set_ambient_control(patch.get("ambient_control"))
            applied.append("ambient_control")
        if "ambient_weights" in patch:
            aw = patch.get("ambient_weights")
            if isinstance(aw, dict):
                self.local_store.set_ambient_weights(aw)
                applied.append("ambient_weights")
        if "ambient_site_weights" in patch:
            asw = patch.get("ambient_site_weights")
            if isinstance(asw, dict):
                self.local_store.set_ambient_site_weights(asw)
                applied.append("ambient_site_weights")
        if "ssid_matrix" in patch:
            sm = patch.get("ssid_matrix")
            if isinstance(sm, list):
                self.local_store.set_ssid_matrix(sm)
                applied.append("ssid_matrix")
        if "ssid_placement" in patch:
            sp = patch.get("ssid_placement")
            if isinstance(sp, dict):
                self.local_store.set_ssid_placement(sp)
                applied.append("ssid_placement")
        if "ssid_weights" in patch:
            sw = patch.get("ssid_weights")
            if isinstance(sw, list):
                self.local_store.set_ssid_weights(sw)
                applied.append("ssid_weights")
        # Dongle-quarantine exclusion sims (hub-pushed resolved set: per-tenant
        # csc override else the platform-wide default). Merged INTO the local
        # central_sites_config (read-modify-write) so the SimQuotaEngine reads it
        # via local_store.get_central_sites_config(); the rest of csc is preserved.
        # The engine defaults to the locked set when this key is absent.
        if "qt_exclude_sims" in patch:
            qt = patch.get("qt_exclude_sims")
            if isinstance(qt, list):
                csc = self.local_store.get_central_sites_config() or {}
                csc["qt_exclude_sims"] = [str(s) for s in qt if str(s).strip()]
                self.local_store.set_central_sites_config(csc)
                applied.append("qt_exclude_sims")
        # Quota tier priority (T1 = dedicated PCI radios, T2 = USB dongles).
        # Sets the default tier a quota gets when it does not pin its own —
        # T1s are the reliable clients and should run the issue-generating quota
        # sims. It does NOT gate the ambient spread: every spare client does
        # background work, and a quota preempts a background T1 when it needs
        # one. Merged into csc the same way as qt_exclude_sims so SimQuotaEngine
        # reads it from one place. Unknown values are dropped rather than stored
        # so the engine's 't1_first' default (historical behaviour) stands.
        if "tier_priority" in patch:
            tp = str(patch.get("tier_priority") or "").strip().lower()
            if tp in ("t1_first", "t2_first", "t1_only", "t2_only"):
                csc = self.local_store.get_central_sites_config() or {}
                csc["tier_priority"] = tp
                self.local_store.set_central_sites_config(csc)
                applied.append("tier_priority")
        if "ignored_hostnames" in patch:
            ih = patch.get("ignored_hostnames")
            if isinstance(ih, list):
                self.local_store.set_ignored_hostnames(ih)
                applied.append("ignored_hostnames")
        for key in self._HUB_DIRECT_KEYS:
            if key in patch:
                update[key] = patch[key]
                applied.append(key)
        for hub_key, settings_key in self._HUB_KEY_REMAP.items():
            if hub_key in patch:
                update[settings_key] = patch[hub_key]
                applied.append(f"{hub_key}->{settings_key}")
        # N-image generic keys from the new VM Images UI (any i). vm_image_count +
        # vm_image_{i}_pct are direct settings keys; vm_image_{i}_template_id /
        # _template_spec remap to image{i}_*. (i=1/2 may ALSO be covered by the
        # static lists above — re-applying the same value is harmless/idempotent.)
        if "vm_image_count" in patch:
            update["vm_image_count"] = patch["vm_image_count"]
            applied.append("vm_image_count")
        for _k, _v in patch.items():
            if not _k.startswith("vm_image_"):
                continue
            _parts = _k[len("vm_image_"):].split("_", 1)
            if len(_parts) != 2 or not _parts[0].isdigit():
                continue
            _i, _field = _parts[0], _parts[1]
            if _field == "pct":
                update[_k] = _v
                applied.append(_k)
            elif _field in ("template_id", "template_spec"):
                _sk = f"image{_i}_{_field}"
                update[_sk] = _v
                applied.append(f"{_k}->{_sk}")
        # Spoke-side relay timeouts (send_to_agent long-op / fast windows) —
        # hub-configurable via Setup → General. Not a CSSettings/agent key: stored
        # on the spoke and read by the SPOKE_RELAY forward (send_to_agent).
        for _k, _attr in (("agent_relay_timeout_long_s", "_relay_timeout_long"),
                          ("agent_relay_timeout_fast_s", "_relay_timeout_fast")):
            if _k in patch:
                try:
                    setattr(self, _attr, max(1.0, float(patch[_k])))
                    applied.append(_k)
                except (TypeError, ValueError):
                    pass
        # GitHub repo/token (Source of Truth = GitHub) — held in memory only.
        if "github_config" in patch:
            gc = patch.get("github_config")
            self._github_config = dict(gc) if isinstance(gc, dict) else {}
            applied.append("github_config")

        # Effective Source of Truth for this write: prefer the patch value, else
        # the persisted flag, else 'github'.
        _cfg_dir = self.settings.config_dir
        if "config_source" in patch:
            _source = "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
        else:
            try:
                _source = (_cfg_dir / "hub-config-source").read_text(encoding="utf-8").strip().lower()
            except Exception:  # noqa: BLE001
                _source = "github"
        _gh_token = bool(str((self._github_config or {}).get("github_token") or "").strip())

        # Optional simulation.conf / user-overrides.conf INI text.
        #  - Source=GitHub WITH a token: write the REPO file and commit+push it so
        #    GitHub stays authoritative (edit survives the next repo sync); the
        #    stale hub override is removed so load_configs doesn't double-apply it.
        #  - otherwise: write configs/hub-*-overrides.conf (hub-managed override).
        #    None = clear the override so the base file applies.
        # STANDALONE-ONLY: the commit+push branch below fires only for a genuinely
        # standalone spoke (no hub → _source stays 'github' + its own token). When
        # a hub manages this spoke it sends config_source='hub' AND a token-less
        # github_config (replacing _github_config above → _gh_token=False), so BOTH
        # conditions fail and the edit lands as a hub-override — the hub is the
        # sole GitHub client and does the commit. Do NOT let an attached spoke push.
        _push_map = {}  # repo-relative path -> content, for the fetch+reset+push
        _client_config_changed = False  # sim/user override changed → push update_now
        for override_key, hub_filename, repo_filename in (
            ("sim_conf_override", "hub-sim-overrides.conf", "simulation.conf"),
            ("user_conf_override", "hub-user-overrides.conf", "user-overrides.conf"),
        ):
            if override_key not in patch:
                continue
            _client_config_changed = True
            text = patch[override_key]
            if _source == "github" and text is not None and not _gh_token:
                # Source=GitHub but this spoke has NO token in memory. The token
                # is held in-memory only (never persisted), so a spoke restart
                # (hourly self-update / reboot / crash) wipes it until the hub
                # re-delivers github_config. The hub accepted the save (its store
                # has the token), but we can't push — warn LOUDLY so the operator
                # catches it, instead of seeing a silent revert on the next repo
                # sync (the "old GitHub version on sync" symptom). Fall through to
                # the hub-override write so the edit at least applies locally.
                logger.warning(
                    "CS_CONFIG_UPDATE[%s]: %s received with source=github but no "
                    "github_token in memory (spoke restarted since the key was "
                    "delivered?) — will NOT push; the repo file reverts on the next "
                    "sync. Re-save the GitHub credentials (Setup → Sim Config → "
                    "GitHub) to re-deliver the token.",
                    self.spoke_id, repo_filename)
            if _source == "github" and _gh_token and text is not None:
                repo_path = _cfg_dir / repo_filename
                try:
                    repo_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = repo_path.with_suffix(".tmp")
                    tmp.write_text(str(text), encoding="utf-8")
                    tmp.replace(repo_path)   # immediate local effect
                    hub_path = _cfg_dir / hub_filename  # drop stale hub override
                    if hub_path.exists():
                        hub_path.unlink()
                    _push_map[f"configs/{repo_filename}"] = str(text)
                    applied.append(f"{repo_filename}:github")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CS_CONFIG_UPDATE[%s]: %s (github) write failed: %s",
                                   self.spoke_id, repo_filename, exc)
                continue
            override_path = _cfg_dir / hub_filename
            try:
                if text is None:
                    if override_path.exists():
                        override_path.unlink()
                    applied.append(f"{override_key}:cleared")
                else:
                    override_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = override_path.with_suffix(".tmp")
                    tmp.write_text(str(text), encoding="utf-8")
                    tmp.replace(override_path)
                    applied.append(f"{override_key}:updated")
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE: %s write failed: %s",
                               override_path, exc)
        # Fire-and-forget the git commit+push (async; _apply_hub_config is sync).
        if _push_map:
            try:
                _t = asyncio.get_event_loop().create_task(
                    self._push_files_to_github(_push_map, "WebUI: update simulation config"))
                self._gh_push_tasks.add(_t)
                def _push_done(t, _set=self._gh_push_tasks) -> None:
                    _set.discard(t)
                    if t.cancelled():
                        logger.warning("github push[%s]: task cancelled before completion",
                                       self.spoke_id)
                        return
                    exc = t.exception()  # defensive — _push_files_to_github catches its own
                    if exc:
                        logger.warning("github push[%s]: task raised %r", self.spoke_id, exc)
                _t.add_done_callback(_push_done)
                logger.info("CS_CONFIG_UPDATE[%s]: scheduled github push for %s",
                            self.spoke_id, list(_push_map))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE[%s]: github push schedule failed: %s",
                               self.spoke_id, exc)
        # A sim/user override change is only useful if the clients re-fetch it.
        # update.sh (which fetches /api/config + /api/config/overrides and diffs)
        # runs ONLY on an ``update_now`` command or a VERSION bump — there is no
        # periodic config-pull timer (the 1-min watchdog runs sys_mon.sh, not
        # update.sh). So without enqueueing ``update_now`` here, a hub-side edit
        # writes the spoke's override file but the client's local simulation.conf
        # stays stale until the next manual update / version bump. Mirror the
        # kill_switch "all clients" pattern: enqueue update_now to every
        # registered client and push it live to any currently connected. Fire-and-
        # forget (this method is sync); update.sh is idempotent (diffs before mv).
        if _client_config_changed:
            try:
                _t2 = asyncio.get_event_loop().create_task(
                    self._push_config_refresh_to_clients())
                self._gh_push_tasks.add(_t2)
                _t2.add_done_callback(
                    lambda t, _set=self._gh_push_tasks: _set.discard(t))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE[%s]: update_now schedule failed: %s",
                               self.spoke_id, exc)
        # Config Source of Truth ('hub' | 'github'). In 'hub' mode sim_config.
        # load_configs uses the hub override files as the WHOLE config and ignores
        # the repo base (so a repo pull can never revert hub edits). Persisted as a
        # flag file the loader reads. Default 'github' preserves the repo-base merge.
        if "config_source" in patch:
            src = "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
            try:
                (self.settings.config_dir / "hub-config-source").write_text(src, encoding="utf-8")
                applied.append(f"config_source:{src}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE: config_source write failed: %s", exc)
        if applied:
            self.settings.update(update)
        logger.info("CS_CONFIG_UPDATE: applied %s",
                    ", ".join(applied) if applied else "no changes")
        # Re-reconcile the SimQuotaEngine when a CS_CONFIG_UPDATE changed anything
        # the engine cares about: the effective quota list (the engine's target),
        # central_sites_config (sim_quotas + site_mappings + monitored_checks), or
        # the sim/user-override INI text (bucket-default wsite + sim flags → a
        # quota's site pool + exclusivity eligibility can shift). One trigger per
        # push; the reconcile lock serializes it with the periodic 60s sweep.
        _reconcile_keys = (
            "effective_sim_quotas", "central_sites_config",
            "mist_sites_config",
            "sim_conf_override", "user_conf_override", "qt_exclude_sims",
            "tier_priority",
        )
        if any(k in a for a in applied for k in _reconcile_keys):
            self._trigger_sim_quota_reconcile()
        # RepoSync: when github_config (creds/branch) or config_source changed,
        # pull the tenant's branch immediately rather than waiting up to the
        # loop interval (e.g. switching a fresh install off main onto the
        # tenant branch at save-time). No-op unless source=github + creds set.
        if "github_config" in patch or "config_source" in patch:
            rs = getattr(self, "repo_sync", None)
            if rs is not None:
                rs.trigger()
        return applied
