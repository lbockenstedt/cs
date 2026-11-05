"""Per-domain CS_* command handler mixins for :class:`cs_spoke.CSSpoke`.

The spoke's ``handle_command`` dispatch skeleton stays in ``cs_spoke.py``; the
handler bodies were moved here verbatim, grouped by domain, each as a mixin the
spoke inherits. See each module for its command group.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Mixin with CS_* commands that manage agents.
from command_handlers.handlers_agents import AgentCommandsMixin
# Mixin with CS_* commands that manage simulated/registered clients.
from command_handlers.handlers_clients import ClientCommandsMixin
# Mixin with CS_* commands that read and change configuration.
from command_handlers.handlers_config import ConfigCommandsMixin
# Mixin with CS_* commands that ingest data pushed to the spoke.
from command_handlers.handlers_ingest import IngestCommandsMixin
# Mixin with CS_* commands that control the simulation engine.
from command_handlers.handlers_sim import SimCommandsMixin

# Public names exported by "from command_handlers import *".
__all__ = [
    "AgentCommandsMixin",
    "ClientCommandsMixin",
    "ConfigCommandsMixin",
    "IngestCommandsMixin",
    "SimCommandsMixin",
]
