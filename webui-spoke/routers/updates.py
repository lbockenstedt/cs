"""Updates API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Update/sync helpers and shared state live in server.py; this file only defines routes.
from server import (
    APP_VERSION,
    Any,
    HTTPException,
    _broadcast_update_state,
    _get_repo_version,
    _run_self_update,
    _run_update_all,
    _sync_repo_now,
    approved_proxmox_agents,
    asyncio,
    background_tasks,
    broadcast,
    contextlib,
    httpx,
    repo_state,
    settings,
    state,
    sync_repo,
    time,
    update_state,
)

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/test-github")
async def api_test_github() -> dict[str, Any]:
    """Validate the stored GitHub token against the GitHub API."""
    # Need a token and httpx before we can test anything.
    token = settings.get("github_token", "").strip()
    if not token:
        return {"valid": False, "error": "No GitHub token configured"}
    if httpx is None:
        return {"valid": False, "error": "httpx not available on server"}
    # Ask GitHub who owns the token (10 s limit).
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            )
        # 200 means valid; return the account name.
        if resp.status_code == 200:
            data = resp.json()
            return {"valid": True, "username": data.get("login", ""), "error": None}
        elif resp.status_code == 401:
            # 401 means the token is bad or expired.
            return {"valid": False, "error": "Token is invalid or expired"}
        else:
            # Any other status is reported as-is.
            return {"valid": False, "error": f"GitHub returned HTTP {resp.status_code}"}
    except Exception as exc:
        # Network errors are reported, not raised.
        return {"valid": False, "error": f"Request failed: {exc}"}




@router.get("/api/repo/status")
async def api_repo_status() -> dict[str, Any]:
    """Return the repo sync status and the version found in the synced repo."""
    return {
        "synced": repo_state.get("synced", False),
        "error": repo_state.get("error"),
        "last_sync": repo_state.get("last_sync"),
        "repo_version": state._repo_ver,
    }




@router.post("/api/sync-now")
async def api_sync_now() -> dict[str, Any]:
    """Trigger an immediate GitHub sync outside the normal interval."""
    # Stop any sync already running and wait for it to finish cancelling.
    if "sync_repo" in background_tasks:
        background_tasks["sync_repo"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_tasks["sync_repo"]
    # Mark the repo as not synced and start a fresh sync task.
    repo_state["synced"] = False
    repo_state["error"] = None
    background_tasks["sync_repo"] = asyncio.create_task(sync_repo())
    # Tell UIs a sync is under way.
    await broadcast({"type": "repo_status", "synced": False, "error": None, "last_sync": repo_state["last_sync"]})
    return {"status": "ok", "message": "GitHub sync started"}




@router.get("/api/version")
async def api_version() -> dict[str, Any]:
    """Return installed and available installer versions."""
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "current_version": update_state["current_version"],
        "available_version": update_state["available_version"],
        "update_available": update_state["update_available"],
        "last_checked": update_state["last_checked"],
        "update_in_progress": update_state["update_in_progress"],
        "cswebui_current": update_state.get("cswebui_current") or APP_VERSION,
        "cswebui_available": update_state.get("cswebui_available"),
    }




@router.post("/api/update-all")
async def api_update_all() -> dict[str, Any]:
    """Queue the shared Proxmox update command, then self-update the WebUI."""
    # Refuse if an update-all or a WebUI update is already running.
    if state.update_all_state["running"]:
        raise HTTPException(status_code=409, detail="Update All already in progress")
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="WebUI update already in progress")
    # Set up progress: agents first if any are approved, otherwise go straight to the WebUI.
    has_approved_agents = bool(approved_proxmox_agents)
    state.update_all_state.update({
        "running": True,
        "phase": "agents" if has_approved_agents else "webui",
        "total_agents": 1 if has_approved_agents else 0,
        "completed_agents": 0,
        "failed_agents": 0,
        "agent_cmds": [],
        "started_at": time.time(),
        "error": None,
    })
    # Tell UIs, then run the update in the background.
    await broadcast({"type": "update_all_progress", **state.update_all_state})
    asyncio.create_task(_run_update_all())
    return {"status": "ok", "message": "Update All started"}




@router.post("/api/self-update")
async def api_self_update() -> dict[str, Any]:
    """Manually trigger a self-update check and apply if a new version is available."""
    # Only one update at a time.
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="Update already in progress")
    # Sync from GitHub first so version check reflects the latest repo state
    try:
        await _sync_repo_now()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub sync failed: {exc}") from exc
    # Now check version against freshly synced repo
    # Read the version in the synced repo (in a thread, as it touches disk).
    available = await asyncio.to_thread(_get_repo_version)
    # Imported here because only this endpoint needs it.
    import datetime
    # Record the result and whether it differs from the installed version.
    update_state["available_version"] = available
    update_state["last_checked"] = datetime.datetime.now().isoformat(timespec="seconds")
    update_state["update_available"] = (
        available is not None and available != update_state["current_version"]
    )
    # Tell UIs what was found.
    await _broadcast_update_state()
    # Nothing newer: done.
    if not update_state["update_available"]:
        return {"status": "ok", "message": f"Already up to date (v{update_state['current_version']})"}
    # Newer version: run the update in the background (the service restarts afterwards).
    asyncio.create_task(_run_self_update())
    return {"status": "ok", "message": f"Update to v{available} started — service will restart shortly"}
