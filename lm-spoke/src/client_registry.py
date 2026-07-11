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
so the WS receive loop and HTTP handlers can't tear each other's writes. The
per-mutation persist runs on every client beacon (~20 sim clients) on the SAME
event loop as the hub connection + uvicorn API, so the disk write is offloaded
to a thread (``_apersist`` → ``asyncio.to_thread``); the list is snapshotted on
the loop under the lock so the write sees a consistent state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ClientRegistry")

# Keep at most this many recent error strings per client (rolling window).
_MAX_RECENT_ERRORS = 20
_STATE_FILE = "clients.json"


class ClientRegistry:
    """Per-sim-client registry persisted to ``data/clients.json``: last-seen,
    SSID, gateway reachability, running sims, recent errors, and the
    per-client persisted override flags (the Control Panel writes here).
    Upserted on every ``POST /api/status``; read by ``/api/config`` to bake
    a client's overrides into its profile. See the module docstring."""

    def __init__(self, data_dir: Path,
                 bucket_resolver: Optional[Callable[[str], Dict[str, Any]]] = None
                 ) -> None:
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
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                loaded = json.loads(raw) if raw.strip() else {}
                if isinstance(loaded, dict):
                    self.clients = {str(k): v for k, v in loaded.items()
                                    if isinstance(v, dict)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not load %s: %s — starting empty", self._path, exc)
            self.clients = {}

    def _persist(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self.clients, indent=2, sort_keys=True),
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist %s: %s", self._path, exc)

    async def _apersist(self) -> None:
        """Persist off the event loop. apply_status runs on every client beacon
        (~20 sim clients), on the SAME loop as the hub connection + uvicorn API,
        so a synchronous write here contributed to the hub's Request Timeouts.
        The caller (apply_status) holds ``self._lock`` across this await, so
        ``self.clients`` cannot mutate while the worker runs — do BOTH the
        O(N) ``json.dumps`` AND the file write in the worker thread so neither
        the serialization CPU nor the disk I/O blocks the shared event loop.
        Drops indent to shrink serialization + file size."""
        try:
            clients = self.clients  # stable: caller holds self._lock

            def _write() -> None:
                self._path.write_text(
                    json.dumps(clients, sort_keys=True), encoding="utf-8")

            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist %s: %s", self._path, exc)

    # ── status upsert ──────────────────────────────────────────────────────
    async def apply_status(self, hostname: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Upsert *hostname* with *payload* (a status beacon) and return the entry.

        Merges the mutable fields a client reports (last_seen, connected_ssid,
        gateway_reachable, active_simulations, errors, iteration, platform,
        simulation_id, config, status) without clobbering server-side fields
        like ``overrides``.
        """
        hostname = str(hostname or "").strip()
        if not hostname:
            hostname = "__unknown__"
        async with self._lock:
            entry = self.clients.get(hostname, {})
            entry["hostname"] = hostname
            entry["last_seen"] = time.time()

            for key in ("simulation_id", "platform", "iteration",
                        "connected_ssid", "gateway_reachable",
                        "active_simulations", "config", "status", "has_usb"):
                if key in payload:
                    entry[key] = payload[key]

            # Merge errors into a rolling recent_errors window.
            errs: List[str] = list(payload.get("errors") or [])
            if errs:
                recent = list(entry.get("recent_errors", [])) + errs
                entry["recent_errors"] = recent[-_MAX_RECENT_ERRORS:]

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
        entry = self.clients.get(hostname)
        return dict(entry) if entry is not None else None

    def count(self) -> int:
        return len(self.clients)

    # ── per-client overrides ───────────────────────────────────────────────
    async def set_overrides(self, hostname: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.setdefault(hostname, {"hostname": hostname})
            entry["hostname"] = hostname
            # MERGE (not replace) so a per-sim toggle that sends a single flag
            # doesn't wipe the client's other overrides. The WebUI sends one flag
            # per click; the Control panel's "Apply" sends the full set (merge of
            # the full set == replace, so no behavior change there). clear_overrides
            # removes them all.
            cur = dict(entry.get("overrides") or {})
            if isinstance(overrides, dict):
                cur.update(overrides)
            # PRUNE redundant overrides: drop any key whose on/off value matches
            # the pure simulation.conf bucket default for this hostname. This is
            # the "turn off a sim → it reverts to the bucket default → remove the
            # override entry" behaviour: without it, every toggle-off leaves a
            # `flag:"off"` entry that masks the bucket, so the override object
            # grows to mirror the bucket instead of being a diff. An override that
            # genuinely deviates from the bucket (e.g. turning OFF a sim the
            # bucket has ON) is kept. Best-effort: a resolver failure leaves the
            # merged overrides intact (never blocks a toggle).
            if self._bucket_resolver and cur:
                profile = None
                try:
                    profile = self._bucket_resolver(hostname) or {}
                except Exception as exc:  # noqa: BLE001 — never block a toggle
                    logger.warning("bucket resolve failed for %s: %s — skip prune",
                                   hostname, exc)
                # Only prune when we actually resolved the bucket; a failed
                # resolve MUST leave the merged overrides intact (an empty
                # profile would treat every key as "off" and wrongly prune
                # "off" overrides — a toggle must never be silently dropped on
                # an I/O hiccup).
                if profile is not None:
                    for flag in list(cur.keys()):
                        bucket_on = str(profile.get(flag, "")).strip().lower() == "on"
                        ov_on = str(cur[flag]).strip().lower() == "on"
                        if bucket_on == ov_on:
                            del cur[flag]
            entry["overrides"] = cur
            self.clients[hostname] = entry
            await self._apersist()
            return dict(entry)

    async def clear_overrides(self, hostname: str) -> Dict[str, Any]:
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.get(hostname)
            if entry is not None:
                entry.pop("overrides", None)
                await self._apersist()
                return dict(entry)
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
        if not updates:
            return
        async with self._lock:
            changed = False
            for hn, upd in updates.items():
                entry = self.clients.get(hn)
                if entry is None:
                    continue
                tier = upd.get("tier")
                has_usb = upd.get("has_usb")
                if entry.get("tier") != tier or entry.get("last_known_has_usb") != has_usb:
                    entry["tier"] = tier
                    entry["last_known_has_usb"] = bool(has_usb) if has_usb is not None else None
                    changed = True
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
        async with self._lock:
            n = len(self.clients)
            self.clients = {}
            try:
                await asyncio.to_thread(self._path.unlink, missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not remove %s: %s", self._path, exc)
            return {"purged": n}