import logging
import random
import zlib
from typing import Dict, Any, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("SimulationEngine")

class SimulationEngine:
    """
    Core logic for the Client Simulator.
    Handles bucket assignment and simulation profile management.
    Agnostic of transport (Standalone vs Hub).
    """
    def __init__(self, hostname: str, config_data: Dict[str, Any] = None):
        self.hostname = hostname
        self.config = config_data or {}
        self.username = hostname.split('-')[0] if '-' in hostname else hostname
        self.simulation_id = self._assign_bucket()
        self.active_simulations = []
        self.status = "IDLE"

    def _assign_bucket(self) -> str:
        # Deterministic bucket assignment (s0-s9) based on hostname
        # Matches original client-sim logic using CRC32
        bucket_idx = zlib.crc32(self.hostname.encode()) % 10
        return f"s{bucket_idx}"

    def update_config(self, new_config: Dict[str, Any]):
        logger.info(f"Updating simulation config for {self.simulation_id}")
        self.config.update(new_config)

    async def run_iteration(self):
        """
        Simulates one iteration of the network behavior defined in config.
        In a real scenario, this would trigger shell scripts or API calls to guests.
        """
        profile = self.config.get(self.simulation_id, {})
        if not profile:
            logger.warning(f"No simulation profile found for bucket {self.simulation_id}")
            return {"status": "ERROR", "message": "No profile"}

        # Simulation logic mapping (simplified version of simulation.sh)
        results = []
        sims_to_run = [k for k, v in profile.items() if v == "on"]

        for sim in sims_to_run:
            logger.info(f"Executing simulation: {sim}")
            # Simulate some activity
            await asyncio.sleep(random.uniform(0.1, 0.5))
            results.append(sim)

        self.active_simulations = results
        return {
            "hostname": self.hostname,
            "bucket": self.simulation_id,
            "active_sims": results,
            "status": "SUCCESS"
        }

    def get_current_state(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "simulation_id": self.simulation_id,
            "config": self.config,
            "active_simulations": self.active_simulations,
            "status": self.status
        }

import asyncio # Needed for run_iteration
