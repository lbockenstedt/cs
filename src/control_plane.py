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

from .simulation_engine import SimulationEngine
from .cs_spoke import CSSpoke

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CSControlPlane")

class CSControlPlane:
    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_secret = hub_secret
        self.hub_url = hub_url
        self.engine = SimulationEngine(hostname=spoke_id)
        self.modules: Dict[str, Any] = {}

    def register_module(self, name: str, module_instance: Any):
        self.modules[name] = module_instance
        logger.info(f"Registered module: {name}")

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting CS Module in HUB MODE -> {self.hub_url}")

        # Create and register the CS module
        cs_spoke = CSSpoke(self.spoke_id, {"sim_profiles": {}})
        self.register_module("cs", cs_spoke)

        async with websockets.connect(self.hub_url) as websocket:
            # 1. Spoke Authentication Handshake
            await websocket.send(json.dumps({"spoke_id": self.spoke_id, "secret": self.secret}))
            logger.info(f"Connected to Lab Manager Hub as {self.spoke_id}. Performing mutual authentication...")

            # 2. Hub Mutual Authentication
            try:
                hub_proof_json = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                hub_proof = json.loads(hub_proof_json)

                if hub_proof.get("status") == "HUB_VERIFIED":
                    challenge = hub_proof.get("challenge")
                    signature = hub_proof.get("signature")

                    if self.hub_secret:
                        expected_sig = hmac.new(
                            self.hub_secret.encode(),
                            challenge.encode(),
                            hashlib.sha256
                        ).hexdigest()

                        if hmac.compare_digest(expected_sig, signature):
                            logger.info("Hub identity verified successfully.")
                            await websocket.send(json.dumps({"status": "HUB_OK"}))
                        else:
                            logger.error("Hub identity verification failed.")
                            await websocket.close(1008, "Hub verification failed")
                            return
                    else:
                        logger.warning("Hub secret not configured. Skipping verification.")
                        await websocket.send(json.dumps({"status": "HUB_OK"}))
                else:
                    await websocket.close(1008, "Mutual authentication failed")
                    return
            except Exception as e:
                logger.error(f"Hub verification failed: {e}")
                await websocket.close(1008, "Mutual authentication timed out")
                return

            async def heartbeat():
                while True:
                    msg = {
                        "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                                   "sender_id": self.spoke_id, "destination_id": "hub"},
                        "payload": {"type": "HEARTBEAT", "data": {}}
                    }
                    msg["signature"] = self._sign(msg)
                    await websocket.send(json.dumps(msg))
                    await asyncio.sleep(30)

            asyncio.create_task(heartbeat())

            async for message in websocket:
                msg = json.loads(message)
                if not self._verify_signature(msg):
                    continue

                payload = msg.get("payload", {})
                cmd_type = payload.get("type")
                data = payload.get("data", {})
                corr_id = msg.get("header", {}).get("message_id")

                # Multi-module routing
                result = None
                for module_name, module in self.modules.items():
                    if cmd_type.startswith(module_name) or True: # Simplify: let module try
                        result = await module.handle_command(cmd_type, data)
                        if result is not None: break

                if result is None and self.modules:
                    result = await list(self.modules.values())[0].handle_command(cmd_type, data)

                resp = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.spoke_id, "destination_id": "hub",
                               "correlation_id": corr_id},
                    "payload": {"type": "COMMAND_RESULT", "data": result}
                }
                resp["signature"] = self._sign(resp)
                await websocket.send(json.dumps(resp))

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

    def _sign(self, msg):
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _verify_signature(self, msg):
        sig = msg.get("signature")
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True).encode()
        expected = hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", required=True, help="Authentication secret")
    parser.add_argument("--hub-secret", help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", help="Hub WebSocket URL (defaults to standalone mode if omitted)")
    args = parser.parse_args()

    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    if args.hub:
        asyncio.run(cp.run_hub_mode())
    else:
        cp.run_standalone_mode()
