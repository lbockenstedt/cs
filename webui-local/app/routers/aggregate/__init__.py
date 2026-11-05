"""Aggregate router package: composes concern sub-routers into one APIRouter.

Public import surface preserved for external importers (app.main, tasks.py,
routers.spokes): ``router`` plus the central-browse cache helpers.
"""
from __future__ import annotations

# APIRouter is used to build the single combined router this package exports.
from fastapi import APIRouter

# Import the shared module itself so it is loaded (and its state initialised) up front.
from . import _common  # noqa: F401
# Re-export every public helper from _common so older ``from .aggregate import x`` keeps working.
from ._common import *  # noqa: F401,F403 -- re-export shared helpers/state
# Underscore-prefixed names are not covered by ``import *``, so re-export these explicitly.
from ._common import (  # explicit re-export for external importers
    _refresh_central_browse,
    _central_browse_cache,
    _central_browse_cache_ts,
    _load_browse_disk_cache,
)
# Each concern-specific module defines its own router; import them under private names.
from .config_routes import router as _config_router
from .clients_routes import router as _clients_router
from .dashboard_routes import router as _dashboard_router
from .fleet_routes import router as _fleet_router
from .central_routes import router as _central_router
from .qa_routes import router as _qa_router

# The one router that app.main mounts; it is populated with every sub-router below.
router = APIRouter()
# Attach each sub-router so all aggregate endpoints are served under one router object.
for _sub in (
    _config_router,
    _clients_router,
    _dashboard_router,
    _fleet_router,
    _central_router,
    _qa_router,
):
    router.include_router(_sub)
