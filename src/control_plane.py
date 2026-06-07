import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import hashlib
import argparse
from fastapi import FastAPI, HTTPException
import uvicorn

from .simulation_engine import SimulationEngine
from .cs_spoke import CSSpoke

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CSControlPlane")

class CSControlPlane:
    def __init__(self, spoke_id: str, secret: str, hub_url: str = None):
        self.spoke_id = spoke_id
        self.secret = secret
        self.hub_url = hub_url
        # Use the same engine for both modes
        self.engine = SimulationEngine(hostname=spoke_id)

    async def run_hub_mode(self):
        """Native LM Spoke behavior."""
        logger.info(f"Starting CS Module in HUB MODE -> {self.hub_url}")

        # Create the CSSpoke instance for command handling logic
        cs_spoke = CSSpoke(self.spoke_id, {"sim_profiles": {}})

        async with websockets.connect(self.hub_url) as websocket:
            # Handshake
            await websocket.send(json.dumps({"spoke_id": self.spoke_id, "secret": self.secret}))
            logger.info(f"Connected to Lab Manager Hub as {self.spoke_id}")

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
                # Signature verification (simplified match to lm/spoke/src/main.py)
                if not self._verify_signature(msg):
                    continue

                payload = msg.get("payload", {})
                cmd_type = payload.get("type")
                data = payload.get("data", {})

                # Route to the CSSpoke logic
                result = await cs_spoke.handle_command(cmd_type, data)

                # Send Ack/Result back to Hub
                resp = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.spoke_id, "destination_id": "hub"},
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
    parser.add_argument("--hub", help="Hub WebSocket URL (defaults to standalone mode if omitted)")
    args = parser.parse_args()

    cp = CSControlPlane(args.id, args.secret, args.hub)
    if args.hub:
        asyncio.run(cp.run_hub_mode())
    else:
        cp.run_standalone_mode()
