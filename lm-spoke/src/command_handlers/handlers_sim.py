"""Sim command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_sim`` scans only its own command group and
returns the result dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
from demo_scenarios import DEMO_SCENARIOS
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


class SimCommandsMixin:
    async def _dispatch_sim(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:

        # ── simulation execution ────────────────────────────────────────────
        if cmd in ("CS_TRIGGER_ITERATION", "TRIGGER_ITERATION"):
            result = await self.engine.run_iteration()
            return {"status": "SUCCESS", **result}

        if cmd in ("CS_GET_SIMULATION_STATE", "GET_SIMULATION_STATE"):
            return {"status": "SUCCESS", **self.engine.get_current_state()}

        if cmd in ("CS_SET_SIMULATION_PROFILE", "SET_SIMULATION_PROFILE"):
            self.engine.update_config(d.get("profile", {}))
            return {"status": "SUCCESS",
                    "message": f"Profile patched for {self.engine.hostname}"}

        # ── kill switch ────────────────────────────────────────────────────
        if cmd in ("CS_KILL_SWITCH",):
            on = bool(d.get("on", d.get("kill_switch", False)))
            self.engine.set_kill_switch(on)
            return {"status": "SUCCESS", "kill_switch": on}

        if cmd in ("CS_GET_KILL_SWITCH",):
            # Read for the hub's kill-switch banner. Sits BEFORE the
            # NOT_IMPLEMENTED matcher (whose set includes "GET") so the hub's
            # GET /kill-switch doesn't hit a dead command.
            return {"status": "SUCCESS",
                    "kill_switch": self.engine.kill_switch_active()}

        # ── demo scenarios (named per-client failure presets, TTL + auto-expiry)
        # None of CS_DEMO_* / CS_GET_DEMO_* match the NOT_IMPLEMENTED matcher's
        # second-segment set {QUEUE,GET,CLEAR,...} except the GET pair ("GET" is
        # in the set), so all four sit here before the matcher.
        if cmd in ("CS_DEMO_SCENARIO",):
            hostname = str(d.get("hostname") or "").strip()
            scenario = str(d.get("scenario") or "").strip()
            if not hostname or not scenario:
                return {"status": "ERROR", "message": "missing 'hostname' or 'scenario'"}
            try:
                summ = await self.demo.apply(hostname, scenario,
                                             str(d.get("triggered_by") or ""))
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            return {"status": "SUCCESS", **summ}

        if cmd in ("CS_DEMO_CLEAR",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            cleared = await self.demo.clear(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}

        if cmd in ("CS_GET_DEMO_ACTIVE",):
            return {"status": "SUCCESS",
                    "active": await self.demo.active_summary()}

        if cmd in ("CS_GET_DEMO_SCENARIOS",):
            return {"status": "SUCCESS", "scenarios": DEMO_SCENARIOS}
        return None
