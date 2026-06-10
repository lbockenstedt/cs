import logging
from typing import Dict, Any
from lm.core.src.base_spoke import BaseSpoke
from .simulation_engine import SimulationEngine

logger = logging.getLogger("CSSpoke")

class CSSpoke(BaseSpoke):
    """
    Client Simulator Spoke implementation for Lab Manager.
    Integrates the CS simulation engine with LM's native messaging protocol.
    """
    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        # Initialize the simulation engine using the spoke_id as hostname for consistency
        self.engine = SimulationEngine(hostname=spoke_id, config_data=config.get("sim_profiles", {}))

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Handling CS Command: {command_type} with data {data}")

        if command_type == "UPDATE_CONFIG":
            # General configuration update from Hub
            logger.info(f"Updating CS configuration: {data}")
            self.config = data
            # Update simulation engine if profiles are present
            if "sim_profiles" in data:
                self.engine.update_config(data["sim_profiles"])
            return {"status": "SUCCESS", "message": "CS configuration updated from Hub"}

        elif command_type == "SET_SIMULATION_PROFILE":
            # Update the simulation engine's configuration
            new_profile = data.get("profile", {})
            self.engine.update_config(new_profile)
            return {"status": "SUCCESS", "message": f"Profile updated for {self.engine.simulation_id}"}

        elif command_type == "TRIGGER_ITERATION":
            # Force an immediate simulation run
            result = await self.engine.run_iteration()
            return result

        elif command_type == "GET_SIMULATION_STATE":
            return self.engine.get_current_state()

        else:
            logger.warning(f"Unknown CS command type: {command_type}")
            return {"status": "ERROR", "message": f"Command {command_type} not supported by CS module"}

    async def get_status(self) -> Dict[str, Any]:
        """
        Native LM status report including simulation health.
        """
        state = self.engine.get_current_state()
        return {
            "spoke_id": self.spoke_id,
            "module": "client-simulator",
            "simulation_id": state["simulation_id"],
            "active_sims": state["active_simulations"],
            "status": state["status"]
        }
