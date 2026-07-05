"""Aggregate router package: composes concern sub-routers into one APIRouter.

Public import surface preserved for external importers (app.main, tasks.py,
routers.spokes): ``router`` plus the central-browse cache helpers.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import _common  # noqa: F401
from ._common import *  # noqa: F401,F403 -- re-export shared helpers/state
from ._common import (  # explicit re-export for external importers
    _refresh_central_browse,
    _central_browse_cache,
    _central_browse_cache_ts,
    _load_browse_disk_cache,
)
from .config_routes import router as _config_router
from .clients_routes import router as _clients_router
from .dashboard_routes import router as _dashboard_router
from .fleet_routes import router as _fleet_router
from .central_routes import router as _central_router
from .qa_routes import router as _qa_router

router = APIRouter()
for _sub in (
    _config_router,
    _clients_router,
    _dashboard_router,
    _fleet_router,
    _central_router,
    _qa_router,
):
    router.include_router(_sub)
