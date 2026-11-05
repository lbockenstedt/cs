"""Base class for pluggable agent roles loaded by the LM spoke at runtime."""
# Type hints only; they document the shape of command payloads and replies.
from typing import Dict, Any


class AgentRole:
    """
    Base class for pluggable roles the generic agent can load at runtime.

    Subclass this in cs/agents/<role>/role.py, set role_name, and implement
    handle_command().  The CSSpoke will delegate all unknown commands to the
    active role after checking its own built-in command set.
    """

    # Unique role identifier; subclasses override it so the spoke can select them.
    role_name: str = "base"
    # Which kind of process loads this role (agent spokes by default).
    module_type: str = "agent"

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        """Store the owning spoke id and the role's configuration."""
        # Remember which spoke owns this role, used in logs and replies.
        self.spoke_id = spoke_id
        # Role-specific settings supplied by the spoke's configuration.
        self.config = config

    async def on_load(self):
        """Called once when the role is activated.  Override for setup."""

    async def on_unload(self):
        """Called once when the role is replaced or the agent shuts down."""

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a role-specific command; subclasses override this."""
        # Default behaviour: the base role handles no commands, so report an error.
        return {"status": "ERROR", "message": f"Command '{command_type}' not handled by role '{self.role_name}'"}

    async def get_status(self) -> Dict[str, Any]:
        """Return a basic status dict; subclasses can extend it."""
        # Minimal status: which role is loaded and that it is active.
        return {"role": self.role_name, "status": "ACTIVE"}
