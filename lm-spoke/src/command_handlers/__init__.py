"""Per-domain CS_* command handler mixins for :class:`cs_spoke.CSSpoke`.

The spoke's ``handle_command`` dispatch skeleton stays in ``cs_spoke.py``; the
handler bodies were moved here verbatim, grouped by domain, each as a mixin the
spoke inherits. See each module for its command group.
"""

from __future__ import annotations

from command_handlers.handlers_agents import AgentCommandsMixin
from command_handlers.handlers_clients import ClientCommandsMixin
from command_handlers.handlers_config import ConfigCommandsMixin
from command_handlers.handlers_ingest import IngestCommandsMixin
from command_handlers.handlers_sim import SimCommandsMixin

__all__ = [
    "AgentCommandsMixin",
    "ClientCommandsMixin",
    "ConfigCommandsMixin",
    "IngestCommandsMixin",
    "SimCommandsMixin",
]
