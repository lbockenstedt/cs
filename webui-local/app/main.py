"""FastAPI application entry point for the hub web UI.

Creates the ``app`` object, runs start-up tasks (store, admin user, key check, spoke
upgrades, background loops), mounts static files and API routers, and serves the
single-page frontend for every other path.
"""
# Postpone annotation evaluation (allows modern hint syntax).
from __future__ import annotations

# asyncio starts background loops; contextlib suppresses cancellation errors on shutdown.
import asyncio
import contextlib
# Module logging for start-up and request diagnostics.
import logging
# asynccontextmanager turns lifespan() into FastAPI's start-up/shutdown hook.
from contextlib import asynccontextmanager
# Path builds filesystem locations for frontend assets and data files.
from pathlib import Path

# FastAPI app, HTTP errors, WebSocket type, HTML responses and static-file serving.
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# store = persistence layer; ensure_admin creates the first admin account.
from . import store
from .auth import ensure_admin
# Settings accessor, plus WebSocket helpers (event-loop registration and connection handler).
from .config import get_settings
from .ws import set_main_loop, ws_connect

# Module logger.
logger = logging.getLogger(__name__)
# webui-local folder (two levels above this file) and the frontend/shared asset folders in it.
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"
TEMPLATE_DIR = FRONTEND_DIR / "templates"
SHARED_DIR = BASE_DIR / "shared"


# Encrypted marker file used to detect a changed encryption key between restarts.
_SENTINEL_FILE = Path(get_settings().data_dir) / ".keycheck"
# Plain text the marker must decrypt to when the key is unchanged.
_SENTINEL_VALUE = "hub-key-ok"


def _run_key_health_check() -> None:
    """Verify the encryption key is consistent between restarts.

    On first run: encrypt a sentinel string and write it to .keycheck.
    On subsequent runs: decrypt the sentinel and compare to the expected value.
    If the sentinel cannot be decrypted (key changed or corrupted), log a
    CRITICAL warning but continue — the hub can still operate, existing
    encrypted configs will just fail to decrypt.
    """
    # Imported lazily so the key is only resolved when this check runs.
    from .crypto import decrypt_str, encrypt_str

    # Any filesystem problem (e.g. storage mount not ready) is caught by the outer handler.
    try:
        # Marker exists: try to decrypt it with the current key.
        if _SENTINEL_FILE.exists():
            ciphertext = _SENTINEL_FILE.read_text().strip()
            try:
                plaintext = decrypt_str(ciphertext)
                # Decrypted fine: check the content matches the expected value.
                if plaintext == _SENTINEL_VALUE:
                    logger.info("Key health check: WEBUI_SECRET_KEY verified ✓")
                else:
                    logger.critical(
                        "Key health check FAILED: decrypted sentinel mismatch — "
                        "WEBUI_SECRET_KEY may have changed. Existing encrypted configs will be unreadable."
                    )
            # Decryption failed, which almost always means the key changed.
            except Exception as exc:
                logger.critical(
                    "Key health check FAILED: cannot decrypt sentinel — "
                    "WEBUI_SECRET_KEY may have changed or be wrong: %s. "
                    "Existing encrypted configs will be unreadable.",
                    exc,
                )
        else:
            # First run — write the sentinel
            _SENTINEL_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SENTINEL_FILE.write_text(encrypt_str(_SENTINEL_VALUE))
            logger.info("Key health check: sentinel written (first run) ✓")
    # Never block start-up because of this diagnostic check.
    except Exception as exc:
        logger.warning("Key health check skipped (mount not ready?): %s", exc)


# VERSION file shipped with the build, and a data-dir file remembering the last version started.
_VERSION_FILE = BASE_DIR / "VERSION"
_LAST_HUB_VERSION_FILE = Path(get_settings().data_dir) / ".last_hub_version"


def _trigger_spoke_updates_on_hub_upgrade() -> None:
    """If the hub VERSION changed since last startup, queue spoke + proxmox
    agent self-updates for all approved spokes across all tenants."""
    # Current build version, or "unknown" when there is no VERSION file.
    current = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "unknown"
    # Read the previously started version (None on first run or if unreadable).
    try:
        _LAST_HUB_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        last = _LAST_HUB_VERSION_FILE.read_text().strip() if _LAST_HUB_VERSION_FILE.exists() else None
    # Unreadable file: behave as if this is the first run.
    except Exception:
        last = None

    # Same version as last start: nothing to do.
    if last == current:
        logger.info("Hub version unchanged (%s) — no automatic spoke updates queued", current)
        return

    logger.info(
        "Hub upgraded %s → %s — queuing spoke and agent updates for all tenants",
        last or "(first run)",
        current,
    )
    # Remember this version so the next restart does not re-queue updates.
    _LAST_HUB_VERSION_FILE.write_text(current)

    if last is None:
        # First run — record version but don't push updates; spokes are fresh
        return

    # Imported here because they are only needed when updates are actually queued.
    from .data_models import Command
    from datetime import timedelta, timezone
    from datetime import datetime
    import uuid

    # Walk every tenant's approved spokes and queue two update commands for each.
    now = datetime.now(timezone.utc)
    tenants = store.list_tenants()
    queued = 0
    for tenant in tenants:
        spokes = [s for s in store.list_spokes(tenant.id) if s.status == "approved"]
        for spoke in spokes:
            # Commands expire after 24 hours so offline spokes do not run stale updates much later.
            expires = now + timedelta(hours=24)
            # Update the Proxmox helper agent on the spoke's server.
            store.enqueue_command(Command(
                spoke_id=spoke.id,
                tenant_id=tenant.id,
                type="proxmox_agent_update",
                payload={},
                expires_at=expires,
            ))
            # Update the spoke software itself.
            store.enqueue_command(Command(
                spoke_id=spoke.id,
                tenant_id=tenant.id,
                type="self_update",
                payload={},
                expires_at=expires,
            ))
            queued += 2
    logger.info("Hub upgrade: queued %d update commands across %d tenants", queued, len(tenants))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run start-up work, launch background loops, then cancel them on shutdown."""
    # Load settings and initialise the JSON store before anything reads data.
    settings = get_settings()
    store.init_store()
    # Make sure an admin account exists, verify the encryption key and push upgrades if needed.
    ensure_admin()
    _run_key_health_check()
    _trigger_spoke_updates_on_hub_upgrade()
    # Remember the running event loop so thread code can schedule WebSocket broadcasts on it.
    set_main_loop(asyncio.get_running_loop())
    logger.info(f"Hub starting — data dir: {settings.data_dir}")

    # Imported here to avoid circular imports (tasks imports routers that import main pieces).
    from . import tasks

    # Start every periodic background job as an asyncio task.
    bg_tasks = [
        asyncio.create_task(tasks.gkill_switch_poller()),
        asyncio.create_task(tasks.heartbeat_monitor()),
        asyncio.create_task(tasks.auto_recovery_check()),
        asyncio.create_task(tasks.schedule_check()),
        asyncio.create_task(tasks.aruba_poller()),
        asyncio.create_task(tasks.hub_baseline_saver()),
        asyncio.create_task(tasks.check_state_engine()),
        asyncio.create_task(tasks.maintenance_loop()),
        asyncio.create_task(tasks.acme_renewal_check()),
        asyncio.create_task(tasks.central_browse_poller()),
        asyncio.create_task(tasks.loop_lag_monitor()),
    ]
    # yield hands control to FastAPI while the server runs.
    try:
        yield
    # On shutdown cancel each task, then wait for it to finish, ignoring the expected cancellation.
    finally:
        for task in bg_tasks:
            task.cancel()
        for task in bg_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


# The ASGI application object that the web server (uvicorn/gunicorn) loads.
app = FastAPI(title="Hub — Client-Sim Central Platform", lifespan=lifespan)


# Serve frontend static files and shared assets directly from disk.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/shared", StaticFiles(directory=str(SHARED_DIR)), name="shared")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Accept a browser WebSocket connection for live updates."""
    # Delegate authentication and message handling to the ws module.
    await ws_connect(websocket)


# Import routers after app is created; each module defines endpoints for one area.
from .routers import auth as auth_router
from .routers import aggregate, backups, checks, commands, console, qa as qa_router, settings as settings_router, sites, spokes, superadmin, webhook, workspaces
from .routers import t3 as t3_router

# Mount each router; most live under /api. Console and webhook define their own full paths.
app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(qa_router.router, prefix="/api", tags=["qa"])
app.include_router(console.router, tags=["console"])
app.include_router(spokes.router, prefix="/api", tags=["spokes"])
app.include_router(sites.router, prefix="/api", tags=["sites"])
app.include_router(superadmin.router, prefix="/api", tags=["superadmin"])
app.include_router(workspaces.router, prefix="/api", tags=["workspaces"])
app.include_router(checks.router, prefix="/api", tags=["checks"])
app.include_router(commands.router, prefix="/api", tags=["commands"])
app.include_router(settings_router.router, prefix="/api", tags=["settings"])
app.include_router(aggregate.router, prefix="/api", tags=["aggregate"])
app.include_router(webhook.router)
app.include_router(backups.router, prefix="/api", tags=["backups"])
app.include_router(t3_router.router, prefix="/api", tags=["t3"])


@app.get("/api/health")
def health():
    """Health probe: report status plus version, branch and commit SHA (no authentication)."""
    # Local imports keep this tiny endpoint self-contained.
    import os
    from pathlib import Path
    # Commit SHA and branch injected at build time (defaults for local dev).
    sha = os.getenv("APP_VERSION", "dev")
    branch = os.getenv("APP_BRANCH", "local")
    # Short 7-character SHA, as shown by git.
    short = sha[:7] if len(sha) > 7 else sha
    # Prefer the VERSION file content; fall back to the short SHA.
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    app_version = version_file.read_text().strip() if version_file.exists() else short
    return {"status": "ok", "version": app_version, "branch": branch, "sha": sha}


@app.get("/api/init")
# intentionally unauthenticated
def api_init():
    """Return app and installer version numbers for the login page (no authentication needed)."""
    # Version files live in the webui-local folder; default to "1.00" when missing.
    base = Path(__file__).resolve().parent.parent
    semver_file = base / "frontend" / "SEMVER"
    client_sim_file = base / "CLIENT_SIM_VERSION"
    app_version = semver_file.read_text().strip() if semver_file.exists() else "1.00"
    installer_version = client_sim_file.read_text().strip() if client_sim_file.exists() else "1.00"
    return {"mode": "hub", "app_version": app_version, "installer_version": installer_version}


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_fallback(full_path: str):
    """Serve index.html for any unmatched path so the frontend router can handle it.

    Adds the build version to asset URLs for cache-busting and fills template placeholders.
    """
    # The single-page app's HTML shell.
    index = TEMPLATE_DIR / "index.html"
    # Missing or unreadable index.html is handled by the except blocks below.
    try:
        html = index.read_text()
        # Build version used as a cache-busting query string (?v=...).
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        ver = version_file.read_text().strip() if version_file.exists() else "dev"
        # Append the version to the CSS/JS URLs so browsers fetch new files after each deploy.
        html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={ver}"')
        html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={ver}"')
        html = html.replace('src="/static/js/main.js"', f'src="/static/js/main.js?v={ver}"')
        # Fill in the placeholders the frontend reads to know its mode and version.
        html = html.replace("'{{WEBUI_MODE}}'", "'hub'")
        html = html.replace("'{{WEBUI_VERSION}}'", f"'{ver}'")
        return HTMLResponse(content=html)
    # No frontend build present: 503 tells operators the deployment is incomplete.
    except FileNotFoundError:
        logger.error("index.html not found at %s — frontend may not be built", index)
        raise HTTPException(status_code=503, detail="Frontend not available. Check hub deployment.")
    # Other read errors (permissions etc.) are reported as a 500.
    except OSError as exc:
        logger.error("Failed to read index.html: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read frontend.")
