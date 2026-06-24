"""CSSpoke — the cs module's command surface (LM spoke + shared modules).

This is the entry point the LM hub (and, in Phase 2, the local mgmt API) drives.
``handle_command`` dispatches the ``CS_*`` command contract to the underlying
plain modules (``SimulationEngine`` for Phase 1; ``ClientRegistry`` /
``CommandQueue`` / ``ProxmoxDeploy`` added in Phases 2–3). Business logic lives in
those modules — ``handle_command`` is a thin dispatcher so the spoke is drivable
identically from an LM hub command or an HTTP client.

Phase 1 command subset (config + simulations + kill switch + loop control):
    GET_VERSION, CS_GET_VERSION
    CS_GET_STATUS  (falls back to get_status() for *_GET_STATUS)
    CS_GET_TELEMETRY, CS_GET_CLIENTS   (Phase 1: self-status only)
    CS_TRIGGER_ITERATION
    CS_GET_SIMULATION_STATE
    CS_SET_SIMULATION_PROFILE
    CS_GET_CONFIG, CS_UPDATE_CONFIG, CS_UPDATE_USER_OVERRIDES
    CS_KILL_SWITCH
    CS_START_SIMULATION, CS_STOP_SIMULATION
Legacy aliases: TRIGGER_ITERATION, SET_SIMULATION_PROFILE, GET_SIMULATION_STATE,
    UPDATE_CONFIG.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from simulation_engine import SimulationEngine
import sim_config

try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke  # type: ignore

logger = logging.getLogger("CSSpoke")


class CSSpoke(BaseSpoke):
    """Client simulator spoke. Owns the sim engine (+ registry/queue/deploy later)."""

    def __init__(self, spoke_id: str, config: Dict[str, Any] | None = None):
        super().__init__(spoke_id, config or {})
        # Resolve repo-relative dirs from this file so cwd doesn't matter.
        base = Path(__file__).resolve().parent.parent
        config_dir = base / "configs"
        data_dir = base / "data"
        self.engine = SimulationEngine(spoke_id, config_dir=config_dir, data_dir=data_dir)
        # Phase 2/3 modules land here (registry/queue/deploy); None until then.
        self.registry = None
        self.queue = None
        self.deploy = None

    # ── BaseSpoke: status (fallback for *_GET_STATUS) ───────────────────────
    async def get_status(self) -> Dict[str, Any]:
        state = self.engine.get_current_state()
        return {
            "spoke_id": self.spoke_id,
            "module": "simulation",
            "mode": "simulator",
            "simulation_id": state["simulation_id"],
            "active_sims": state["active_simulations"],
            "status": state["status"],
            "iteration": state["iteration"],
            "kill_switch": self.engine.kill_switch_active(),
        }

    def get_version(self) -> str:
        try:
            return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
        except Exception:  # noqa: BLE001
            return "unknown"

    # ── command dispatch ───────────────────────────────────────────────────
    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Command: %s", command_type)
        cmd = command_type.upper()
        d = data or {}

        # ── identity / status ──────────────────────────────────────────────
        if cmd in ("GET_VERSION", "CS_GET_VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}

        if cmd in ("CS_GET_STATUS", "CS_GET_TELEMETRY", "CS_GET_CLIENTS"):
            # Phase 1: no client registry yet — return the spoke's own sim state.
            return {"status": "SUCCESS", "self": self.engine.get_current_state(),
                    "clients": [], "kill_switch": self.engine.kill_switch_active()}

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

        if cmd in ("CS_START_SIMULATION",):
            iterations = int(d.get("iterations", 100))
            iter_sleep = float(d.get("iter_sleep", 5.0))
            self.engine.start(iterations=iterations, iter_sleep=iter_sleep)
            return {"status": "SUCCESS", "message": f"started {iterations}-iter loop"}

        if cmd in ("CS_STOP_SIMULATION",):
            self.engine.stop()
            return {"status": "SUCCESS", "message": "simulation stopped"}

        # ── config ─────────────────────────────────────────────────────────
        if cmd in ("CS_GET_CONFIG",):
            base = Path(__file__).resolve().parent.parent / "configs"
            return {"status": "SUCCESS", "mode": "local",
                    "simulation_conf": _read(base / "simulation.conf"),
                    "user_overrides": _read(base / "user-overrides.conf")}

        if cmd in ("CS_UPDATE_CONFIG", "UPDATE_CONFIG"):
            content = d.get("content")
            if content is None:
                return {"status": "ERROR", "message": "missing 'content'"}
            try:
                sim_config.validate_ini_text(content)
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            base = Path(__file__).resolve().parent.parent / "configs"
            (base / "simulation.conf").write_text(content, encoding="utf-8")
            self.engine.reload_config()
            return {"status": "SUCCESS", "message": "simulation.conf updated"}

        if cmd in ("CS_UPDATE_USER_OVERRIDES",):
            content = d.get("content")
            if content is None:
                return {"status": "ERROR", "message": "missing 'content'"}
            try:
                sim_config.validate_ini_text(content)
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            base = Path(__file__).resolve().parent.parent / "configs"
            (base / "user-overrides.conf").write_text(content, encoding="utf-8")
            self.engine.reload_config()
            return {"status": "SUCCESS", "message": "user-overrides.conf updated"}

        # ── kill switch ────────────────────────────────────────────────────
        if cmd in ("CS_KILL_SWITCH",):
            on = bool(d.get("on", d.get("kill_switch", False)))
            self.engine.set_kill_switch(on)
            return {"status": "SUCCESS", "kill_switch": on}

        # Phase 2/3 commands (queue/proxmox/clients) return NotImplemented until
        # those modules land, so the LM hub sees a clear "not yet" rather than a
        # silent error.
        if cmd.startswith("CS_") and cmd.split("_")[1] in {
            "QUEUE", "GET", "CLEAR", "DEPLOY", "RECLONE", "VM", "APPROVE",
            "REJECT", "UPDATE", "SELF",
        }:
            if cmd in ("CS_GET_PROXMOX_STATUS", "CS_GET_PROXMOX_LOGS"):
                return {"status": "SUCCESS", "reachable": False,
                        "message": "Proxmox deploy lands in Phase 3",
                        "vms": [], "log": []}
            return {"status": "NOT_IMPLEMENTED",
                    "message": f"{cmd} lands in a later phase", "command": cmd}

        return {"status": "ERROR", "message": f"Unknown command: {command_type}"}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""