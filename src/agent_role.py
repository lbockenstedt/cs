from typing import Dict, Any


class AgentRole:
    """
    Base class for pluggable roles the generic agent can load at runtime.

    Subclass this in cs/agents/<role>/role.py, set role_name, and implement
    handle_command().  The CSSpoke will delegate all unknown commands to the
    active role after checking its own built-in command set.
    """

    role_name: str = "base"
    module_type: str = "agent"

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        self.spoke_id = spoke_id
        self.config = config

    async def on_load(self):
        """Called once when the role is activated.  Override for setup."""

    async def on_unload(self):
        """Called once when the role is replaced or the agent shuts down."""

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ERROR", "message": f"Command '{command_type}' not handled by role '{self.role_name}'"}

    async def get_status(self) -> Dict[str, Any]:
        return {"role": self.role_name, "status": "ACTIVE"}
