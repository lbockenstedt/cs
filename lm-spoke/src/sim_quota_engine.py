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

multi_capable (Chunk 4 refines): failure sims are exclusive (one failure sim per
client), traffic sims are multi-capable. Chunk 2 sets the flag + wsite only;
exclusivity enforcement lands later.
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
    return f"{q.get('alert_type','alert')}:{q.get('alert_id','')}:{q.get('site','')}"


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
        engine re-home override (wsite) still wins over both."""
        ov = c.get("overrides") or {}
        w = ov.get("wsite")
        if w:
            return str(w)
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

    def _is_free_runner(self, hostname: str, c: Dict[str, Any]) -> bool:
        """Online client with NO manual sim-flag override — eligible for the
        pool. ``wsite`` alone doesn't disqualify (the engine may re-home)."""
        ov = c.get("overrides") or {}
        # A sim-flag override the engine didn't set means a human pinned it.
        # The engine owns only the (sim_id, wsite) it set on ledger clients; a
        # non-ledger client with any sim flag override is manually pinned.
        if hostname in self._engine_owned_clients():
            return False
        for k, v in ov.items():
            if k == "wsite":
                continue
            if str(v).strip().lower() in ("on", "off"):
                return False
        return True

    def _engine_owned_clients(self) -> set:
        out = set()
        for entry in self._ledger.values():
            for h in (entry.get("clients") or {}).keys():
                out.add(h)
        return out

    # ── assign / release ─────────────────────────────────────────────────────
    async def _assign(self, hostname: str, sim_id: str, site: str) -> None:
        reg = self._registry()
        if reg is None:
            return
        c = reg.get(hostname) or {}
        from_site = self._effective_site(hostname, c)
        overrides: Dict[str, Any] = {sim_id: "on"}
        if site and site != from_site:
            overrides["wsite"] = site
        await reg.set_overrides(hostname, overrides)
        logger.info("SimQuotaEngine: assigned %s → sim=%s site=%s (from %s)",
                    hostname, sim_id, site or "*", from_site or "*")

    async def _release(self, hostname: str, sim_id: str, from_site: str) -> None:
        reg = self._registry()
        if reg is None:
            return
        # Turn the sim off (reverts to bucket default via registry prune) and
        # restore the pre-assignment site if the engine re-homed it.
        overrides: Dict[str, Any] = {sim_id: "off"}
        c = reg.get(hostname) or {}
        cur_site = self._effective_site(hostname, c)
        if from_site and cur_site != from_site:
            overrides["wsite"] = from_site
        await reg.set_overrides(hostname, overrides)
        logger.info("SimQuotaEngine: released %s ← sim=%s (restored site %s)",
                    hostname, sim_id, from_site or "*")

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
            for q in quotas:
                key = _quota_key(q)
                sim_id = q.get("sim_id") or ""
                site = q.get("site") or ""
                target = int(q.get("count") or 1)
                if not sim_id:
                    continue
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
                    if not self._has_sim_on(c, sim_id):
                        # Lost the override (human cleared it) — drop from
                        # ledger, don't fight the human.
                        assigned.pop(h, None)
                        continue
                    if site and self._effective_site(h, c) != site:
                        # Drifted off-site — release, let a substitute pick up.
                        await self._release(h, sim_id, from_site)
                        assigned.pop(h, None)
                        actions["released"] += 1
                        continue
                    if not self._is_online(c, now):
                        # Offline: keep (sim still runs on the VM) unless it's
                        # been gone long enough to be considered dead.
                        last_seen = float(c.get("last_seen") or 0)
                        if (now - last_seen) > OFFLINE_TTL_S:
                            await self._release(h, sim_id, from_site)
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
                # ``wsite`` to re-home them; the ledger records their original
                # site as ``from_site`` so a later release reverts it.
                if len(producing) < target:
                    owned = self._engine_owned_clients()
                    need = target - len(producing)
                    in_site = [h for h, c in clients.items()
                               if h not in assigned and h not in owned
                               and self._is_online(c, now)
                               and self._is_free_runner(h, c)
                               and (not site or self._effective_site(h, c) == site)]
                    picks = in_site[:need]
                    if q.get("rehome") and len(picks) < need:
                        # Cross-site fallback: any other-site free runner. _assign
                        # sets wsite=site when the client's current effective site
                        # differs, re-homing it; assigned[h] captures the PRE-rehome
                        # effective site (the snapshot ``c`` predates the override).
                        cross = [h for h, c in clients.items()
                                 if h not in assigned and h not in owned
                                 and self._is_online(c, now)
                                 and self._is_free_runner(h, c)
                                 and (not site or self._effective_site(h, c) != site)]
                        picks += cross[:need - len(picks)]
                    for h in picks:
                        await self._assign(h, sim_id, site)
                        c = clients.get(h) or {}
                        assigned[h] = self._effective_site(h, c)
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
                        await self._release(h, sim_id, from_site)
                        actions["released"] += 1

            # Release clients whose quota left the effective set.
            for key in list(self._ledger.keys()):
                if key not in eff_keys:
                    entry = self._ledger.pop(key)
                    sim_id = entry.get("sim_id") or ""
                    for h, from_site in (entry.get("clients") or {}).items():
                        await self._release(h, sim_id, from_site)
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