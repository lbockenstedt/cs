import logging
from pathlib import Path
from typing import Dict, Any

from core.src.base_spoke import BaseSpoke
from simulation_engine import SimulationEngine

logger = logging.getLogger("CSSpoke")


class CSSpoke(BaseSpoke):
    """Client simulator spoke. Drives synthetic client traffic profiles."""

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.engine = SimulationEngine(
            hostname=spoke_id,
            config_data=config.get("sim_profiles", {}),
        )

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Command: {command_type}")
        cmd = command_type.upper()

        if cmd == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if cmd == "CS_GET_STATUS":
            return {"status": "SUCCESS", **self.engine.get_current_state()}

        if cmd == "CS_START_SIMULATION":
            return await self.engine.run_iteration()

        if cmd == "CS_STOP_SIMULATION":
            return {"status": "SUCCESS", "message": "Simulation stopped"}

        if cmd == "CS_GET_TELEMETRY":
            return self.engine.get_current_state()

        if cmd == "SET_SIMULATION_PROFILE":
            self.engine.update_config(data.get("profile", {}))
            return {"status": "SUCCESS",
                    "message": f"Profile updated for {self.engine.simulation_id}"}

        if cmd == "TRIGGER_ITERATION":
            return await self.engine.run_iteration()

        if cmd == "GET_SIMULATION_STATE":
            return self.engine.get_current_state()

        if cmd == "UPDATE_CONFIG":
            self.config = data
            if "sim_profiles" in data:
                self.engine.update_config(data["sim_profiles"])
            return {"status": "SUCCESS", "message": "Configuration updated"}

        return {"status": "ERROR",
                "message": f"Unknown command: {command_type}"}

    async def get_status(self) -> Dict[str, Any]:
        state = self.engine.get_current_state()
        return {
            "spoke_id": self.spoke_id,
            "module": "simulation",
            "mode": "simulator",
            "simulation_id": state["simulation_id"],
            "active_sims": state["active_simulations"],
            "status": state["status"],
        }

    def get_version(self) -> str:
        try:
            return (Path(__file__).parent.parent / "VERSION").read_text().strip()
        except Exception:
            return "unknown"
