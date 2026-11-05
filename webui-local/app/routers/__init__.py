"""HTTP route modules for the web UI API; each submodule exposes a FastAPI ``router``."""
# Import each router module so ``app.main`` can mount them via ``from .routers import ...``.
from . import auth, backups, checks, commands, sites, spokes, workspaces

# Explicit public names exported by ``from app.routers import *``.
__all__ = ["auth", "backups", "checks", "commands", "sites", "spokes", "workspaces"]
