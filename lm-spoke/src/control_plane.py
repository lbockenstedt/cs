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
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("CSControlPlane")

class CSControlPlane(BaseControlPlane):
    def get_service_name(self) -> str:
        return "lm-cs"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "simulation"
        self.startup_config = config or {}

    async def run(self):
        logger.info(f"Starting CS (Client Simulator) -> {self.hub_url}")
        cs_spoke = CSSpoke(self.spoke_id, self.startup_config)
        self.register_module("cs", cs_spoke)
        await super().run()
    def run_standalone_mode(self):
        """Standalone FastAPI server for local management / testing.

        Phase 1: minimal surface (status / trigger / config / version) driven by
        the same CSSpoke the hub mode uses. Phase 2 will mount the full
        webui_spoke_api router here.
        """
        logger.info("Starting CS Module in STANDALONE MODE on port 8000")
        spoke = CSSpoke(self.spoke_id, self.startup_config)
        app = FastAPI(title="LM CS Spoke (standalone)")

        @app.get("/status")
        async def get_status():
            return spoke.engine.get_current_state()

        @app.post("/simulate/trigger")
        async def trigger_sim():
            return await spoke.engine.run_iteration()

        @app.post("/config")
        async def update_config(config: Dict[str, Any]):
            spoke.engine.update_config(config or {})
            return {"status": "success"}

        @app.get("/config")
        async def get_config():
            return {"status": "success", "state": spoke.engine.get_current_state()}

        @app.get("/version")
        async def get_version():
            return {"version": spoke.get_version()}

        uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Lab Manager Generic Agent")
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID", "cs-spoke-1"))
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", nargs='?', default=os.getenv("HUB_SECRET", ""), const="")
    parser.add_argument("--hub",        default=os.getenv("HUB_URL", "ws://localhost:8765"))
    args = parser.parse_args()
    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    if args.hub:
        asyncio.run(cp.run())
    else:
        cp.run_standalone_mode()
