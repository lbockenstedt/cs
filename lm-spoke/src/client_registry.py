"""ClientRegistry — the cs spoke's per-host client registry (Phase 2).

The spoke's client-facing surface (`client_api.py`) needs to track every
simulation client that has reported in over ``POST /api/status`` or the
``/ws/client`` socket: last-seen, connected SSID, gateway reachability, the
simulations it is running, and per-client config overrides the hub/UI can push
(``/api/clients/{hostname}/control``).

This is the lm-spoke equivalent of the webui-spoke ``clients`` dict +
``CLIENT_HISTORY_FILE``. State is persisted to ``data/clients.json`` (runtime
state — covered by ``.gitignore``, never committed).

The registry is small and the dict ops trivial, so persistence is synchronous
per mutation (no debounce needed at this scale). All public mutators are async
and serialize through a single ``asyncio.Lock`` so the WS receive loop and HTTP
handlers can't tear each other's writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ClientRegistry")

# Keep at most this many recent error strings per client (rolling window).
_MAX_RECENT_ERRORS = 20
_STATE_FILE = "clients.json"


class ClientRegistry:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clients: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._path = self.data_dir / _STATE_FILE
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
                        "active_simulations", "config", "status"):
                if key in payload:
                    entry[key] = payload[key]

            # Merge errors into a rolling recent_errors window.
            errs: List[str] = list(payload.get("errors") or [])
            if errs:
                recent = list(entry.get("recent_errors", [])) + errs
                entry["recent_errors"] = recent[-_MAX_RECENT_ERRORS:]

            self.clients[hostname] = entry
            self._persist()
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
            entry["overrides"] = dict(overrides) if isinstance(overrides, dict) else {}
            self.clients[hostname] = entry
            self._persist()
            return dict(entry)

    async def clear_overrides(self, hostname: str) -> Dict[str, Any]:
        hostname = str(hostname or "").strip()
        async with self._lock:
            entry = self.clients.get(hostname)
            if entry is not None:
                entry.pop("overrides", None)
                self._persist()
                return dict(entry)
            return {}