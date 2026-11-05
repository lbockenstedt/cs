"""Sim command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_sim`` looks the command up in
``_SIM_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Standard-library logging for handler messages.
import logging
# Catalogue of named demo scenarios returned by CS_GET_DEMO_SCENARIOS.
from demo_scenarios import DEMO_SCENARIOS
# Type hints only.
from typing import Any, Dict, Optional

# Share the spoke's logger so these messages appear alongside the rest of the spoke.
logger = logging.getLogger("CSSpoke")


class SimCommandsMixin:
    """Mixin handling simulation profile switching, kill switches, and demo scenarios."""

    # Map of command name -> handler method name; legacy names without CS_ map to the same handler.
    _SIM_COMMANDS = {
        "CS_TRIGGER_ITERATION": "_cmd_trigger_iteration",
        "TRIGGER_ITERATION": "_cmd_trigger_iteration",
        "CS_GET_SIMULATION_STATE": "_cmd_get_simulation_state",
        "GET_SIMULATION_STATE": "_cmd_get_simulation_state",
        "CS_SET_SIMULATION_PROFILE": "_cmd_set_simulation_profile",
        "SET_SIMULATION_PROFILE": "_cmd_set_simulation_profile",
        "CS_KILL_SWITCH": "_cmd_kill_switch",
        "CS_GET_KILL_SWITCH": "_cmd_get_kill_switch",
        "CS_DEMO_SCENARIO": "_cmd_demo_scenario",
        "CS_DEMO_CLEAR": "_cmd_demo_clear",
        "CS_GET_DEMO_ACTIVE": "_cmd_get_demo_active",
        "CS_GET_DEMO_SCENARIOS": "_cmd_get_demo_scenarios",
    }

    async def _dispatch_sim(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the handler for ``cmd`` from ``_SIM_COMMANDS``; None if it is not one of ours."""
        # Look up which method handles this command.
        handler = self._SIM_COMMANDS.get(cmd)
        # Not a simulation command: return None so the next domain mixin can try it.
        if handler is None:
            return None
        # Call the handler method on the spoke and return its reply.
        return await getattr(self, handler)(d)

    # ── simulation execution ────────────────────────────────────────────
    async def _cmd_trigger_iteration(self, d):
        """Run one simulation iteration now and return its result."""
        # Run one simulation iteration immediately instead of waiting for the schedule.
        result = await self.engine.run_iteration()
        # Merge the iteration result into a success reply.
        return {"status": "SUCCESS", **result}

    async def _cmd_get_simulation_state(self, d):
        """Return the simulation engine's current state."""
        # Return the engine's current state snapshot in a success reply.
        return {"status": "SUCCESS", **self.engine.get_current_state()}

    async def _cmd_set_simulation_profile(self, d):
        """Patch the simulation profile with the fields sent by the hub."""
        # Apply the supplied profile changes on top of the current engine config.
        self.engine.update_config(d.get("profile", {}))
        # Confirm which host's profile was changed.
        return {"status": "SUCCESS",
                "message": f"Profile patched for {self.engine.hostname}"}

    # ── kill switch ────────────────────────────────────────────────────
    async def _cmd_kill_switch(self, d):
        """Turn the simulation kill switch on or off."""
        # Accept either the "on" or legacy "kill_switch" key; missing means off.
        on = bool(d.get("on", d.get("kill_switch", False)))
        # Tell the engine to stop (or resume) simulated activity.
        self.engine.set_kill_switch(on)
        # Echo the new state back to the hub.
        return {"status": "SUCCESS", "kill_switch": on}

    async def _cmd_get_kill_switch(self, d):
        """Return the current kill-switch state."""
        # Read for the hub's kill-switch banner. Sits BEFORE the
        # NOT_IMPLEMENTED matcher (whose set includes "GET") so the hub's
        # GET /kill-switch doesn't hit a dead command.
        # Report whether the kill switch is currently active.
        return {"status": "SUCCESS",
                "kill_switch": self.engine.kill_switch_active()}

    # ── demo scenarios (named per-client failure presets, TTL + auto-expiry)
    # None of CS_DEMO_* / CS_GET_DEMO_* match the NOT_IMPLEMENTED matcher's
    # second-segment set {QUEUE,GET,CLEAR,...} except the GET pair ("GET" is
    # in the set), so all four sit here before the matcher.
    async def _cmd_demo_scenario(self, d):
        """Apply a named demo failure scenario to one client."""
        # Read and clean the target client and scenario name.
        hostname = str(d.get("hostname") or "").strip()
        scenario = str(d.get("scenario") or "").strip()
        # Both values are required.
        if not hostname or not scenario:
            return {"status": "ERROR", "message": "missing 'hostname' or 'scenario'"}
        # Apply the scenario; the demo manager raises ValueError for bad names or hosts.
        try:
            summ = await self.demo.apply(hostname, scenario,
                                         str(d.get("triggered_by") or ""))
        # Turn a validation problem into an error reply instead of a crash.
        except ValueError as exc:
            return {"status": "ERROR", "message": str(exc)}
        # Return the scenario summary in a success reply.
        return {"status": "SUCCESS", **summ}

    async def _cmd_demo_clear(self, d):
        """Clear any active demo scenario from one client."""
        # Read and clean the target client name; it is required.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Remove any active scenario and report whether one was cleared.
        cleared = await self.demo.clear(hostname)
        return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}

    async def _cmd_get_demo_active(self, d):
        """List the demo scenarios currently active on clients."""
        # Return a summary of all currently active demo scenarios.
        return {"status": "SUCCESS",
                "active": await self.demo.active_summary()}

    async def _cmd_get_demo_scenarios(self, d):
        """Return every available demo scenario definition."""
        # Return the static scenario catalogue.
        return {"status": "SUCCESS", "scenarios": DEMO_SCENARIOS}
