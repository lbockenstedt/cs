"""Tenant simulation/user override config routes for the aggregate router package."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# APIRouter collects this module's endpoints.
from fastapi import APIRouter
# Shared helpers, models, imports (auth, store, httpx, base64, datetime...) and state.
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

# Sub-router merged into the aggregate router.
router = APIRouter()



@router.get("/{tenant_id}/config/simulation-conf")
async def get_simulation_conf(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return simulation.conf for a tenant: from GitHub when a token is set, else the hub override.
    """
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Read the tenant's GitHub repo settings to decide between GitHub mode and hub override mode.
    cfg = _github_repo_settings(tenant)
    # No GitHub token configured: use hub-managed content instead of the repository.
    if not cfg.get("github_token"):
        # No GitHub API key — serve hub-managed override content.
        # If no override has been saved yet, fall back to sim_conf_content
        # from the first online spoke that includes it in telemetry.
        content = tenant.sim_conf_override or ""
        if not content:
            # Look through approved spokes' telemetry for a reported copy of the file.
            for spoke in _approved_spokes(resolved_tenant_id):
                spoke_content = (spoke.telemetry or {}).get("sim_conf_content", "")
                if spoke_content:
                    content = spoke_content
                    break
        # Response in "override" mode (no git SHA/branch).
        return {
            "content": content,
            "sha": "",
            "branch": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "mode": "override",
        }
    # GitHub mode: fetch the file contents, blob SHA and branch from the repository.
    content, sha, branch = await _fetch_simulation_conf_from_github(tenant)
    # Response in "github" mode, timestamped so the UI can show when it was fetched.
    return {
        "content": content,
        "sha": sha,
        "branch": branch,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "mode": "github",
    }


@router.put("/{tenant_id}/config/simulation-conf")
async def save_simulation_conf(
    tenant_id: str,
    payload: SimulationConfUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save simulation.conf: commit it to GitHub when a token is set, else store as hub override."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Read the tenant's GitHub repo settings to decide between GitHub mode and hub override mode.
    cfg = _github_repo_settings(tenant)
    # No GitHub token: store the content on the tenant and push it to spokes directly.
    if not cfg.get("github_token"):
        # No GitHub API key — save as hub-managed override pushed to all spokes.
        tenant.sim_conf_override = payload.content
        # Persist the tenant change.
        store.save_tenant(tenant)
        # Send the updated override files to every approved spoke; returns how many were queued.
        pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
        return {"ok": True, "pushed_to_spokes": pushed, "mode": "override"}
    # GitHub mode: get the token/repo/branch (errors if incomplete) and the file's current SHA,
    # which GitHub requires to update an existing file.
    github_token, owner, repo, branch = _require_sim_repo_config(tenant)
    _, sha, _ = await _fetch_simulation_conf_from_github(tenant)
    # GitHub "contents" API endpoint for the file.
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/configs/simulation.conf"
    # Commit request: message, base64-encoded new content, the SHA being replaced and the branch.
    body = {
        "message": f"Update configs/simulation.conf via hub for tenant {resolved_tenant_id}",
        "content": base64.b64encode(payload.content.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": branch,
    }
    # Send the update to GitHub.
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(url, headers=_github_api_headers(github_token), json=body)
    # Pass GitHub errors (e.g. 409 SHA conflict) straight back to the caller.
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_github_error_detail(response))
    # Extract the new commit SHA from GitHub's reply.
    response_payload = response.json()
    commit_sha = str((response_payload.get("commit") or {}).get("sha") or "")
    # Ask every approved spoke to pull the repo so the change takes effect.
    synced_spokes = _queue_repo_sync_for_all_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "commit_sha": commit_sha, "synced_spokes": synced_spokes, "mode": "github"}


@router.get("/{tenant_id}/config/sim-conf-override")
def get_sim_conf_override(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return the hub-managed simulation.conf override (INI text), or null if unset."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # "active" tells the UI whether an override exists (None means unset).
    return {"content": tenant.sim_conf_override, "active": tenant.sim_conf_override is not None}


@router.put("/{tenant_id}/config/sim-conf-override")
def save_sim_conf_override(
    tenant_id: str,
    payload: ConfOverrideRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save hub-managed simulation.conf override and push to all approved spokes."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Store the new override content.
    tenant.sim_conf_override = payload.content
    # Persist the tenant change.
    store.save_tenant(tenant)
    # Send the updated override files to every approved spoke; returns how many were queued.
    pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "pushed_to_spokes": pushed}


@router.delete("/{tenant_id}/config/sim-conf-override")
def clear_sim_conf_override(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Clear hub-managed simulation.conf override — spokes revert to GitHub file."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # None removes the override so spokes use the repository file again.
    tenant.sim_conf_override = None
    # Persist the tenant change.
    store.save_tenant(tenant)
    # Send the updated override files to every approved spoke; returns how many were queued.
    pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "cleared": True, "pushed_to_spokes": pushed}


@router.get("/{tenant_id}/config/user-conf-override")
def get_user_conf_override(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return the hub-managed user-overrides.conf override (INI text), or null if unset."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # "active" tells the UI whether an override exists.
    return {"content": tenant.user_conf_override, "active": tenant.user_conf_override is not None}


@router.put("/{tenant_id}/config/user-conf-override")
def save_user_conf_override(
    tenant_id: str,
    payload: ConfOverrideRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save hub-managed user-overrides.conf override and push to all approved spokes."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Store the new override content.
    tenant.user_conf_override = payload.content
    # Persist the tenant change.
    store.save_tenant(tenant)
    # Send the updated override files to every approved spoke; returns how many were queued.
    pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "pushed_to_spokes": pushed}


@router.delete("/{tenant_id}/config/user-conf-override")
def clear_user_conf_override(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Clear hub-managed user-overrides.conf override — spokes revert to GitHub file."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # None removes the override so spokes use the repository file again.
    tenant.user_conf_override = None
    # Persist the tenant change.
    store.save_tenant(tenant)
    # Send the updated override files to every approved spoke; returns how many were queued.
    pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "cleared": True, "pushed_to_spokes": pushed}


# ── User-overrides.conf editor endpoints ─────────────────────────────────────
# These are always in "override" mode (no GitHub path needed).

@router.get("/{tenant_id}/config/user-overrides-conf")
async def get_user_overrides_conf(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return user-overrides.conf content.
    If a GitHub token is configured, fetches configs/user-overrides.conf from the repo.
    Otherwise serves the hub-managed override (tenant.user_conf_override)."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Read the tenant's GitHub repo settings to decide between GitHub mode and hub override mode.
    cfg = _github_repo_settings(tenant)
    # No GitHub token configured: hub-managed content only.
    if not cfg.get("github_token"):
        # No GitHub API key — serve hub-managed override content only.
        # Fall back to user_overrides_conf_content from spoke telemetry if no override set.
        content = tenant.user_conf_override or ""
        if not content:
            # Look through approved spokes' telemetry for a reported copy of the file.
            for spoke in _approved_spokes(resolved_tenant_id):
                spoke_content = (spoke.telemetry or {}).get("user_overrides_conf_content", "")
                if spoke_content:
                    content = spoke_content
                    break
        # Response in "override" mode.
        return {
            "content": content,
            "sha": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "mode": "override",
        }
    # GitHub token available — read the file from the repo.
    # If the hub-managed override is set (non-empty), it takes precedence (admin explicitly set it).
    if tenant.user_conf_override:
        return {
            "content": tenant.user_conf_override,
            "sha": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "mode": "override",
        }
    # Fetch the file from GitHub.
    content, sha, branch = await _fetch_user_overrides_conf_from_github(tenant)
    # If GitHub returned nothing, fall back to spoke telemetry so users aren't left with an empty view.
    if not content:
        # Look through approved spokes' telemetry for a reported copy of the file.
        for spoke in _approved_spokes(resolved_tenant_id):
            spoke_content = (spoke.telemetry or {}).get("user_overrides_conf_content", "")
            if spoke_content:
                content = spoke_content
                sha = ""
                break
    # Response in "github" mode.
    return {
        "content": content,
        "sha": sha,
        "branch": branch,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "mode": "github",
    }


@router.put("/{tenant_id}/config/user-overrides-conf")
def save_user_overrides_conf(
    tenant_id: str,
    payload: ConfOverrideRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save hub-managed user-overrides.conf and push to all approved spokes.
    Saving empty content clears the hub-managed override so GitHub is used instead."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Empty content → clear the override so the GitHub file is served again.
    tenant.user_conf_override = payload.content if payload.content and payload.content.strip() else None
    # Persist the tenant change.
    store.save_tenant(tenant)
    # Send the updated override files to every approved spoke; returns how many were queued.
    pushed = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    return {"ok": True, "pushed_to_spokes": pushed, "mode": "override"}
