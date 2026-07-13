"""SimQuotaEngine — keeps each declared sim quota filled from the online pool.

The hub merges platform-wide defaults + a tenant's overrides into
``effective_sim_quotas`` (enabled-only) and pushes them to this cs spoke
(``CS_CONFIG_UPDATE`` → ``_apply_hub_config`` → ``local_store.set_effective_sim_quotas``).
This engine reconciles the spoke's client registry against that target list:

  For each quota (sim_id, count, site):
    * keep the ledger's assigned clients that are still online, in-site, and
      still running the sim;
    * drop assigned clients that went offline or lost the override (and pick a
      substitute from the pool so the count stays at N);
    * pick more from the pool when under N; release extras when over N.
  Quotas that left the effective set release all their ledger clients.

The pool = online clients running their bucket default (no manual sim override)
that aren't already ledger-assigned — "free runners". A client a human manually
pinned (a per-client override the engine didn't set) is NOT touched: the ledger
is the provenance, and the engine only ever toggles the specific ``sim_id`` flag
(+ ``wsite``) it owns on a ledger client, never another field.

Self-heal is driven by a periodic reconcile loop (``run``) PLUS an immediate
reconcile on every effective-quota push (``_trigger_sim_quota_reconcile``), so a
runner dying is picked up on the next sweep (default every 60s) — faster WS-
disconnect drop detection is a later chunk.

Re-home (Chunk 3 refines): assigning a client to a site-specific quota sets
``wsite``; releasing it reverts ``wsite`` to the bucket default site. The ledger
records the client's original site so a return-and-revert path can restore it.

multi_capable (Chunk 4): failure sims are exclusive (one failure sim per
client), traffic sims are multi-capable and may PACK onto a client already
running other sims. Pool eligibility is quota-aware: an exclusive quota only
takes a client not already running an exclusive sim; a multi-capable quota may
stack onto an engine-owned client (and onto a client running an exclusive sim).
A human manual pin on a sim flag the engine didn't set is never touched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SimQuotaEngine")

HARVEST_WINDOW_S = 1800.0        # a client seen within the last 30 min is
                                 # harvestable. Real-ish clients flap (connect/
                                 # disconnect constantly), so a tight "online now"
                                 # window would shrink the pool to a handful; 30
                                 # min keeps a flapping client in the pool.
OFFLINE_TTL_S = 3600.0           # keep an offline runner this long before
                                 # treating it as dead (the sim keeps running on
                                 # the VM through a WS blip; release only a
                                 # truly-gone client so it can rejoin the pool).
RECONCILE_INTERVAL_S = 60.0      # periodic self-heal sweep


def _quota_key(q: Dict[str, Any]) -> str:
    # Mirrors sim_quota.quota_dedup_key: a presence quota (sim_id empty —
    # "Clients Associated", N clients homed to a site, no sim) is keyed by site
    # alone, so it never collides with an alert-driven sim quota's ledger entry.
    if not (q.get("sim_id") or ""):
        return f"presence::{q.get('site', '')}"
    # An untethered sim quota (sim_id set, no alert_id) is keyed by sim+site so
    # two untethered quotas for different sims at a site don't collide.
    if not (q.get("alert_id") or ""):
        return f"sim:{q.get('sim_id', '')}:{q.get('site', '')}"
    return f"{q.get('alert_type', 'alert')}:{q.get('alert_id', '')}:{q.get('site', '')}"


PLACEMENT_PREFIX = "placement:"   # ledger key prefix for SSID placement quotas
# pxmx_site_map value marking a server whose clients are NOT physically site-bound
# (site-based SSID) — they join this tenant's assignable pool and placement gives
# them a site/SSID. A deployment can MIX: some servers → a real site (RF chamber),
# others → TENANT_POOL. An unmapped server is treated as tenant-pool too. Pools
# are always per-tenant (each tenant has its own spoke/registry) — never shared.
TENANT_POOL = "Tenant-Wide Pool"


class SimQuotaEngine:
    """Owns the engine ledger + reconcile loop for one cs spoke."""

    def __init__(self, spoke) -> None:
        self.spoke = spoke
        # Ledger: quota_key -> { "sim_id", "site", "clients": { hostname: from_site } }
        # from_site is the client's pre-assignment site (for Chunk 3 revert);
        # "" / None means the client was already in the target site.
        self._ledger: Dict[str, Dict[str, Any]] = {}
        self._placement_warnings: List[Dict[str, Any]] = []
        self._user_conf = None
        self._ledger_path: Optional[Path] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._reconcile_lock = asyncio.Lock()
        # Per-sweep hosting-server index + pxmx_site_map, refreshed at the top
        # of each reconcile so _effective_site can resolve a client's site via
        # its hosting pxmx server without rebuilding the index per client.
        self._name_to_host: Dict[str, str] = {}
        self._pxmx_site_map: Dict[str, str] = {}
        try:
            data_dir = Path(getattr(spoke, "data_dir", None) or ".")
            data_dir.mkdir(parents=True, exist_ok=True)
            self._ledger_path = data_dir / "sim_quota_ledger.json"
        except Exception:  # noqa: BLE001
            self._ledger_path = None
        self._load_ledger()

    # ── ledger persistence ───────────────────────────────────────────────────
    def _load_ledger(self) -> None:
        if not self._ledger_path or not self._ledger_path.exists():
            return
        try:
            self._ledger = json.loads(self._ledger_path.read_text(encoding="utf-8")) or {}
            if not isinstance(self._ledger, dict):
                self._ledger = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("SimQuotaEngine: ledger load failed: %s", exc)
            self._ledger = {}

    def _save_ledger(self) -> None:
        if not self._ledger_path:
            return
        try:
            self._ledger_path.write_text(
                json.dumps(self._ledger, indent=2, default=str), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SimQuotaEngine: ledger save failed: %s", exc)

    # ── client state helpers ─────────────────────────────────────────────────
    def _registry(self):
        return getattr(self.spoke, "registry", None)

    def _all_clients(self) -> Dict[str, Dict[str, Any]]:
        reg = self._registry()
        return reg.get_all() if reg is not None else {}

    def _is_harvestable(self, c: Dict[str, Any], now: float) -> bool:
        """Eligible for the pool if seen within HARVEST_WINDOW_S. Clients flap in
        and out constantly (like real ones), so "seen recently" — not "connected
        right now" — is what keeps the pool from collapsing to a handful."""
        ls = c.get("last_seen")
        return bool(ls and (now - float(ls)) < HARVEST_WINDOW_S)

    def _refresh_host_index(self) -> None:
        """Snapshot the client→host and host→site indices for this sweep. Both
        are guarded: a spoke with no deploy (tests / pre-agent mode) or no
        pxmx_site_map yields empty maps and _effective_site falls through to the
        bucket-default wsite path."""
        deploy = getattr(self.spoke, "deploy", None)
        try:
            self._name_to_host = deploy.name_to_host() if deploy is not None else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("SimQuotaEngine: name_to_host failed: %s", exc)
            self._name_to_host = {}
        try:
            self._pxmx_site_map = self.spoke.local_store.get_pxmx_site_map() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("SimQuotaEngine: pxmx_site_map load failed: %s", exc)
            self._pxmx_site_map = {}
        # Load user-overrides.conf so the engine can honor a HUMAN per-user sim
        # pin ([username] flag = on/off) — it must never harvest/count against a
        # flag a human explicitly set (serve-time already lets the human win; this
        # keeps the engine's ledger honest so it doesn't over-count a client whose
        # human 'off' will actually keep the sim off).
        self._user_conf = None
        try:
            import sim_config
            cd = getattr(self.spoke.settings, "config_dir", None)
            if cd is not None:
                _, self._user_conf = sim_config.load_configs(cd)
        except Exception as exc:  # noqa: BLE001
            logger.debug("SimQuotaEngine: user_conf load failed: %s", exc)
            self._user_conf = None

    def _human_pinned_sim(self, hostname: str, sim_id: str) -> bool:
        """True when a human explicitly set this sim flag in the client's
        user-overrides.conf ``[username]`` section (on OR off). The engine leaves
        those clients alone for that sim."""
        uc = getattr(self, "_user_conf", None)
        if uc is None or not sim_id:
            return False
        try:
            from sim_config import username_for
            un = username_for(hostname)
            if uc.has_section(un):
                v = str(uc.get(un, sim_id, fallback="")).strip().lower()
                return v in ("on", "off")
        except Exception:  # noqa: BLE001
            return False
        return False

    def _effective_site(self, hostname: str, c: Dict[str, Any]) -> str:
        """The client's effective site =
        override wsite → hosting pxmx server's assigned site (pxmx_site_map) →
        bucket-default wsite → sim_config fallback. An operator-assigned server
        site wins over the bucket default so a site-specific quota ("10 DNS-fail
        in MIA") is filled from clients whose hosting server is in MIA; an
        engine re-home override (wsite) still wins over both.

        WHY this is PXMX-server-based (not bucket-based): the sim systems live
        in RF chambers with dedicated Proxmox nodes per site — a "site" is a
        physical chamber and the pxmx server is its boundary. With site-based
        SSID enabled the SSID appends the site (MIA + PSK → ``MIA-PSK``); with
        it disabled every site uses the same SSID. Linking pxmx servers to a
        site (pxmx_site_map) makes each site its OWN runner pool (per-site
        scale + RF isolation); without it, clients fall back to bucket wsite
        and you get one entire-tenant pool. The pxmx-server step is what lets a
        quota's pool match the chamber boundary instead of the bucket hash."""
        ov = c.get("overrides") or {}
        w = ov.get("wsite")
        if w:
            return str(w)
        return self._site_without_override(hostname, c)

    def _site_without_override(self, hostname: str, c: Dict[str, Any]) -> str:
        """Effective site ignoring any ``wsite`` override (engine OR manual):
        hosting pxmx server's assigned site → bucket-default wsite →
        sim_config fallback. Shared by ``_effective_site`` (after the override
        check) and ``_natural_site`` (always)."""
        # Hosting pxmx server's assigned site (operator-set pxmx_site_map). The
        # host index is refreshed once per reconcile sweep. A TENANT_POOL server
        # is NOT a physical site — its clients fall through to the wsite that
        # placement assigns (or the bucket default) so they can be placed anywhere.
        host = self._name_to_host.get(str(hostname).strip().lower())
        if host:
            s = self._pxmx_site_map.get(host)
            if s and s != TENANT_POOL:
                return str(s)
        cfg = c.get("config") or {}
        w = cfg.get("wsite")
        if w:
            return str(w)
        # Fall back to the bucket-resolved site via sim_config if available.
        try:
            import sim_config
            cd = getattr(self.spoke.settings, "config_dir", None)
            if cd is not None:
                sc, uc = sim_config.load_configs(cd)
                _, eff_cfg = sim_config.effective_client_fields(
                    hostname, sc, uc, c.get("simulation_id") or "", c.get("config"))
                return str(eff_cfg.get("wsite") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _physical_site_of(self, hostname: str) -> str:
        """The hosting pxmx server's REAL assigned site (RF chamber), or "" when
        the server is in the tenant pool or unmapped. Uses the per-sweep index."""
        host = self._name_to_host.get(str(hostname).strip().lower())
        if host:
            s = self._pxmx_site_map.get(host)
            if s and s != TENANT_POOL:
                return str(s)
        return ""

    def _is_tenant_pool_client(self, hostname: str) -> bool:
        """True when the client's hosting server is in the tenant-wide pool (or
        unmapped) — i.e. it is assignable to any site/SSID by placement rather
        than physically pinned to one chamber."""
        host = self._name_to_host.get(str(hostname).strip().lower())
        if not host:
            return True  # unmapped server → assignable (tenant pool)
        s = self._pxmx_site_map.get(host)
        return (not s) or s == TENANT_POOL

    def _site_ok_for(self, hostname: str, site: str) -> bool:
        """A tenant-pool (assignable) client may go to ``site`` only if it hasn't
        already been claimed for a DIFFERENT site this sweep — so it can't end up
        in two sites at once."""
        return getattr(self, "_claimed_site", {}).get(hostname, site) == site

    def _cell_ok_for(self, hostname: str, claim: str) -> bool:
        """A client may be used by a quota keyed on ``claim`` (an SSID cell like
        "MIA-PSK", or a bare site for non-cell quotas) only if it isn't already
        claimed for a DIFFERENT cell this sweep — a client has ONE SSID, so
        MIA-PSK and MIA-ACD can't both hold it. Quotas on the SAME cell share
        (same claim → eligible), which is what lets dns_fail stack onto the cell's
        "Clients Associated" clients."""
        return getattr(self, "_claimed_cell", {}).get(hostname, claim) == claim

    def _natural_site(self, hostname: str, c: Dict[str, Any]) -> str:
        """The client's site with engine re-homes reverted — a MANUAL ``wsite``
        override is honored, but an ENGINE-set ``wsite`` (one that matches the
        site of a ledger entry owning this client) is skipped → pxmx_site_map →
        bucket wsite → sim_config.

        This is the site a release reverts ``wsite`` to, and the ``from_site``
        captured at assign. Recording the NATURAL site (not the already-re-homed
        effective site) is what makes the multi_capable packing × re-home
        reference count work: a second quota that PACKS onto an already-re-homed
        client records the client's natural site, so it's recognized as a fellow
        re-homer and releasing one quota won't revert ``wsite`` out from under
        the other. A manual operator ``wsite`` override is preserved on revert."""
        ov = c.get("overrides") or {}
        ov_wsite = str(ov.get("wsite") or "")
        if ov_wsite:
            owning_sites = {str(e.get("site") or "")
                            for e in self._ledger.values()
                            if hostname in (e.get("clients") or {})}
            if ov_wsite not in owning_sites:
                return ov_wsite  # manual wsite override — honor it
            # engine-set wsite → skip, fall through to pxmx/bucket
        return self._site_without_override(hostname, c)

    def _has_sim_on(self, c: Dict[str, Any], sim_id: str) -> bool:
        ov = c.get("overrides") or {}
        v = str(ov.get(sim_id, "")).strip().lower()
        if v == "on":
            return True
        if v == "off":
            return False
        # No override → bucket default decides.
        prof = c.get("config") or {}
        return str(prof.get(sim_id, "")).strip().lower() == "on"

    def _engine_owned_clients(self) -> set:
        out = set()
        for entry in self._ledger.values():
            for h in (entry.get("clients") or {}).keys():
                out.add(h)
        return out

    def _engine_sims_for(self, hostname: str) -> set:
        """The sim_ids the engine has set ON on this client (across every ledger
        entry it appears in — a multi-capable client can be packed under several
        quotas). Used to tell engine-set overrides from human manual pins."""
        out = set()
        for entry in self._ledger.values():
            if hostname in (entry.get("clients") or {}):
                s = entry.get("sim_id")
                if s:
                    out.add(s)
        return out

    def _sim_multi(self, sim_id: str) -> bool:
        # Authoritative per-sim shareable override (hub-pushed via sim_shareable)
        # WINS over the hardcoded SIM_META default — a sim set non-shareable can
        # never be stacked (Config → Sim Quotas → Simulation Sharing tile).
        try:
            sh = self.spoke.local_store.get_sim_shareable()
            if sim_id in sh:
                return bool(sh[sim_id])
        except Exception:  # noqa: BLE001
            pass
        try:
            from sim_quota import SIM_META
        except Exception:  # noqa: BLE001
            return False
        return bool(SIM_META.get(sim_id, {}).get("multi_capable", False))

    def _client_active_sims(self, c: Dict[str, Any]) -> set:
        """Sims currently ON for this client — override wins, else bucket default.
        ``off`` overrides suppress a bucket-default-on sim."""
        try:
            from sim_quota import SIM_META
            flags = list(SIM_META.keys())
        except Exception:  # noqa: BLE001
            flags = []
        ov = c.get("overrides") or {}
        cfg = c.get("config") or {}
        active = set()
        for sim in flags:
            v = str(ov.get(sim, cfg.get(sim, ""))).strip().lower()
            if v == "on":
                active.add(sim)
        return active

    def _exclusive_running(self, hostname: str, c: Dict[str, Any]) -> set:
        """The exclusive (multi_capable=False) sims currently ON on this client.

        Combines the per-sweep snapshot (bucket defaults + manual pins + engine
        sims from PRIOR sweeps) with the in-memory ledger (engine sims assigned
        EARLIER in THIS sweep — the snapshot was taken before those overrides
        landed, so without the ledger union a second exclusive quota would
        re-pick a client the engine just assigned to the first one)."""
        active = self._client_active_sims(c) | self._engine_sims_for(hostname)
        return {s for s in active if not self._sim_multi(s)}

    def _has_manual_sim_pin(self, hostname: str, c: Dict[str, Any]) -> bool:
        """A human pinned a sim flag the engine didn't set on this client. The
        engine owns only the (sim_id, wsite) it set on ledger clients; any OTHER
        sim-flag override (on/off) is a manual pin we must not fight."""
        eng = self._engine_sims_for(hostname)
        ov = c.get("overrides") or {}
        for k, v in ov.items():
            if k == "wsite" or k in eng:
                continue
            if str(v).strip().lower() in ("on", "off"):
                return True
        return False

    def _pool_eligible(self, hostname: str, c: Dict[str, Any], sim_id: str,
                       multi: bool, now: float, assigned: Dict[str, str]) -> bool:
        """Quota-aware pool eligibility for a top-up pick:

        * online, and not already assigned to THIS quota;
        * no human manual pin on a sim flag the engine didn't set (respect
          provenance — never fight a human);
        * an EXCLUSIVE quota (``multi`` False) only takes a client not already
          running an exclusive sim — one failure sim per client;
        * a MULTI-CAPABLE quota (``multi`` True) may PACK onto a client the
          engine already owns under another quota (and onto a client running an
          exclusive sim) — traffic sims stack.
        """
        if hostname in assigned:
            return False
        if not self._is_harvestable(c, now):
            return False
        if self._has_manual_sim_pin(hostname, c):
            return False
        # Never harvest a client whose HUMAN user-override pins THIS sim — the
        # served config would honor the human, so counting it here would lie.
        if self._human_pinned_sim(hostname, sim_id):
            return False
        if multi:
            return True
        return not self._exclusive_running(hostname, c)

    # ── assign / release ─────────────────────────────────────────────────────
    async def _assign(self, hostname: str, sim_id: str, site: str,
                      cell: Optional[Dict[str, Any]] = None) -> None:
        reg = self._registry()
        if reg is None:
            return
        c = reg.get(hostname) or {}
        # Record the NATURAL site (engine re-homes reverted, manual wsite kept)
        # so a packed second quota is recognized as a fellow re-homer — see
        # _natural_site. The engine re-homes when the quota site differs from it.
        from_site = self._natural_site(hostname, c)
        overrides: Dict[str, Any] = {}
        # A sim quota turns its sim flag ON; a PRESENCE quota (sim_id empty —
        # "Clients Associated") sets NO sim flag — it only homes the client
        # (wsite) so other stackable sims may still pack onto it.
        if sim_id:
            overrides[sim_id] = "on"
        if site and site != from_site:
            overrides["wsite"] = site
        # Cell quota: also pin the client's SSID (+ password) to the cell, so a
        # "MIA-PSK" quota lands the client on the PSK SSID — not just the site.
        if cell:
            for src, key in (("ssid", "ssid"), ("ssidpw", "ssidpw")):
                val = str(cell.get(src) or "").strip()
                if val:
                    overrides[key] = val
        if overrides:
            await self._engine_set(hostname, overrides)
        logger.info("SimQuotaEngine: assigned %s → sim=%s site=%s (from %s)",
                    hostname, sim_id or "(presence)", site or "*", from_site or "*")

    async def _release(self, hostname: str, sim_id: str, from_site: str,
                       quota_key: Optional[str] = None) -> None:
        reg = self._registry()
        if reg is None:
            return
        # Revert the sim flag by DELETION so the client returns to its bucket
        # default (ON or OFF) — setting "off" would linger as a real override
        # when the bucket default is ON, keeping a released sim forced off
        # instead of reverting (the old "reverts to bucket default via registry
        # prune" only pruned when the bucket was off). _engine_remove is a no-op
        # when the engine doesn't own the key (e.g. a human reclaimed the sim
        # mid-assignment). A PRESENCE quota (sim_id empty) sets NO sim flag.
        if sim_id:
            await self._engine_remove(hostname, [sim_id])
        # Restore the pre-assignment site UNLESS another ledger entry still
        # re-homes this client (multi_capable packing can stack re-homing
        # quotas on one client), in which case keep wsite at that other quota's
        # target so releasing one doesn't undo the other's re-home. A manual
        # operator wsite pin is preserved (revert target == the from_site
        # _natural_site captured at assign, which honors a manual wsite).
        c = reg.get(hostname) or {}
        cur_site = self._effective_site(hostname, c)
        target = (self._remaining_rehome_target(hostname, quota_key)
                  if quota_key else None)
        if target:
            if cur_site != target:
                await self._engine_set(hostname, {"wsite": target})
        elif from_site and cur_site != from_site:
            await self._engine_set(hostname, {"wsite": from_site})
        logger.info("SimQuotaEngine: released %s ← sim=%s (site %s)",
                    hostname, sim_id or "(presence)", target or from_site or "*")

    async def _engine_set(self, hostname: str, overrides: Dict[str, Any]) -> None:
        """Set engine-owned overrides, recording provenance in ``engine_keys``
        so the reconcile tail can later remove ONLY keys the engine set (never
        a human manual pin). Falls back to plain ``set_overrides`` for
        fake/test registries that don't track provenance (behavior identical
        minus the engine_keys bookkeeping)."""
        reg = self._registry()
        if reg is None:
            return
        if hasattr(reg, "set_engine_overrides"):
            await reg.set_engine_overrides(hostname, overrides)
        else:
            await reg.set_overrides(hostname, overrides)

    async def _engine_remove(self, hostname: str, keys: List[str]) -> None:
        """Revert engine-owned keys by DELETION (return to bucket default).
        Falls back to setting each key ``"off"`` for fakes without
        ``remove_engine_keys`` — matches the pre-provenance release behavior
        (and the fake's set_overrides doesn't prune, so the "off" lingers in
        the fake exactly as it did before)."""
        reg = self._registry()
        if reg is None:
            return
        if hasattr(reg, "remove_engine_keys"):
            await reg.remove_engine_keys(hostname, keys)
        else:
            await reg.set_overrides(hostname, {k: "off" for k in keys})

    def _remaining_rehome_target(self, hostname: str,
                                 excluding_key: Optional[str]) -> Optional[str]:
        """Target site another ledger entry still re-homes ``hostname`` to.

        A ledger entry re-homed ``hostname`` when its ``site`` differs from the
        ``from_site`` it recorded for that client. While any other entry still
        re-homes the client, releasing this quota must keep ``wsite`` at that
        target instead of reverting to ``from_site`` (which would undo the other
        quota's re-home — the multi_capable packing × re-home edge case). Returns
        ``None`` when no other quota re-homes the client.
        """
        for key, entry in self._ledger.items():
            if key == excluding_key:
                continue
            clients = entry.get("clients") or {}
            if hostname not in clients:
                continue
            esite = entry.get("site") or ""
            fsite = clients.get(hostname) or ""
            if esite and esite != fsite:
                return esite
        return None

    # ── SSID placement (design doc §5) ───────────────────────────────────────
    async def _place(self, hostname: str, cell: Dict[str, Any]) -> None:
        """Move a client onto an SSID cell by setting its connectivity overrides
        (wsite/ssid/ssidpw). Delivered via the served [username] layer, so it
        wins over the client's bucket. Non-toggle overrides survive the registry
        prune (unlike on/off sim flags)."""
        reg = self._registry()
        if reg is None:
            return
        overrides: Dict[str, Any] = {}
        for src, key in (("site", "wsite"), ("ssid", "ssid"), ("ssidpw", "ssidpw")):
            val = str(cell.get(src) or "").strip()
            if val:
                overrides[key] = val
        if overrides:
            await reg.set_overrides(hostname, overrides)

    def _cell_of(self, hostname: str, c: Dict[str, Any], site: str,
                 cells: Dict[str, Dict[str, Any]]) -> str:
        """Which cell (by name) this client currently sits on at ``site`` — matched
        by its effective ssid (override → bucket default)."""
        ov = c.get("overrides") or {}
        ssid = str(ov.get("ssid") or (c.get("config") or {}).get("ssid") or "").strip()
        for name, cd in cells.items():
            if str(cd.get("site") or "").strip() == site and str(cd.get("ssid") or "").strip() == ssid:
                return name
        return ""

    def _quota_cell(self, q: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """If a quota's ``site`` names an SSID cell (Pool & SSID matrix), return
        that cell dict; else None. A cell quota scopes to the cell's physical
        site and pins the assigned client's ssid/ssidpw to the cell."""
        return getattr(self, "_cells_by_name", {}).get(str(q.get("site") or "").strip())

    async def _reconcile_weighted(self, clients: Dict[str, Any], now: float,
                                  actions: Dict[str, int]) -> None:
        """Spread the SPARE pool (clients no harvest quota claimed this sweep)
        across each site's weighted SSID cells — the ~99% random clients, with NO
        per-client accounting (no ledger). Each weighted rule is {site, ssid,
        weight, all}: clients split proportional to weight; weight 0 = that cell
        takes none; a cell flagged ``all`` soaks the balance after the split.
        Runs AFTER the harvest loop so it only touches clients not pinned to a
        quota. Stateless: it just sets each spare client's ssid/wsite each sweep."""
        self._placement_warnings = []
        try:
            rules = self.spoke.local_store.get_ssid_weights() or []
        except Exception:  # noqa: BLE001
            return
        if not rules:
            return
        cells = getattr(self, "_cells_by_name", {})
        online = {h for h, c in clients.items() if self._is_harvestable(c, now)}
        # Group rules by the cell's physical site.
        by_site: Dict[str, List[Dict[str, Any]]] = {}
        for r in rules:
            cname = str(r.get("ssid") or r.get("cell") or "").strip()
            cell = cells.get(cname)
            if not cell:
                continue
            site = str(cell.get("site") or r.get("site") or "").strip()
            if not site:
                continue
            by_site.setdefault(site, []).append({
                "cell": cell, "name": cname,
                "weight": max(0.0, float(r.get("weight") or 0)),
                "all": bool(r.get("all")),
            })

        for site, srules in by_site.items():
            # Spare = online clients physically here (RF chamber) OR assignable
            # (tenant-pool), that no harvest quota already claimed this sweep.
            spare = [h for h in online
                     if h not in self._claimed_site
                     and (self._physical_site_of(h) == site
                          or (self._is_tenant_pool_client(h) and self._site_ok_for(h, site)))]
            if not spare:
                continue
            weighted = [r for r in srules if r["weight"] > 0]
            all_rule = next((r for r in srules if r["all"]), None)
            total_w = sum(r["weight"] for r in weighted)
            n = len(spare)
            alloc: Dict[str, int] = {r["name"]: 0 for r in srules}
            if total_w > 0:
                raw = [(r, n * r["weight"] / total_w) for r in weighted]
                for r, x in raw:
                    alloc[r["name"]] = int(x)
                rem = n - sum(alloc.values())
                if all_rule is not None:            # balance → the `all` cell
                    alloc[all_rule["name"]] += rem
                else:                                # largest-remainder among weighted
                    for r, _x in sorted(raw, key=lambda t: t[1] - int(t[1]),
                                        reverse=True)[:max(0, rem)]:
                        alloc[r["name"]] += 1
            elif all_rule is not None:               # no weights set → everyone to `all`
                alloc[all_rule["name"]] = n
            # Hand out the spare clients per allocation (order is arbitrary — these
            # are random clients; no stickiness needed).
            i = 0
            for r in srules:
                take = alloc.get(r["name"], 0)
                for h in spare[i:i + take]:
                    await self._place(h, r["cell"])
                    self._claimed_site[h] = site
                    actions["assigned"] += 1
                i += take

    # ── reconcile ────────────────────────────────────────────────────────────
    async def reconcile(self) -> Dict[str, Any]:
        """One sweep: align the ledger + overrides with effective_sim_quotas."""
        async with self._reconcile_lock:
            quotas = self.spoke.local_store.get_effective_sim_quotas() or []
            now = time.time()
            clients = self._all_clients()
            eff_keys = {_quota_key(q) for q in quotas}
            self._refresh_host_index()
            # SSID-cell index for this sweep. A quota whose `site` names a cell
            # (e.g. "MIA-PSK") scopes to the cell's physical SITE and pins each
            # assigned client's ssid/ssidpw to the cell — a self-contained cell
            # quota. The weighted spread (_reconcile_weighted) reuses this index.
            self._cells_by_name = {}
            try:
                for cd in (self.spoke.local_store.get_ssid_matrix() or []):
                    nm = str(cd.get("name") or cd.get("ssid_name") or "").strip()
                    if nm and cd.get("enabled", True):
                        self._cells_by_name[nm] = cd
            except Exception:  # noqa: BLE001
                self._cells_by_name = {}

            actions = {"assigned": 0, "released": 0, "kept": 0}
            # Exclusivity: a tenant-pool client belongs to exactly ONE physical
            # SITE per sweep (no MIA+DFW), and to exactly ONE SSID CELL (no
            # MIA-PSK+MIA-ACD). Seed both from the current ledger (first entry
            # wins) so an already-homed client stays put; the sim quotas below
            # claim into these maps. _claimed_cell keys sharing too: quotas on the
            # SAME cell reuse each other's clients (dns_fail stacks onto the cell's
            # Clients Associated), different cells don't.
            self._claimed_site: Dict[str, str] = {}
            self._claimed_cell: Dict[str, str] = {}
            for _e in self._ledger.values():
                _s = str(_e.get("site") or "")
                _ck = str(_e.get("claim") or _s)
                for _h in (_e.get("clients") or {}):
                    if _s:
                        self._claimed_site.setdefault(_h, _s)
                    if _ck:
                        self._claimed_cell.setdefault(_h, _ck)
            # Harvest quotas run FIRST and claim their exact (accounted) clients;
            # the weighted random spread (below, after the loop) then places every
            # remaining spare client — so only the ~1% harvested is accounted for.
            # Process PRESENCE quotas (sim_id empty — "Clients Associated") first
            # so clients are homed to their site before the sim quotas that stack
            # onto them run. get_all() is a per-sweep SNAPSHOT COPY, so a same-
            # sweep re-home isn't visible to a later sim quota via _effective_site
            # — the sim quotas instead consult the ledger (homed_here) below.
            quotas = sorted(quotas, key=lambda q: 0 if not (q.get("sim_id") or "") else 1)
            for q in quotas:
                key = _quota_key(q)
                sim_id = q.get("sim_id") or ""
                site = q.get("site") or ""
                # If `site` names an SSID cell (e.g. "MIA-PSK"), scope to the
                # cell's PHYSICAL site for all in-site/pool/claim tests, and pin
                # that cell's SSID on the clients we assign. The ledger KEY still
                # carries the cell name (via _quota_key), so MIA-PSK and MIA-ACD
                # are distinct quotas that can coexist at the same site.
                cell = self._quota_cell(q)
                scope_site = str(cell.get("site") or "") if cell else site
                # claim = the SHARING + EXCLUSIVITY identity: the SSID cell name
                # for a cell quota, else the (physical) site. Quotas with the same
                # claim share clients; different claims can't hold the same client.
                claim_key = str(cell.get("name") or scope_site) if cell else scope_site
                target = int(q.get("count") or 1)
                # A PRESENCE quota (sim_id empty — "Clients Associated") homes N
                # clients to the site and runs no sim; it's still a real ledger
                # entry the engine keeps filled (substitute on offline/dead).
                entry = self._ledger.setdefault(
                    key, {"sim_id": sim_id, "site": scope_site, "clients": {}})
                entry["sim_id"] = sim_id
                entry["site"] = scope_site
                entry["claim"] = claim_key
                # Alias the stored dict (NOT `or {}` — an empty dict is falsy,
                # so `or {}` would rebind to a fresh dict and mutations wouldn't
                # land in the ledger).
                assigned = entry.setdefault("clients", {})

                # Walk the ledger: keep eligible clients, drop ineligible ones.
                # An offline client is NOT released outright — the sim keeps
                # running on the VM through a WS blip, so we keep it in the
                # ledger and let a substitute fill the online gap; when it
                # returns (online-assigned > N) the over-N trim releases one.
                # Only a client offline past OFFLINE_TTL_S is treated as dead
                # and released so it can rejoin the pool. ``producing`` is the
                # online+in-site+sim-on subset that counts toward N.
                producing: List[str] = []
                for h, from_site in list(assigned.items()):
                    c = clients.get(h)
                    if c is None:
                        # Vanished from the registry — drop the dead entry.
                        assigned.pop(h, None)
                        continue
                    if sim_id and not self._has_sim_on(c, sim_id):
                        # Lost the sim override (human cleared it) — drop from
                        # ledger, don't fight the human. A PRESENCE quota has no
                        # sim flag to lose; it stays until it drifts off-site or
                        # goes dead (handled below).
                        assigned.pop(h, None)
                        continue
                    if scope_site and self._effective_site(h, c) != scope_site:
                        # Drifted off-site — release, let a substitute pick up.
                        await self._release(h, sim_id, from_site, key)
                        assigned.pop(h, None)
                        actions["released"] += 1
                        continue
                    if not self._is_harvestable(c, now):
                        # Offline: keep (sim still runs on the VM) unless it's
                        # been gone long enough to be considered dead.
                        last_seen = float(c.get("last_seen") or 0)
                        if (now - last_seen) > OFFLINE_TTL_S:
                            await self._release(h, sim_id, from_site, key)
                            assigned.pop(h, None)
                            actions["released"] += 1
                        continue
                    producing.append(h)
                    actions["kept"] += 1

                # Top up producing to N from the free-runner pool. First try
                # in-site free runners (clients whose hosting server / wsite is
                # already the quota site — respects physical placement). If the
                # quota opts into re-home (``rehome``) and the in-site pool is
                # exhausted, borrow free runners from OTHER sites and set
                # ``wsite`` to re-home them; the ledger records their NATURAL
                # site (engine re-homes reverted, manual wsite kept) as
                # ``from_site`` so a later release reverts it AND a packed fellow
                # quota is recognized as a re-homer (no cross-quota wsite
                # stomp).
                if len(producing) < target:
                    need = target - len(producing)
                    multi = bool(q.get("multi_capable"))
                    # Clients the engine has ALREADY homed to this site under any
                    # other quota (typically a presence quota — "Clients
                    # Associated") count as in-site here, so a sim quota's tests
                    # stack onto presence-homed clients immediately instead of
                    # waiting for the next sweep's snapshot to reflect the wsite
                    # re-home (get_all() is a stale per-sweep copy).
                    # Share by CELL, not site: only clients already homed to THIS
                    # cell (same claim) count as homed_here — so dns_fail on MIA-PSK
                    # stacks onto MIA-PSK's Clients Associated, but NOT onto MIA-ACD.
                    homed_here = set()
                    if claim_key:
                        for e in self._ledger.values():
                            if (e.get("claim") or e.get("site") or "") == claim_key:
                                homed_here.update((e.get("clients") or {}).keys())
                    # A tenant-pool client (server not site-pinned) is assignable
                    # to any site, so it counts as in-site for a site-scoped
                    # harvest (assign sets its wsite=site). Physically-bound
                    # clients at OTHER sites are still excluded (RF isolation).
                    # _cell_ok_for excludes a client already claimed for a DIFFERENT
                    # cell this sweep (one SSID per client).
                    in_site = [h for h, c in clients.items()
                               if self._pool_eligible(h, c, sim_id, multi, now, assigned)
                               and self._cell_ok_for(h, claim_key)
                               and (not scope_site or self._effective_site(h, c) == scope_site
                                    or h in homed_here
                                    or (self._is_tenant_pool_client(h) and self._site_ok_for(h, scope_site)))]
                    picks = in_site[:need]
                    if q.get("rehome") and len(picks) < need:
                        # Cross-site fallback: any other-site eligible runner.
                        # _assign sets wsite=site when the client's natural
                        # site differs, re-homing it; assigned[h] captures the
                        # NATURAL site (the snapshot ``c`` predates the override
                        # and _natural_site skips any engine-set wsite).
                        # Only TENANT-POOL clients can be re-homed across sites (a
                        # physically-bound client can't leave its chamber), and only
                        # if not already claimed for another site this sweep.
                        cross = [h for h, c in clients.items()
                                 if self._pool_eligible(h, c, sim_id, multi, now, assigned)
                                 and self._is_tenant_pool_client(h)
                                 and self._cell_ok_for(h, claim_key)
                                 and self._site_ok_for(h, scope_site)
                                 and (not scope_site or self._effective_site(h, c) != scope_site)]
                        picks += cross[:need - len(picks)]
                    for h in picks:
                        await self._assign(h, sim_id, scope_site, cell=cell)
                        c = clients.get(h) or {}
                        # Record the natural site (pre-rehome, manual wsite kept)
                        # so a packed fellow quota is recognized as a re-homer.
                        assigned[h] = self._natural_site(h, c)
                        if scope_site:
                            self._claimed_site[h] = scope_site   # one site per client per sweep
                        if claim_key:
                            self._claimed_cell[h] = claim_key    # one SSID cell per client per sweep
                        producing.append(h)
                        actions["assigned"] += 1

                # Trim producing extras when over N (release the most recently
                # added — the substitute, not a returning original that's earlier
                # in the ledger). Offline-but-kept clients are NOT trimmed here
                # (they're not in ``producing``); they simply don't count toward
                # N until they come back.
                if len(producing) > target:
                    extras = producing[target:]
                    for h in extras:
                        from_site = assigned.pop(h)
                        await self._release(h, sim_id, from_site, key)
                        actions["released"] += 1

            # Release clients whose quota left the effective set. Legacy
            # placement:* ledger entries are DEPRECATED (placement is now the
            # stateless weighted spread) — drop them without a release; the
            # weighted pass re-sets each spare client's ssid this same sweep.
            for key in list(self._ledger.keys()):
                if key.startswith(PLACEMENT_PREFIX):
                    self._ledger.pop(key, None)
                    continue
                if key not in eff_keys:
                    entry = self._ledger.pop(key)
                    sim_id = entry.get("sim_id") or ""
                    for h, from_site in (entry.get("clients") or {}).items():
                        await self._release(h, sim_id, from_site, key)
                        actions["released"] += 1

            # Weighted random spread: place every spare (un-harvested) client
            # across the site's weighted SSID cells. Stateless, no accounting.
            await self._reconcile_weighted(clients, now, actions)

            # Reconcile engine-owned override keys against the ledger: drop
            # orphaned engine-set sim flags (a transient _release failure left
            # sim_id=on after the ledger entry was dropped) and re-prune every
            # client's on/off overrides against the CURRENT bucket default
            # (set_overrides prunes at write time; this catches flags that
            # became no-ops when the bucket config changed). Both are pure
            # hygiene — the engine removes only keys it set (provenance via
            # engine_keys) and only on/off flags matching the bucket, so a
            # human manual pin is never touched and served config is unchanged.
            await self._reconcile_engine_keys(clients)
            await self._reconcile_prune_defaults(clients)

            self._save_ledger()
            if any(actions.values()):
                logger.info("SimQuotaEngine reconcile: %s", actions)
            return actions

    # ── override hygiene (provenance + bucket re-prune) ──────────────────────
    async def _reconcile_engine_keys(self, clients: Dict[str, Any]) -> None:
        """Remove engine-set sim_id overrides the ledger no longer claims.

        Closes the missed-_release leak: if ``_release`` silently failed (a
        transient registry error) the ledger entry was dropped but the
        ``sim_id=on`` override it set lingered, so the client kept running a
        sim no quota was paying for. The engine removes ONLY sim_id keys it
        recorded in ``engine_keys`` that are STILL "on" AND no longer claimed
        by any ledger entry: a human manual pin is never in ``engine_keys``,
        and a human "off" (which made the quota loop drop the ledger entry at
        ``not _has_sim_on``) is left untouched — reverting it is the re-prune
        pass's job, and only when it matches the bucket. ``wsite`` is
        reconciled by ``_release``'s from_site revert, not here (a stale
        ``engine_keys`` wsite entry with no override is a no-op)."""
        reg = self._registry()
        if reg is None or not hasattr(reg, "remove_engine_keys"):
            return
        for hostname, c in clients.items():
            eng = list((c.get("engine_keys") or []))
            if not eng:
                continue
            claimed = self._engine_sims_for(hostname)
            ov = c.get("overrides") or {}
            orphans = [k for k in eng
                       if k != "wsite" and k not in claimed
                       and str(ov.get(k, "")).strip().lower() == "on"]
            if orphans:
                await reg.remove_engine_keys(hostname, orphans)
                logger.debug("SimQuotaEngine: removed orphan engine keys %s for %s",
                             orphans, hostname)

    async def _reconcile_prune_defaults(self, clients: Dict[str, Any]) -> None:
        """Re-prune every client's on/off overrides against the CURRENT bucket
        default. ``set_overrides`` prunes at write time; this catches flags
        that became no-ops because the bucket default changed LATER (a flag
        pinned "off" when the bucket was on is redundant once the bucket
        flips off, so the prune drops it and the override object stays a true
        diff over the bucket). Only removes overrides matching the bucket —
        served config is unchanged. Skips clients with no on/off flags."""
        reg = self._registry()
        if reg is None or not hasattr(reg, "prune_against_bucket"):
            return
        for hostname, c in clients.items():
            ov = c.get("overrides") or {}
            if not ov:
                continue
            if not any(str(v).strip().lower() in ("on", "off") for v in ov.values()):
                continue
            try:
                await reg.prune_against_bucket(hostname)
            except Exception as exc:  # noqa: BLE001 — hygiene pass must not kill the sweep
                logger.debug("SimQuotaEngine: re-prune failed for %s: %s",
                             hostname, exc)

    # ── loop ─────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._loop(), name="sim-quota-engine")

    def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a sweep must not kill the loop
                logger.warning("SimQuotaEngine sweep failed: %s", exc)
            await asyncio.sleep(RECONCILE_INTERVAL_S)

    def trigger(self) -> None:
        """Immediate best-effort reconcile (on effective-quota push)."""
        try:
            asyncio.create_task(self.reconcile(), name="sim-quota-reconcile-now")
        except RuntimeError:
            pass  # no running loop yet — the periodic loop will catch it

    # ── introspection (for the Chunk 4 quota-state view) ─────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            key: {"sim_id": e.get("sim_id"), "site": e.get("site"),
                  "clients": list((e.get("clients") or {}).keys())}
            for key, e in self._ledger.items()
        }

    def placement_warnings(self) -> List[Dict[str, Any]]:
        """SSID cells that couldn't reach their hold-N floor last sweep (pool too
        small). Consumed by the Quota State view."""
        return list(getattr(self, "_placement_warnings", []) or [])

    def pool_counts(self) -> Dict[str, Any]:
        """A CHEAP count of the current harvestable pool — one O(n) pass over the
        client registry, NO ledger/accounting (this is the ~99% we deliberately
        don't track per-client). Returns total online, per-physical-site counts,
        and the tenant-pool (assignable-anywhere) count."""
        now = time.time()
        self._refresh_host_index()
        out = {"online": 0, "by_site": {}, "tenant_pool": 0}
        for h, c in self._all_clients().items():
            if not self._is_harvestable(c, now):
                continue
            out["online"] += 1
            if self._is_tenant_pool_client(h):
                out["tenant_pool"] += 1
            else:
                s = self._physical_site_of(h)
                if s:
                    out["by_site"][s] = out["by_site"].get(s, 0) + 1
        return out