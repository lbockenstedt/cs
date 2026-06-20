import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Dict, Any, Optional

from core.src.base_spoke import BaseSpoke
from simulation_engine import SimulationEngine
from agent_role import AgentRole

logger = logging.getLogger("CSSpoke")

# Roles bundled in the agents/ directory (relative to the repo root)
_AGENTS_DIR = Path(__file__).parent.parent / "agents"

# Registry: role_name → (module_path, class_name)
_BUILTIN_ROLES = {
    "linux_monitor": (_AGENTS_DIR / "linux" / "role.py", "LinuxMonitorRole"),
    "dns":           (_AGENTS_DIR / "dns"   / "role.py", "DnsRole"),
    "dhcp":          (_AGENTS_DIR / "dhcp"  / "role.py", "DhcpRole"),
    # proxmox role planned — see roadmap Phase 7c
}


def _load_role_class(role_name: str) -> Optional[type]:
    """Dynamically imports a role class from agents/<role>/role.py."""
    if role_name not in _BUILTIN_ROLES:
        return None
    path, cls_name = _BUILTIN_ROLES[role_name]
    if not path.exists():
        logger.error(f"Role file not found: {path}")
        return None
    spec = importlib.util.spec_from_file_location(f"lm_agent_role_{role_name}", path)
    mod = importlib.util.module_from_spec(spec)
    # Make src/ importable so role files can do: from agent_role import AgentRole
    src_dir = str(Path(__file__).parent)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        spec.loader.exec_module(mod)
        return getattr(mod, cls_name)
    except Exception as e:
        logger.error(f"Failed to load role '{role_name}': {e}")
        return None


class CSSpoke(BaseSpoke):
    """
    Generic / morphable LM spoke.

    Ships as a client-simulator (backward compat) but supports dynamic role
    loading so it can become a linux_monitor, dns, dhcp, or proxmox agent
    without redeployment.  The hub pushes LOAD_ROLE to change the persona.
    """

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        super().__init__(spoke_id, config)
        self.engine = SimulationEngine(
            hostname=spoke_id,
            config_data=config.get("sim_profiles", {}),
        )
        self._active_role: Optional[AgentRole] = None
        # Auto-load role from config if specified at startup
        startup_role = config.get("role")
        if startup_role:
            self._sync_load_role(startup_role, config.get("role_config", {}))

    # ── Role management ───────────────────────────────────────────────────────

    def _sync_load_role(self, role_name: str, role_config: dict) -> bool:
        cls = _load_role_class(role_name)
        if cls is None:
            logger.warning(f"Role '{role_name}' not found — staying in simulator mode")
            return False
        self._active_role = cls(self.spoke_id, role_config)
        logger.info(f"Loaded role: {role_name}")
        return True

    async def _load_role(self, role_name: str, role_config: dict) -> Dict[str, Any]:
        cls = _load_role_class(role_name)
        if cls is None:
            available = list(_BUILTIN_ROLES.keys())
            return {"status": "ERROR",
                    "message": f"Unknown role '{role_name}'",
                    "available": available}
        if self._active_role:
            await self._active_role.on_unload()
        self._active_role = cls(self.spoke_id, role_config)
        await self._active_role.on_load()
        logger.info(f"Role switched to: {role_name}")
        return {"status": "SUCCESS", "role": role_name,
                "message": f"Agent morphed to role '{role_name}'"}

    # ── Command dispatch ──────────────────────────────────────────────────────

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"Command: {command_type}")
        cmd = command_type.upper()

        # ── Generic agent management ──────────────────────────────────────────
        if cmd == "GET_VERSION":
            return {"status": "SUCCESS", "version": self.get_version()}

        if cmd == "GET_AGENT_STATUS":
            role_status = (await self._active_role.get_status()
                           if self._active_role else {"role": "simulator"})
            return {"status": "SUCCESS", "spoke_id": self.spoke_id,
                    "active_role": role_status}

        if cmd == "GET_AVAILABLE_ROLES":
            return {"status": "SUCCESS",
                    "roles": list(_BUILTIN_ROLES.keys()),
                    "active": self._active_role.role_name if self._active_role else "simulator"}

        if cmd == "LOAD_ROLE":
            role_name = data.get("role")
            if not role_name:
                return {"status": "ERROR", "message": "role is required"}
            return await self._load_role(role_name, data.get("config", {}))

        if cmd == "UNLOAD_ROLE":
            if self._active_role:
                await self._active_role.on_unload()
                old = self._active_role.role_name
                self._active_role = None
                return {"status": "SUCCESS", "message": f"Role '{old}' unloaded — back to simulator mode"}
            return {"status": "SUCCESS", "message": "No role was active"}

        if cmd == "UPDATE_CONFIG":
            self.config = data
            if "sim_profiles" in data:
                self.engine.update_config(data["sim_profiles"])
            if "role" in data:
                await self._load_role(data["role"], data.get("role_config", {}))
            return {"status": "SUCCESS", "message": "Configuration updated"}

        # ── Delegate to active role ───────────────────────────────────────────
        if self._active_role:
            return await self._active_role.handle_command(command_type, data)

        # ── Simulator built-ins (backward compat) ─────────────────────────────
        if cmd == "SET_SIMULATION_PROFILE":
            self.engine.update_config(data.get("profile", {}))
            return {"status": "SUCCESS",
                    "message": f"Profile updated for {self.engine.simulation_id}"}

        if cmd == "TRIGGER_ITERATION":
            return await self.engine.run_iteration()

        if cmd == "GET_SIMULATION_STATE":
            return self.engine.get_current_state()

        return {"status": "ERROR",
                "message": f"Command '{command_type}' not supported. "
                           f"Use LOAD_ROLE to activate a role or GET_AVAILABLE_ROLES."}

    async def get_status(self) -> Dict[str, Any]:
        if self._active_role:
            return await self._active_role.get_status()
        state = self.engine.get_current_state()
        return {
            "spoke_id": self.spoke_id,
            "module": "generic-agent",
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
