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

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# asyncio provides the lock and the background expiry task.
import asyncio
# Standard-library logging for expiry/notification messages.
import logging
# Used to stamp and check scenario expiry times.
import time
# Type hints only.
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Dedicated logger so demo messages can be filtered on their own.
logger = logging.getLogger("CSDemo")

# How long a demo scenario stays active before auto-expiring (seconds).
DEMO_TTL_SECONDS = 120 * 60  # 120 minutes
# Every failure flag a demo can turn on; each one becomes its own scenario.
FAILURE_FLAGS = ("dns_fail", "dns_latency", "dhcp_fail", "assoc_fail",
                 "auth_fail", "ssidpw_fail", "mac_auth_fail", "port_flap")


def build_scenarios() -> Dict[str, Dict[str, str]]:
    """``normal`` = all flags off; one scenario per failure flag (that flag on,
    the rest off)."""
    # Start with the "normal" scenario, which turns every flag off.
    scenarios: Dict[str, Dict[str, str]] = {
        "normal": {f: "off" for f in FAILURE_FLAGS},
    }
    # Add one scenario per flag, named after the flag.
    for flag in FAILURE_FLAGS:
        # Only that flag is "on"; every other flag is explicitly "off".
        scenarios[flag] = {f: ("on" if f == flag else "off") for f in FAILURE_FLAGS}
    # Return the full name -> flags table.
    return scenarios


# Built once at import; this is the catalogue the hub can choose from.
DEMO_SCENARIOS: Dict[str, Dict[str, str]] = build_scenarios()


class DemoManager:
    """In-memory per-client demo-scenario state with TTL auto-expiry."""

    def __init__(self, on_change: Optional[Callable[[str], Awaitable[None]]] = None
                 ) -> None:
        """Create an empty manager; ``on_change`` is awaited with a hostname on each change."""
        # hostname → {scenario, flags, expires_at, triggered_by}
        # Active scenarios keyed by hostname (shape described above).
        self._active: Dict[str, Dict[str, Any]] = {}
        # Serialises changes to _active between concurrent commands.
        self._lock = asyncio.Lock()
        # Handle of the background expiry task, once started.
        self._expiry_task: Optional[asyncio.Task] = None
        # Fired (best-effort) whenever a client's demo flags change — apply,
        # clear, OR the 120-min expiry sweep. The spoke passes a callback that
        # enqueues ``update_now`` so the client re-fetches /api/config and its
        # LOCAL simulation.conf picks up the change. Without this a demo
        # expiring on the spoke leaves the client serving a stale [username]
        # from its cached file (update.sh runs only on update_now / a VERSION
        # bump), so the 2-hour auto-clear never reached the client — the bug
        # behind "the overrides are still there after they should have expired".
        # Optional coroutine to call with a hostname when its flags change (see above).
        self._on_change = on_change

    def start(self) -> None:
        """Start the background expiry sweep. No-op if there's no running loop
        (e.g. unit tests constructing a spoke without a loop)."""
        # Do nothing if the sweep task is already running.
        if self._expiry_task is not None and not self._expiry_task.done():
            return
        # Launch the sweep loop in the background on the current event loop.
        try:
            self._expiry_task = asyncio.create_task(self._expiry_loop())
        # No running event loop (e.g. in tests): skip the background sweep.
        except RuntimeError:
            pass  # no current event loop

    async def _expiry_loop(self) -> None:
        """Every 30 seconds remove expired demo scenarios, until cancelled."""
        # Loop forever until the task is cancelled.
        while True:
            try:
                # Check for expired scenarios every 30 seconds.
                await asyncio.sleep(30)
                await self.sweep_expired()
            # The spoke is shutting down: exit the loop cleanly.
            except asyncio.CancelledError:
                return
            # Log any other error and keep looping so one failure does not stop expiry.
            except Exception as exc:  # noqa: BLE001
                logger.warning("demo expiry loop error: %s", exc)

    async def _fire_on_change(self, hostname: str) -> None:
        """Best-effort: tell the spoke a client's demo flags changed so it can
        push a re-fetch. Never raises — a push failure must not break the
        demo mutation that triggered it."""
        # Nothing to notify if no callback is set or the hostname is empty.
        cb = self._on_change
        if cb is None or not hostname:
            return
        # Run the callback (typically queues an update_now for the client).
        try:
            await cb(hostname)
        # Swallow and log failures so the demo change itself still succeeds.
        except Exception as exc:  # noqa: BLE001
            logger.debug("demo on_change for %s failed: %s", hostname, exc)

    async def sweep_expired(self) -> int:
        """Clear every expired scenario and return how many were cleared."""
        # Find every hostname whose scenario has passed its expiry time.
        now = time.time()
        expired = [h for h, v in list(self._active.items())
                   if v["expires_at"] <= now]
        # Clear each expired scenario; clear() also notifies the client.
        for h in expired:
            logger.info("Demo override expired for %s — reverting to normal", h)
            await self.clear(h)  # clear() fires on_change → client re-fetches
        # Report how many scenarios expired.
        return len(expired)

    async def apply(self, hostname: str, scenario: str,
                    triggered_by: str = "") -> Dict[str, Any]:
        """Apply a named scenario to a client. ``normal`` clears the demo.
        Raises ``ValueError`` on an unknown scenario name."""
        # Look up the flags for the requested scenario name.
        flags = DEMO_SCENARIOS.get(scenario)
        # Reject unknown names, listing the valid ones in the error.
        if flags is None:
            raise ValueError(
                f"Unknown scenario '{scenario}'. Valid: {sorted(DEMO_SCENARIOS)}")
        # Change the active table under the lock so concurrent commands do not collide.
        async with self._lock:
            # "normal" means remove any active demo for this client.
            if scenario == "normal":
                self._active.pop(hostname, None)
            # Otherwise record the scenario, a copy of its flags, its expiry time and who started
            # it.
            else:
                self._active[hostname] = {
                    "scenario": scenario,
                    "flags": dict(flags),
                    "expires_at": time.time() + DEMO_TTL_SECONDS,
                    "triggered_by": triggered_by,
                }
        # Tell the client to re-fetch its config so the change takes effect.
        await self._fire_on_change(hostname)
        # Return the client's current demo state.
        return await self.summary_one(hostname)

    async def clear(self, hostname: str) -> bool:
        """Remove a client's active demo scenario; return True if one was removed."""
        # Remove the client's scenario under the lock, noting whether there was one.
        async with self._lock:
            was = self._active.pop(hostname, None) is not None
        # Only notify the client if something actually changed.
        if was:
            await self._fire_on_change(hostname)
        # True when a scenario was removed.
        return was

    def clear_all(self) -> None:
        """Synchronous best-effort clear of all demo overrides (used on hub
        reconnect, mirroring the legacy ``_clear_all_demo_scenarios_sync``)."""
        # Drop every active scenario immediately, without notifying clients.
        self._active.clear()

    def effective_flags(self, hostname: str) -> Dict[str, str]:
        """Live demo flags for a hostname (empty if none / lazy-expired). These
        are layered on top of the registry's persisted overrides at config
        delivery time so a demo never touches the persisted store."""
        # Look up this client's active scenario.
        v = self._active.get(hostname)
        # No scenario means no extra flags.
        if not v:
            return {}
        # Already expired but the sweep has not run yet: treat as inactive.
        if v["expires_at"] <= time.time():
            return {}  # expired but not yet swept
        # Return a copy so callers cannot modify the stored flags.
        return dict(v["flags"])

    async def active_summary(self) -> List[Dict[str, Any]]:
        """Return a summary row for every client with an active demo scenario."""
        # Drop expired entries first so the summary is accurate.
        await self.sweep_expired()
        # One "now" for all rows so the minutes remaining are consistent.
        now = time.time()
        # One summary row per active client, including minutes left (never negative).
        return [
            {"hostname": h, "scenario": v["scenario"],
             "triggered_by": v.get("triggered_by", ""),
             "expires_at": v["expires_at"],
             "minutes_remaining": max(0, round((v["expires_at"] - now) / 60, 1))}
            for h, v in list(self._active.items())
        ]

    async def summary_one(self, hostname: str) -> Dict[str, Any]:
        """Return the demo state of one client."""
        # Look up this client's active scenario.
        v = self._active.get(hostname)
        # No scenario: report None with zero minutes remaining.
        if not v:
            return {"hostname": hostname, "scenario": None, "minutes_remaining": 0}
        # Report the scenario and the minutes left before it expires (never negative).
        now = time.time()
        return {"hostname": hostname, "scenario": v["scenario"],
                "minutes_remaining": max(0, round((v["expires_at"] - now) / 60, 1))}