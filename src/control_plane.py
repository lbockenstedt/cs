import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
import sys
import os
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, HTTPException
import uvicorn

from simulation_engine import SimulationEngine
from cs_spoke import CSSpoke
from core.src.messaging.control_plane import BaseControlPlane

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CSControlPlane")

class CSControlPlane(BaseControlPlane):
    def get_service_name(self) -> str:
        return "lm-cs"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "simulation"
        self.startup_config = config or {}
        self.engine = SimulationEngine(hostname=spoke_id)

    async def run(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting Generic Agent in HUB MODE -> {self.hub_url}")
        cs_spoke = CSSpoke(self.spoke_id, self.startup_config)
        self.register_module("cs", cs_spoke)
        await super().run()
    def run_standalone_mode(self):
        """Standalone FastAPI server for local management."""
        logger.info(f"Starting CS Module in STANDALONE MODE on port 8000")
        app = FastAPI()

        @app.get("/status")
        async def get_status():
            return self.engine.get_current_state()

        @app.post("/simulate/trigger")
        async def trigger_sim():
            return await self.engine.run_iteration()

        @app.post("/config")
        async def update_config(config: Dict[str, Any]):
            self.engine.update_config(config)
            return {"status": "success"}

        uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Lab Manager Generic Agent")
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID", "cs-spoke-1"))
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", default=os.getenv("HUB_SECRET", ""))
    parser.add_argument("--hub",        default=os.getenv("HUB_URL", "ws://localhost:8765"))
    parser.add_argument("--role",       default=os.getenv("STARTUP_ROLE", ""),
                        help="Pre-load a role at startup (linux_monitor, dns, dhcp, ...)")
    args = parser.parse_args()

    config = {}
    if args.role:
        config["role"] = args.role

    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub, config=config)
    if args.hub:
        asyncio.run(cp.run())
    else:
        cp.run_standalone_mode()
