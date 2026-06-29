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

# Sibling modules (simulation_engine, cs_spoke, sim_config, sim_primitives,
# data_models) live next to this file. When launched as `python -m
# src.control_plane` the module's own directory is NOT added to sys.path
# (only the cwd / PYTHONPATH entries are), so the bare sibling imports below
# raise ModuleNotFoundError and the spoke crash-loops. Put this file's
# directory on the front of sys.path so every sibling import resolves
# regardless of how the process is started (-m, direct script, or import).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
                 hub_url: str = None, config: Dict[str, Any] = None,
                 onboarding_psk: str = None, tenant_id_hint: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url,
                         onboarding_psk=onboarding_psk, tenant_id_hint=tenant_id_hint)
        self.module_type = "simulation"
        self.startup_config = config or {}

    async def run(self):
        logger.info(f"Starting CS (Client Simulator) -> {self.hub_url}")
        cs_spoke = CSSpoke(self.spoke_id, self.startup_config)
        self.register_module("cs", cs_spoke)
        await super().run()

    def _create_spoke_tasks(self, websocket):
        """Attach the CS telemetry relay loop.

        Every CS_TELEMETRY_INTERVAL_S (default 10s) the cs spoke re-emits its
        per-host Proxmox state (built by ``CSSpoke.deploy.relay_payload``) to the
        hub as a signed ``CS_TELEMETRY`` frame. The hub caches it in
        ``simulations_cache[spoke_id]`` (main.py) and the Simulations/VM Server
        view serves from that cache. Returned tasks are cancelled/awaited by the
        base class when the hub connection closes.
        """
        return [asyncio.create_task(self._cs_telemetry_relay_loop(websocket))]

    async def _cs_telemetry_relay_loop(self, websocket) -> None:
        interval = 10
        try:
            interval = max(2, int(os.environ.get("CS_TELEMETRY_INTERVAL_S", "10")))
        except Exception:
            pass
        # Stagger the first send so a freshly-ingested frame is more likely.
        await asyncio.sleep(2)
        while True:
            try:
                cs_mod = self.modules.get("cs")
                deploy = getattr(cs_mod, "deploy", None) if cs_mod else None
                if deploy is None:
                    await asyncio.sleep(interval)
                    continue
                payload = deploy.relay_payload(self.spoke_id, self.spoke_id)
                msg = {
                    "header": {
                        "message_id": str(uuid.uuid4()),
                        "timestamp": time.time(),
                        "sender_id": self.spoke_id,
                        "destination_id": "hub",
                    },
                    "payload": {"type": "CS_TELEMETRY", "data": payload},
                }
                msg["signature"] = self._sign(msg)
                await websocket.send(json.dumps(msg, separators=(",", ":")))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("CS telemetry relay send failed: %s", e)
            await asyncio.sleep(interval)

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
    # PSK self-provisioning (optional). Falls back to env LM_ONBOARDING_PSK /
    # LM_TENANT_ID_HINT when the flags are absent; a spoke without either
    # connects as before (pending admin approval).
    parser.add_argument("--onboarding-psk",  default=os.getenv("LM_ONBOARDING_PSK", ""))
    parser.add_argument("--tenant-id-hint",  default=os.getenv("LM_TENANT_ID_HINT", ""))
    args = parser.parse_args()
    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub,
                        onboarding_psk=args.onboarding_psk,
                        tenant_id_hint=args.tenant_id_hint)
    if args.hub:
        asyncio.run(cp.run())
    else:
        cp.run_standalone_mode()
