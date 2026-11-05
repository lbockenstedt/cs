"""ClientRegistry — the cs spoke's per-host client registry (Phase 2).

The spoke's client-facing surface (`client_api.py`) needs to track every
simulation client that has reported in over ``POST /api/status`` or the
``/ws/client`` socket: last-seen, connected SSID, gateway reachability, the
simulations it is running, and per-client config overrides the hub/UI can push
(``/api/clients/{hostname}/control``).

This is the lm-spoke equivalent of the webui-spoke ``clients`` dict +
``CLIENT_HISTORY_FILE``. State is persisted to ``data/clients.json`` (runtime
state — covered by ``.gitignore``, never committed).

All public mutators are async and serialize through a single ``asyncio.Lock``
so the WS receive loop and HTTP handlers can't tear each other's writes.

Persistence is DEBOUNCED: every mutation used to rewrite all of clients.json
(one full O(N) serialize + write per client beacon — with N clients beaconing
every ~15 s that's N full-file rewrites per interval). ``_apersist`` now only
marks the registry dirty and arms a coalescing flush task, so disk sees at
most one write per ``_FLUSH_INTERVAL_S`` (~5 s) regardless of beacon rate.
The flush snapshots under the lock and does the ``json.dumps`` + write in a
worker thread (``asyncio.to_thread``) so neither stalls the shared event
loop. A process-exit hook flushes any pending dirty state (best-effort; the
in-memory registry stays authoritative between flushes).
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Async lock, background tasks and worker threads.
import asyncio
# Registers the flush that runs when the process exits.
import atexit
# The registry is saved as JSON.
import json
# Standard-library logging.
import logging
# Timestamps for last-seen and streak tracking.
import time
# Tracks live registries without keeping them alive.
import weakref
# Filesystem path handling.
from pathlib import Path
# Type hints only.
from typing import Any, Callable, Dict, List, Optional

# Logger for this module.
logger = logging.getLogger("ClientRegistry")

# Keep at most this many recent error strings per client (rolling window).
_MAX_RECENT_ERRORS = 20
# How long gateway_reachable must hold a value continuously before the
# sim-quota engine trusts it as a confirmed drop/recovery, not on/off
# flicker (see apply_status). Mirrors sim_quota_engine.OFFLINE_TTL_S — keep
# both at 60 min.
_GATEWAY_CONFIRM_S = 3600.0
# Name of the saved registry file.
_STATE_FILE = "clients.json"
# Coalescing window for the debounced persist: at most one clients.json write
# per this many seconds, no matter how many beacons/mutations land.
_FLUSH_INTERVAL_S = 5.0

# Flush-on-shutdown: every live registry gets a best-effort synchronous flush
# of pending dirty state at interpreter exit (the event loop is gone by then,
# so the async flush task can't run). WeakSet so test registries can be GC'd.
_REGISTRIES: "weakref.WeakSet[ClientRegistry]" = weakref.WeakSet()


def _flush_all_at_exit() -> None:
    """Write any unsaved registry changes at interpreter exit."""
    # Flush each live registry that has unsaved changes.
    for reg in list(_REGISTRIES):
        try:
            if reg._dirty:
                reg._persist()
        except Exception:  # noqa: BLE001 — never break interpreter shutdown
            pass


# Run the flush when Python exits.
atexit.register(_flush_all_at_exit)


class ClientRegistry:
    """Per-sim-client registry persisted to ``data/clients.json``: last-seen,
    SSID, gateway reachability, running sims, recent errors, and the
    per-client persisted override flags (the Control Panel writes here).
    Upserted on every ``POST /api/status``; read by ``/api/config`` to bake
    a client's overrides into its profile. See the module docstring."""

    def __init__(self, data_dir: Path,
                 bucket_resolver: Optional[Callable[[str], Dict[str, Any]]] = None,
                 stale_prune_hours: float = 48.0,
                 ) -> None:
        """Set up the registry in ``data_dir`` and load the saved clients."""
        # Data folder (created if needed), client map, lock and file path.
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clients: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._path = self.data_dir / _STATE_FILE
        # Optional pure-bucket resolver (hostname -> profile dict) used by
        # set_overrides to prune overrides that match the simulation.conf bucket
        # default, so overrides stay a true diff over the bucket and a toggled-
        # off sim reverts to the bucket instead of accumulating a redundant
        # `flag:"off"` entry. None = no pruning (tests / standalone registry).
        self._bucket_resolver = bucket_resolver
        # Debounced-persist state: _dirty marks unflushed in-memory changes;
        # _flush_task is the armed coalescing flusher (None/done = disarmed).
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        # Stale-client prune sweep (see start()/prune_stale()). 48h default:
        # long enough that an ordinary overnight outage (dongle churn, a host
        # reboot) never gets pruned as a false ghost, short enough that a
        # genuinely renamed/decommissioned VM's old hostname doesn't linger
        # indefinitely — previously NOTHING removed an individual stale entry
        # short of a full Purge Clients wipe of the whole registry.
        self._stale_prune_hours = stale_prune_hours
        self._prune_task: Optional[asyncio.Task] = None
        # Register for the exit flush, then load saved clients.
        _REGISTRIES.add(self)
        self._load()

    def start(self) -> None:
        """Start the background stale-client prune sweep. No-op if there's no
        running loop (e.g. unit tests constructing a registry without one) —
        mirrors DemoManager.start()'s pattern."""
        # Already running: do nothing.
        if self._prune_task is not None and not self._prune_task.done():
            return
        # Start the hourly sweep.
        try:
            self._prune_task = asyncio.create_task(self._prune_loop())
        except RuntimeError:
            pass  # no current event loop

    async def _prune_loop(self) -> None:
        """Every hour, remove clients that have not reported within the prune window."""
        # Repeat until cancelled.
        while True:
            try:
                await asyncio.sleep(3600)  # hourly — this is an hours-scale concern, not a hot path
                res = await self.prune_stale(self._stale_prune_hours * 3600)
                # Log what was removed, if anything.
                if res["count"]:
                    logger.info("ClientRegistry: pruned %d stale client(s) (no heartbeat in %.0fh): %s",
                               res["count"], self._stale_prune_hours, res["pruned"])
            # Stop quietly when cancelled.
            except asyncio.CancelledError:
                return
            # Log other errors and keep going.
            except Exception as exc:  # noqa: BLE001
                logger.warning("client-registry prune loop error: %s", exc)

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        """Load the saved clients from disk (empty on any problem)."""
        # Read the file if it exists ("" is treated as empty).
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                loaded = json.loads(raw) if raw.strip() else {}
                # Keep only entries whose value is a dict.
                if isinstance(loaded, dict):
                    self.clients = {str(k): v for k, v in loaded.items()
                                    if isinstance(v, dict)}
        # A corrupt file is logged and the registry starts empty.
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not load %s: %s — starting empty", self._path, exc)
            self.clients = {}

    def _persist(self) -> None:
        """Synchronous full write (load-time repair / atexit flush only —
        the hot path goes through the debounced ``_apersist``)."""
        # Clear the dirty flag and write the whole map.
        try:
            self._dirty = False
            self._path.write_text(
                json.dumps(self.clients, sort_keys=True),
                encoding="utf-8")
        # Write failures are only logged.
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist %s: %s", self._path, exc)

    async def _apersist(self) -> None:
        """Mark dirty + arm the coalescing flush — does NOT write.

        Every mutator calls this under ``self._lock`` on every client beacon;
        rewriting all of clients.json each time serialized N clients to disk N
        times per beacon interval. Instead the write is deferred to
        ``_delayed_flush``, so at most one write happens per
        ``_FLUSH_INTERVAL_S`` no matter the mutation rate. Readers always see
        the in-memory ``self.clients`` (authoritative); the file is a warm-load
        snapshot, so a bounded staleness window is safe — a crash loses at most
        the last ~5 s of last_seen updates."""
        # Mark dirty and start a flush task if one is not already pending.
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.get_running_loop().create_task(
                self._delayed_flush())

    async def _delayed_flush(self) -> None:
        """Wait, flush, and repeat while new changes keep arriving."""
        # Loop: a mutation landing while _flush_now is writing re-marks dirty
        # but may see this task as not-done and skip re-arming — so re-check
        # after each flush instead of exiting immediately.
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            await self._flush_now()
            if not self._dirty:
                return

    async def _flush_now(self) -> None:
        """Write clients.json if dirty. Snapshot under the lock; the O(N)
        ``json.dumps`` AND the file write run in a worker thread so neither
        the serialization CPU nor the disk I/O blocks the shared event loop
        (the lock is held across the await, so ``self.clients`` is stable)."""
        # Hold the lock while writing; skip if nothing changed.
        async with self._lock:
            if not self._dirty:
                return
            try:
                clients = self.clients  # stable: we hold self._lock

                def _write() -> None:
                    """Write the snapshot to disk (runs in a worker thread)."""
                    # Serialise and write the file.
                    self._path.write_text(
                        json.dumps(clients, sort_keys=True), encoding="utf-8")

                # Do the write off the event loop.
                await asyncio.to_thread(_write)
                # Clear dirty only AFTER a successful write — a failed write must
                # keep us dirty so the next _delayed_flush retries instead of
                # silently losing the durable snapshot.
                self._dirty = False
            # A failed write stays dirty so the next flush retries.
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not persist %s: %s", self._path, exc)

    async def aclose(self) -> None:
        """Cancel the armed flusher and flush any pending state to disk.
        Call on orderly shutdown (tests, service stop); the atexit hook is the
        last-resort backstop for paths that skip this."""
        # Take the pending flush task and cancel it, waiting for it to finish.
        task, self._flush_task = self._flush_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Write whatever is still unsaved.
        await self._flush_now()

    # ── status upsert ──────────────────────────────────────────────────────
    async def apply_status(self, hostname: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Upsert *hostname* with *payload* (a status beacon) and return the entry.

        Merges the mutable fields a client reports (last_seen, connected_ssid,
        ip, gateway_reachable, active_simulations, errors, iteration,
        platform, simulation_id, config, status) without clobbering
        server-side fields like ``overrides``.
        """
        # Blank hostnames are stored under a placeholder.
        hostname = str(hostname or "").strip()
        if not hostname:
            hostname = "__unknown__"
        # Under the lock, fetch or create the entry and stamp the time.
        async with self._lock:
            entry = self.clients.get(hostname, {})
            entry["hostname"] = hostname
            entry["last_seen"] = time.time()

            # Copy every reported field the client sent.
            for key in ("simulation_id", "platform", "iteration",
                        "connected_ssid", "ip", "gateway_reachable",
                        "active_simulations", "config", "status", "has_usb",
                        "dns_ceiling",
                        # T3 IoT: the host advertises whether it can run the vwlan
                        # fleet + how many interfaces, so the sim-quota engine's
                        # device-quota pass can target it (see _reconcile_device_quotas).
                        "iot_capable", "iot_capacity",
                        # Full adapter inventory (name/mac/media_type/is_default_route
                        # per interface), self-reported live each heartbeat — lets the
                        # sim-quota engine keep media-tagged sims (assoc_fail etc.) off
                        # clients that lack a matching wired/wireless adapter.
                        "adapters"):
                if key in payload:
                    entry[key] = payload[key]

            # Dongle-quarantine detection state. ``first_seen`` is the grace-
            # window T0 (set once, on the first heartbeat). ``ever_connected``
            # latches True the first time the client reported a sim IP OR an
            # associated SSID — a client that NEVER connected (the QT trigger)
            # stays False; a client that connected then dropped (a mid-run
            # failure, out of scope) stays True and is left alone. Old entries
            # upgraded in place pick up first_seen=now (conservative: restarts
            # the 1h grace once).
            if not entry.get("first_seen"):
                entry["first_seen"] = entry["last_seen"]
            if payload.get("ip") or payload.get("connected_ssid"):
                entry["ever_connected"] = True
            elif "ever_connected" not in entry:
                entry["ever_connected"] = False

            # Gateway-reachability stability latch for the sim-quota engine's
            # working-client count (see sim_quota_engine._is_gateway_down_confirmed).
            # gateway_reachable flickers on/off within a single sim-loop iteration
            # normally, so the instantaneous reading is noisy — only a streak tells
            # a genuine drop (e.g. a detached dongle) apart from that flicker.
            # gateway_state/gateway_state_since track the CURRENT streak;
            # gateway_confirmed_down only flips once a streak (down or up) has
            # held continuously for _GATEWAY_CONFIRM_S (60 min) — so a client stays
            # excluded until it's been reachable that whole window, not just once.
            if "gateway_reachable" in payload and payload["gateway_reachable"] is not None:
                gw = bool(payload["gateway_reachable"])
                if entry.get("gateway_state") != gw:
                    entry["gateway_state"] = gw
                    entry["gateway_state_since"] = entry["last_seen"]
                streak_s = entry["last_seen"] - float(entry.get("gateway_state_since") or entry["last_seen"])
                if not gw and streak_s >= _GATEWAY_CONFIRM_S:
                    entry["gateway_confirmed_down"] = True
                elif gw and streak_s >= _GATEWAY_CONFIRM_S:
                    entry["gateway_confirmed_down"] = False
                # else: mid-streak (< 60 min either way) — leave the latch as-is

            # Merge errors into a rolling recent_errors window.
            errs: List[str] = list(payload.get("errors") or [])
            if errs:
                recent = list(entry.get("recent_errors", [])) + errs
                entry["recent_errors"] = recent[-_MAX_RECENT_ERRORS:]

            # Store the entry, schedule a save and return a copy.
            self.clients[hostname] = entry
            await self._apersist()
            return dict(entry)

    # ── read ───────────────────────────────────────────────────────────────
    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot copy of every registered client."""
        # No lock needed for a shallow copy of a dict that is only replaced
        # wholesale under the lock; mutators replace entries, not share them.
        return {k: dict(v) for k, v in self.clients.items()}

    def get(self, hostname: str) -> Optional[Dict[str, Any]]:
        """Return a copy of one client's entry, or None if unknown."""
        # Look up and copy the entry.
        entry = self.clients.get(hostname)
        return dict(entry) if entry is not None else None

    def count(self) -> int:
        """Return how many clients are registered."""
        # Size of the map.
        return len(self.clients)

    async def delete_one(self, hostname: str) -> bool:
        """Remove ONE client's registry record — the surgical counterpart to
        ``purge()`` (which wipes everything). For a single stale/unrecognized
        entry (a renamed/recloned VM's old hostname left behind, a one-off
        manual test client, etc.) that never ages out on its own — there is
        no automatic expiry here (see ``prune_stale`` for that), and until
        this method existed the only way to remove ONE ghost entry was to
        Purge Clients and lose every real client's history too. Returns
        False if the hostname wasn't registered (nothing to delete)."""
        # Blank hostnames match nothing.
        hostname = str(hostname or "").strip()
        if not hostname:
            return False
        # Under the lock, delete the entry if it exists and schedule a save.
        async with self._lock:
            if hostname not in self.clients:
                return False
            del self.clients[hostname]
            await self._apersist()
            return True

    async def prune_stale(self, max_age_s: float) -> Dict[str, Any]:
        """Remove every client whose ``last_seen`` is older than ``max_age_s``
        (or has no ``last_seen`` at all — a malformed/never-reported entry).
        Called on a slow periodic cadence (see the spoke's poll loop) so a
        renamed/decommissioned VM's old hostname doesn't linger in the
        registry forever — previously nothing ever removed an individual
        stale entry short of a full Purge Clients wipe. Returns
        {"pruned": [hostnames...], "count": N} so the caller can log what
        was removed (never silent — an unexpectedly large prune is exactly
        the kind of thing that should be visible, not swallowed)."""
        # Under the lock, find clients that are too old or never reported.
        now = time.time()
        async with self._lock:
            stale = [h for h, c in self.clients.items()
                     if not c.get("last_seen") or (now - float(c["last_seen"])) > max_age_s]
            # Delete them and save if any were removed.
            for h in stale:
                del self.clients[h]
            if stale:
                await self._apersist()
            return {"pruned": stale, "count": len(stale)}

    # ── per-client overrides ───────────────────────────────────────────────
    def _bucket_profile(self, hostname: str) -> Optional[Dict[str, Any]]:
        """Resolve the pure simulation.conf bucket default profile for
        *hostname*, or ``None`` on failure. A failed resolve MUST return None
        so callers skip pruning entirely (an empty profile would treat every
        key as "off" and wrongly prune "off" overrides — a toggle must never
        be silently dropped on an I/O hiccup)."""
        # No resolver wired: cannot prune.
        if not self._bucket_resolver:
            return None
        # Resolve the profile; failures return None so pruning is skipped.
        try:
            return self._bucket_resolver(hostname) or {}
        except Exception as exc:  # noqa: BLE001 — never block a toggle
            logger.warning("bucket resolve failed for %s: %s — skip prune",
                           hostname, exc)
            return None

    def _prune_redundant(self, cur: Dict[str, Any], hostname: str) -> None:
        """In-place: drop on/off overrides that match the pure bucket default.

        This is the "turn off a sim → it reverts to the bucket default →
        remove the override entry" behaviour: without it, every toggle-off
        leaves a ``flag:"off"`` entry that masks the bucket, so the override
        object grows to mirror the bucket instead of being a diff. An
        override that genuinely deviates (e.g. turning OFF a sim the bucket
        has ON) is kept. Only on/off sim-toggle flags are prune candidates —
        free-form overrides (``wsite="MIA"``, ``ssid="…"``, ``simulation_id``
        …) are never pruned (a bucket default of "" would delete them the
        instant they're written); the SimQuotaEngine re-home sets
        ``wsite=<quota site>`` and that MUST survive. No-op when *cur* is
        empty or no resolver is wired (tests / standalone registry)."""
        # Nothing to prune.
        if not cur:
            return
        # Bucket default profile; skip pruning if it cannot be resolved.
        profile = self._bucket_profile(hostname)
        if profile is None:
            return
        # Drop each on/off override that equals the bucket's value.
        for flag in list(cur.keys()):
            ov_val = str(cur[flag]).strip().lower()
            if ov_val not in ("on", "off"):
                continue
            bucket_on = str(profile.get(flag, "")).strip().lower() == "on"
            if bucket_on == (ov_val == "on"):
                del cur[flag]

    async def set_overrides(self, hostname: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``overrides`` into a client's overrides, drop redundant ones, and save."""
        # Under the lock, fetch or create the client's entry.
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.setdefault(hostname, {"hostname": hostname})
            entry["hostname"] = hostname
            # MERGE (not replace) so a per-sim toggle that sends a single flag
            # doesn't wipe the client's other overrides. The WebUI sends one flag
            # per click; the Control panel's "Apply" sends the full set (merge of
            # the full set == replace, so no behavior change there). clear_overrides
            # removes them all.
            # Merge the new values in.
            cur = dict(entry.get("overrides") or {})
            if isinstance(overrides, dict):
                cur.update(overrides)
            # Remove overrides that equal the bucket default, then save.
            self._prune_redundant(cur, hostname)
            entry["overrides"] = cur
            self.clients[hostname] = entry
            await self._apersist()
            return dict(entry)

    async def set_engine_overrides(self, hostname: str,
                                   overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Set overrides the SimQuotaEngine owns and record provenance.

        Same merge+prune as ``set_overrides``, but ALSO records the keys set
        into ``engine_keys`` so a later reconcile can remove ONLY engine-set
        keys the ledger no longer claims — never a human manual pin (a pin
        written via ``set_overrides`` / the Control Panel is never added to
        ``engine_keys``). The engine owns ``sim_id`` (the sim flag it toggles
        on) and ``wsite`` (a re-home) on its ledger clients. Keys pruned away
        by the bucket-default match are dropped from ``engine_keys`` too
        (there's nothing to revert)."""
        # Under the lock, fetch or create the client's entry.
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.setdefault(hostname, {"hostname": hostname})
            entry["hostname"] = hostname
            # Current overrides and the keys the engine owns.
            cur = dict(entry.get("overrides") or {})
            eng = list(entry.get("engine_keys") or [])
            # Merge the new values and record each key as engine-owned.
            if isinstance(overrides, dict):
                cur.update(overrides)
                for k in overrides:
                    if k not in eng:
                        eng.append(k)
            # Remove overrides that equal the bucket default.
            self._prune_redundant(cur, hostname)
            # A key pruned (matched the bucket default) is no longer an
            # override — drop it from engine_keys so reconcile doesn't try to
            # remove a key that isn't there.
            eng = [k for k in eng if k in cur]
            # Save overrides and owned keys.
            entry["overrides"] = cur
            entry["engine_keys"] = eng
            self.clients[hostname] = entry
            await self._apersist()
            return dict(entry)

    async def remove_engine_keys(self, hostname: str,
                                 keys: List[str]) -> Dict[str, Any]:
        """Remove keys the engine owns from both ``overrides`` and
        ``engine_keys`` — revert to the bucket default by DELETION, not by
        setting ``"off"`` (which lingers as a real override when the bucket
        default is ON, keeping a released sim forced off instead of
        reverting). Called by the SimQuotaEngine reconcile tail for
        engine-set keys the ledger no longer claims (the missed-_release
        leak). No-op for a key not present."""
        # Nothing to remove.
        hostname = str(hostname or "").strip()
        keys = list(keys or [])
        if not keys:
            return {}
        # Under the lock; unknown clients return {}.
        async with self._lock:
            entry = self.clients.get(hostname)
            if entry is None:
                return {}
            # Current overrides and engine-owned keys.
            cur = dict(entry.get("overrides") or {})
            eng = list(entry.get("engine_keys") or [])
            changed = False
            # Remove each key from both lists.
            for k in keys:
                if k in cur:
                    del cur[k]
                    changed = True
                if k in eng:
                    eng.remove(k)
                    changed = True
            # Save only if something changed.
            if changed:
                entry["overrides"] = cur
                entry["engine_keys"] = eng
                self.clients[hostname] = entry
                await self._apersist()
            # Return a copy of the entry.
            return dict(entry)

    async def prune_against_bucket(self, hostname: str) -> Dict[str, Any]:
        """Re-prune this client's on/off overrides against the CURRENT bucket
        default — drop flags that now match the bucket (no-ops).
        ``set_overrides`` prunes at write time; this catches flags that
        became redundant because the bucket default changed LATER (e.g. a
        sim flag pinned "off" when the bucket was on is no longer a
        deviation once the bucket flips off). Called by the SimQuotaEngine
        sweep. Only removes overrides that match the bucket, so the served
        config is unchanged. Non-toggle overrides (wsite/ssid/…) are never
        pruned. Also drops any engine_keys that the prune removed."""
        # Under the lock; unknown clients return {}.
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.get(hostname)
            if entry is None:
                return {}
            # No overrides: nothing to prune.
            cur = dict(entry.get("overrides") or {})
            if not cur:
                return dict(entry)
            # Prune and stop if nothing changed.
            before = dict(cur)
            self._prune_redundant(cur, hostname)
            if cur == before:
                return dict(entry)
            # Save the pruned overrides, dropping engine keys that are gone.
            entry["overrides"] = cur
            eng = entry.get("engine_keys") or []
            new_eng = [k for k in eng if k in cur]
            if new_eng != eng:
                entry["engine_keys"] = new_eng
            # Save and return a copy.
            self.clients[hostname] = entry
            await self._apersist()
            return dict(entry)

    async def clear_overrides(self, hostname: str) -> Dict[str, Any]:
        """Remove all of a client's overrides and return its entry ({} if unknown)."""
        # Under the lock, drop the overrides and save.
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.get(hostname)
            if entry is not None:
                entry.pop("overrides", None)
                await self._apersist()
                return dict(entry)
            # Unknown client.
            return {}

    async def record_tiers_batch(self, updates: Dict[str, Dict[str, Any]]) -> None:
        """Persist the last-known *authoritative* tier/has_usb per hostname.

        Called from the telemetry builder (control_plane.py + local_ui_routes)
        for clients whose Proxmox VM is currently reporting, so that later,
        when the host/agent goes offline and ``vmid`` no longer resolves, the
        builder can fall back to this cached tier instead of dropping the row
        to T1 (the "offline clients lose their T2" bug). ``tier`` is the
        hypervisor-truth join from the agent's ``compute_vm_tiers`` — NOT the
        client's own ``has_usb`` self-report, which is unreliable for T2.

        Single lock + single persist, change-gated so a steady-state tick
        (no tier transitions) writes nothing. Only updates clients already in
        the registry — the join is meaningless for an unknown host.
        """
        # Nothing to record.
        if not updates:
            return
        # Under the lock, update each known client whose tier changed.
        async with self._lock:
            changed = False
            for hn, upd in updates.items():
                entry = self.clients.get(hn)
                if entry is None:
                    continue
                # The new tier and USB state.
                tier = upd.get("tier")
                has_usb = upd.get("has_usb")
                # Store them only if they differ.
                if entry.get("tier") != tier or entry.get("last_known_has_usb") != has_usb:
                    entry["tier"] = tier
                    entry["last_known_has_usb"] = bool(has_usb) if has_usb is not None else None
                    changed = True
            # Save only when something changed.
            if changed:
                await self._apersist()

    async def purge(self) -> Dict[str, Any]:
        """Drop every registered client from memory AND delete ``clients.json``
        on disk, restoring a fresh-empty registry. This is the lm-spoke
        equivalent of the legacy cs-webui "Purge Clients" button
        (``DELETE /api/clients/history``) — it removes all client records from
        memory and disk (cannot be undone). Returns ``{"purged": <count>}``
        naming how many clients were removed. Disk unlink offloaded via
        ``asyncio.to_thread`` for the same event-loop reason as ``_apersist``.
        """
        # Under the lock, count and drop every client.
        async with self._lock:
            n = len(self.clients)
            self.clients = {}
            # Clear the dirty flag so a pending debounced flush can't
            # resurrect clients.json right after the unlink below.
            self._dirty = False
            # Delete the file off the event loop; failure is only logged.
            try:
                await asyncio.to_thread(self._path.unlink, missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not remove %s: %s", self._path, exc)
            # Report how many clients were removed.
            return {"purged": n}