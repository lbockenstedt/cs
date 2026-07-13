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

ONLINE_WINDOW_S = 300.0          # mirrors control_plane.py's online threshold
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


class SimQuotaEngine:
    """Owns the engine ledger + reconcile loop for one cs spoke."""

    def __init__(self, spoke) -> None:
        self.spoke = spoke
        # Ledger: quota_key -> { "sim_id", "site", "clients": { hostname: from_site } }
        # from_site is the client's pre-assignment site (for Chunk 3 revert);
        # "" / None means the client was already in the target site.
        self._ledger: Dict[str, Dict[str, Any]] = {}
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

    def _is_online(self, c: Dict[str, Any], now: float) -> bool:
        ls = c.get("last_seen")
        return bool(ls and (now - float(ls)) < ONLINE_WINDOW_S)

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
        # host index is refreshed once per reconcile sweep.
        host = self._name_to_host.get(str(hostname).strip().lower())
        if host:
            s = self._pxmx_site_map.get(host)
            if s:
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
        if not self._is_online(c, now):
            return False
        if self._has_manual_sim_pin(hostname, c):
            return False
        if multi:
            return True
        return not self._exclusive_running(hostname, c)

    # ── assign / release ─────────────────────────────────────────────────────
    async def _assign(self, hostname: str, sim_id: str, site: str) -> None:
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
        if overrides:
            await reg.set_overrides(hostname, overrides)
        logger.info("SimQuotaEngine: assigned %s → sim=%s site=%s (from %s)",
                    hostname, sim_id or "(presence)", site or "*", from_site or "*")

    async def _release(self, hostname: str, sim_id: str, from_site: str,
                       quota_key: Optional[str] = None) -> None:
        reg = self._registry()
        if reg is None:
            return
        # Turn the sim off (reverts to bucket default via registry prune) and
        # restore the pre-assignment site if the engine re-homed it — UNLESS
        # another ledger entry still re-homes this client (multi_capable
        # packing can stack re-homing quotas on one client), in which case keep
        # wsite at that other quota's target so releasing one doesn't undo the
        # other's re-home. A PRESENCE quota (sim_id empty) sets NO sim flag —
        # only the wsite revert applies.
        overrides: Dict[str, Any] = {}
        if sim_id:
            overrides[sim_id] = "off"
        c = reg.get(hostname) or {}
        cur_site = self._effective_site(hostname, c)
        target = (self._remaining_rehome_target(hostname, quota_key)
                  if quota_key else None)
        if target:
            if cur_site != target:
                overrides["wsite"] = target
        elif from_site and cur_site != from_site:
            overrides["wsite"] = from_site
        if overrides:
            await reg.set_overrides(hostname, overrides)
        logger.info("SimQuotaEngine: released %s ← sim=%s (site %s)",
                    hostname, sim_id or "(presence)", target or from_site or "*")

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

    async def _reconcile_placement(self, clients: Dict[str, Any], now: float,
                                   actions: Dict[str, int]) -> None:
        """Hold N clients on each configured SSID cell within a site (sticky,
        self-healing). Backfill is sourced from the live pool of clients already
        physically in the site — no separate per-client tracking of the balance.
        A ``remainder`` cell soaks up everyone not held by a target."""
        try:
            placement = self.spoke.local_store.get_ssid_placement() or {}
            matrix = self.spoke.local_store.get_ssid_matrix() or []
        except Exception:  # noqa: BLE001
            return
        if not placement:
            return
        cells: Dict[str, Dict[str, Any]] = {}
        for cd in matrix:
            name = str(cd.get("name") or cd.get("ssid_name") or "").strip()
            if name and cd.get("enabled", True):
                cells[name] = cd

        for site, cfg in placement.items():
            if not isinstance(cfg, dict):
                continue
            targets = cfg.get("targets") or {}
            remainder = str(cfg.get("remainder") or "").strip()
            # Clients physically in this site, online = the site's pool.
            pool = [h for h, c in clients.items()
                    if self._is_online(c, now) and self._effective_site(h, c) == site]
            where = {h: self._cell_of(h, clients[h], site, cells) for h in pool}
            held: set = set()  # clients a target cell already owns this pass

            for cell_name, want in targets.items():
                want = int(want or 0)
                cell = cells.get(cell_name)
                if not cell:
                    continue
                key = f"{PLACEMENT_PREFIX}{site}:{cell_name}"
                entry = self._ledger.setdefault(key, {"cell": cell_name, "site": site, "clients": {}})
                assigned = entry.setdefault("clients", {})
                # Keep only clients still in the pool and still on this cell.
                for h in list(assigned.keys()):
                    if where.get(h) == cell_name:
                        held.add(h)
                    else:
                        assigned.pop(h, None)
                have = [h for h in assigned if where.get(h) == cell_name]
                if len(have) < want:
                    need = want - len(have)
                    # Backfill from pooled clients not already on/holding a target.
                    candidates = [h for h in pool if where.get(h) != cell_name and h not in held]
                    for h in candidates[:need]:
                        await self._place(h, cell)
                        assigned[h] = where.get(h, "")
                        where[h] = cell_name
                        held.add(h)
                        actions["assigned"] += 1

            # Remainder: everyone in the pool not held by a target lands on the
            # remainder cell (stable). No tracking — it's the site's default cell.
            rem_cell = cells.get(remainder)
            if rem_cell:
                for h in pool:
                    if h not in held and where.get(h) != remainder:
                        await self._place(h, rem_cell)
                        where[h] = remainder
                        actions["assigned"] += 1

    # ── reconcile ────────────────────────────────────────────────────────────
    async def reconcile(self) -> Dict[str, Any]:
        """One sweep: align the ledger + overrides with effective_sim_quotas."""
        async with self._reconcile_lock:
            quotas = self.spoke.local_store.get_effective_sim_quotas() or []
            now = time.time()
            clients = self._all_clients()
            eff_keys = {_quota_key(q) for q in quotas}
            self._refresh_host_index()

            actions = {"assigned": 0, "released": 0, "kept": 0}
            # SSID placement first: settle each client's site/SSID before the sim
            # harvest (which sits on top of that placement).
            await self._reconcile_placement(clients, now, actions)
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
                target = int(q.get("count") or 1)
                # A PRESENCE quota (sim_id empty — "Clients Associated") homes N
                # clients to the site and runs no sim; it's still a real ledger
                # entry the engine keeps filled (substitute on offline/dead).
                entry = self._ledger.setdefault(
                    key, {"sim_id": sim_id, "site": site, "clients": {}})
                entry["sim_id"] = sim_id
                entry["site"] = site
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
                    if site and self._effective_site(h, c) != site:
                        # Drifted off-site — release, let a substitute pick up.
                        await self._release(h, sim_id, from_site, key)
                        assigned.pop(h, None)
                        actions["released"] += 1
                        continue
                    if not self._is_online(c, now):
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
                    homed_here = set()
                    if site:
                        for e in self._ledger.values():
                            if (e.get("site") or "") == site:
                                homed_here.update((e.get("clients") or {}).keys())
                    in_site = [h for h, c in clients.items()
                               if self._pool_eligible(h, c, sim_id, multi, now, assigned)
                               and (not site or self._effective_site(h, c) == site or h in homed_here)]
                    picks = in_site[:need]
                    if q.get("rehome") and len(picks) < need:
                        # Cross-site fallback: any other-site eligible runner.
                        # _assign sets wsite=site when the client's natural
                        # site differs, re-homing it; assigned[h] captures the
                        # NATURAL site (the snapshot ``c`` predates the override
                        # and _natural_site skips any engine-set wsite).
                        cross = [h for h, c in clients.items()
                                 if self._pool_eligible(h, c, sim_id, multi, now, assigned)
                                 and (not site or self._effective_site(h, c) != site)]
                        picks += cross[:need - len(picks)]
                    for h in picks:
                        await self._assign(h, sim_id, site)
                        c = clients.get(h) or {}
                        # Record the natural site (pre-rehome, manual wsite kept)
                        # so a packed fellow quota is recognized as a re-homer.
                        assigned[h] = self._natural_site(h, c)
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

            # Release clients whose quota left the effective set. Placement
            # entries (SSID cells) are managed by _reconcile_placement, not the
            # sim-quota effective set — never release them here.
            for key in list(self._ledger.keys()):
                if key.startswith(PLACEMENT_PREFIX):
                    continue
                if key not in eff_keys:
                    entry = self._ledger.pop(key)
                    sim_id = entry.get("sim_id") or ""
                    for h, from_site in (entry.get("clients") or {}).items():
                        await self._release(h, sim_id, from_site, key)
                        actions["released"] += 1

            self._save_ledger()
            if any(actions.values()):
                logger.info("SimQuotaEngine reconcile: %s", actions)
            return actions

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