"""Updates API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
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

router = APIRouter()




@router.get("/api/test-github")
async def api_test_github() -> dict[str, Any]:
    """Validate the stored GitHub token against the GitHub API."""
    token = settings.get("github_token", "").strip()
    if not token:
        return {"valid": False, "error": "No GitHub token configured"}
    if httpx is None:
        return {"valid": False, "error": "httpx not available on server"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return {"valid": True, "username": data.get("login", ""), "error": None}
        elif resp.status_code == 401:
            return {"valid": False, "error": "Token is invalid or expired"}
        else:
            return {"valid": False, "error": f"GitHub returned HTTP {resp.status_code}"}
    except Exception as exc:
        return {"valid": False, "error": f"Request failed: {exc}"}




@router.get("/api/repo/status")
async def api_repo_status() -> dict[str, Any]:
    return {
        "synced": repo_state.get("synced", False),
        "error": repo_state.get("error"),
        "last_sync": repo_state.get("last_sync"),
        "repo_version": state._repo_ver,
    }




@router.post("/api/sync-now")
async def api_sync_now() -> dict[str, Any]:
    """Trigger an immediate GitHub sync outside the normal interval."""
    if "sync_repo" in background_tasks:
        background_tasks["sync_repo"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_tasks["sync_repo"]
    repo_state["synced"] = False
    repo_state["error"] = None
    background_tasks["sync_repo"] = asyncio.create_task(sync_repo())
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
    if state.update_all_state["running"]:
        raise HTTPException(status_code=409, detail="Update All already in progress")
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="WebUI update already in progress")
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
    await broadcast({"type": "update_all_progress", **state.update_all_state})
    asyncio.create_task(_run_update_all())
    return {"status": "ok", "message": "Update All started"}




@router.post("/api/self-update")
async def api_self_update() -> dict[str, Any]:
    """Manually trigger a self-update check and apply if a new version is available."""
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="Update already in progress")
    # Sync from GitHub first so version check reflects the latest repo state
    try:
        await _sync_repo_now()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub sync failed: {exc}") from exc
    # Now check version against freshly synced repo
    available = await asyncio.to_thread(_get_repo_version)
    import datetime
    update_state["available_version"] = available
    update_state["last_checked"] = datetime.datetime.now().isoformat(timespec="seconds")
    update_state["update_available"] = (
        available is not None and available != update_state["current_version"]
    )
    await _broadcast_update_state()
    if not update_state["update_available"]:
        return {"status": "ok", "message": f"Already up to date (v{update_state['current_version']})"}
    asyncio.create_task(_run_self_update())
    return {"status": "ok", "message": f"Update to v{available} started — service will restart shortly"}
