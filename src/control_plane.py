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

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.engine = SimulationEngine(hostname=spoke_id)
        self.modules: Dict[str, Any] = {}

    def register_module(self, name: str, module_instance: Any):
        self.modules[name] = module_instance
        logger.info(f"Registered module: {name}")

    async def run(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting CS Module in HUB MODE -> {self.hub_url}")

        # Create and register the CS module
        cs_spoke = CSSpoke(self.spoke_id, {"sim_profiles": {}})
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", nargs='?', const="lm-secret", default="lm-secret", help="Authentication secret (default: lm-secret)")
    parser.add_argument("--hub-secret", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", help="Hub WebSocket URL (defaults to standalone mode if omitted)")
    args = parser.parse_args()

    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    if args.hub:
        asyncio.run(cp.run())
    else:
        cp.run_standalone_mode()
