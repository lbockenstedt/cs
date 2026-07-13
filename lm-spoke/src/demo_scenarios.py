"""Named demo-failure scenarios for the cs spoke (port of the legacy
``cs/webui-spoke/server.py`` demo system, lines 12677-12825).

A demo scenario is an EPHEMERAL, in-memory per-client live override that turns
on one failure flag (``dns_fail`` / ``dhcp_fail`` / ``assoc_fail`` / ``auth_fail``
/ ``ssidpw_fail`` / ``port_flap``) — or ``normal`` to clear them — for a single
client, with a 120-minute TTL and auto-expiry. The hub/UI triggers it via
``CS_DEMO_SCENARIO`` and clears it via ``CS_DEMO_CLEAR``; the live flags are
layered ON TOP of the registry's persisted per-client overrides at config
delivery time (``client_api /api/config``) so a demo never mutates the persisted
override store — it expires or clears back to whatever the operator had set.

Unlike the legacy spoke (which kept demo state next to an in-memory clients
dict), the new arch persists client overrides to ``data/client-status.json``;
keeping demos in this separate in-memory manager preserves the legacy
"expires after 120 min or on reboot" intent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("CSDemo")

DEMO_TTL_SECONDS = 120 * 60  # 120 minutes
FAILURE_FLAGS = ("dns_fail", "dhcp_fail", "assoc_fail", "auth_fail",
                 "ssidpw_fail", "port_flap")


def build_scenarios() -> Dict[str, Dict[str, str]]:
    """``normal`` = all flags off; one scenario per failure flag (that flag on,
    the rest off)."""
    scenarios: Dict[str, Dict[str, str]] = {
        "normal": {f: "off" for f in FAILURE_FLAGS},
    }
    for flag in FAILURE_FLAGS:
        scenarios[flag] = {f: ("on" if f == flag else "off") for f in FAILURE_FLAGS}
    return scenarios


DEMO_SCENARIOS: Dict[str, Dict[str, str]] = build_scenarios()


class DemoManager:
    """In-memory per-client demo-scenario state with TTL auto-expiry."""

    def __init__(self, on_change: Optional[Callable[[str], Awaitable[None]]] = None
                 ) -> None:
        # hostname → {scenario, flags, expires_at, triggered_by}
        self._active: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._expiry_task: Optional[asyncio.Task] = None
        # Fired (best-effort) whenever a client's demo flags change — apply,
        # clear, OR the 120-min expiry sweep. The spoke passes a callback that
        # enqueues ``update_now`` so the client re-fetches /api/config and its
        # LOCAL simulation.conf picks up the change. Without this a demo
        # expiring on the spoke leaves the client serving a stale [username]
        # from its cached file (update.sh runs only on update_now / a VERSION
        # bump), so the 2-hour auto-clear never reached the client — the bug
        # behind "the overrides are still there after they should have expired".
        self._on_change = on_change

    def start(self) -> None:
        """Start the background expiry sweep. No-op if there's no running loop
        (e.g. unit tests constructing a spoke without a loop)."""
        if self._expiry_task is not None and not self._expiry_task.done():
            return
        try:
            self._expiry_task = asyncio.create_task(self._expiry_loop())
        except RuntimeError:
            pass  # no current event loop

    async def _expiry_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.sweep_expired()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("demo expiry loop error: %s", exc)

    async def _fire_on_change(self, hostname: str) -> None:
        """Best-effort: tell the spoke a client's demo flags changed so it can
        push a re-fetch. Never raises — a push failure must not break the
        demo mutation that triggered it."""
        cb = self._on_change
        if cb is None or not hostname:
            return
        try:
            await cb(hostname)
        except Exception as exc:  # noqa: BLE001
            logger.debug("demo on_change for %s failed: %s", hostname, exc)

    async def sweep_expired(self) -> int:
        now = time.time()
        expired = [h for h, v in list(self._active.items())
                   if v["expires_at"] <= now]
        for h in expired:
            logger.info("Demo override expired for %s — reverting to normal", h)
            await self.clear(h)  # clear() fires on_change → client re-fetches
        return len(expired)

    async def apply(self, hostname: str, scenario: str,
                    triggered_by: str = "") -> Dict[str, Any]:
        """Apply a named scenario to a client. ``normal`` clears the demo.
        Raises ``ValueError`` on an unknown scenario name."""
        flags = DEMO_SCENARIOS.get(scenario)
        if flags is None:
            raise ValueError(
                f"Unknown scenario '{scenario}'. Valid: {sorted(DEMO_SCENARIOS)}")
        async with self._lock:
            if scenario == "normal":
                self._active.pop(hostname, None)
            else:
                self._active[hostname] = {
                    "scenario": scenario,
                    "flags": dict(flags),
                    "expires_at": time.time() + DEMO_TTL_SECONDS,
                    "triggered_by": triggered_by,
                }
        await self._fire_on_change(hostname)
        return await self.summary_one(hostname)

    async def clear(self, hostname: str) -> bool:
        async with self._lock:
            was = self._active.pop(hostname, None) is not None
        if was:
            await self._fire_on_change(hostname)
        return was

    def clear_all(self) -> None:
        """Synchronous best-effort clear of all demo overrides (used on hub
        reconnect, mirroring the legacy ``_clear_all_demo_scenarios_sync``)."""
        self._active.clear()

    def effective_flags(self, hostname: str) -> Dict[str, str]:
        """Live demo flags for a hostname (empty if none / lazy-expired). These
        are layered on top of the registry's persisted overrides at config
        delivery time so a demo never touches the persisted store."""
        v = self._active.get(hostname)
        if not v:
            return {}
        if v["expires_at"] <= time.time():
            return {}  # expired but not yet swept
        return dict(v["flags"])

    async def active_summary(self) -> List[Dict[str, Any]]:
        await self.sweep_expired()
        now = time.time()
        return [
            {"hostname": h, "scenario": v["scenario"],
             "triggered_by": v.get("triggered_by", ""),
             "expires_at": v["expires_at"],
             "minutes_remaining": max(0, round((v["expires_at"] - now) / 60, 1))}
            for h, v in list(self._active.items())
        ]

    async def summary_one(self, hostname: str) -> Dict[str, Any]:
        v = self._active.get(hostname)
        if not v:
            return {"hostname": hostname, "scenario": None, "minutes_remaining": 0}
        now = time.time()
        return {"hostname": hostname, "scenario": v["scenario"],
                "minutes_remaining": max(0, round((v["expires_at"] - now) / 60, 1))}