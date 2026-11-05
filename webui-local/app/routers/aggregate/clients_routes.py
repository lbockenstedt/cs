"""Client demo-scenario, sim-override, and USB VID:PID routes for the aggregate router package."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# APIRouter collects this module's endpoints.
from fastapi import APIRouter
# Shared helpers, request models, imports (auth, store, httpx, re, base64...) and state.
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

# Sub-router merged into the aggregate router.
router = APIRouter()



@router.post("/{tenant_id}/spokes/{spoke_id}/clients/{hostname}/demo-scenario")
async def hub_demo_set_scenario(
    tenant_id: str,
    spoke_id: str,
    hostname: str,
    payload: DemoScenarioRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Trigger a named demo scenario on a specific client via spoke WebSocket relay.

    Accessible to demo, viewer, and admin roles.  The spoke applies the scenario
    in-memory — it reverts automatically after 120 minutes or on hub/spoke reboot.
    """
    # Demo, viewer and admin roles may all drive demo scenarios.
    _require_tenant_demo_or_above(tenant_id, current_user)
    # Relay a demo_scenario message to the spoke, naming the client, scenario and who triggered it.
    sent = await _relay_demo_command(tenant_id, spoke_id, {
        "type": "demo_scenario",
        "hostname": hostname,
        "scenario": payload.scenario,
        "triggered_by": current_user.username,
    })
    # The spoke's WebSocket relay is not connected, so the command could not be delivered (502).
    if not sent:
        raise HTTPException(status_code=502, detail="Spoke relay is offline")
    # Confirm what was applied.
    return {"ok": True, "hostname": hostname, "scenario": payload.scenario}


@router.delete("/{tenant_id}/spokes/{spoke_id}/clients/{hostname}/demo-scenario")
async def hub_demo_clear_scenario(
    tenant_id: str,
    spoke_id: str,
    hostname: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Clear the demo scenario for a specific client on a spoke."""
    # Demo, viewer and admin roles may all drive demo scenarios.
    _require_tenant_demo_or_above(tenant_id, current_user)
    # Relay a demo_clear message for this client to the spoke.
    sent = await _relay_demo_command(tenant_id, spoke_id, {
        "type": "demo_clear",
        "hostname": hostname,
    })
    # The spoke's WebSocket relay is not connected, so the command could not be delivered (502).
    if not sent:
        raise HTTPException(status_code=502, detail="Spoke relay is offline")
    return {"ok": True, "hostname": hostname, "cleared": True}


@router.get("/{tenant_id}/clients/sim-overrides")
def get_client_sim_overrides(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return all hub-managed permanent sim overrides for a tenant (hostname → [sim, ...])."""
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Return the hostname -> simulations map (empty if none).
    return {"client_sim_overrides": tenant.client_sim_overrides or {}}


@router.put("/{tenant_id}/clients/{hostname}/sim-override")
async def set_client_sim_override(
    tenant_id: str,
    hostname: str,
    payload: ClientSimOverrideRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Enable or disable a simulation permanently for a specific client.

    Updates both:
    - client_sim_overrides (hub store) for instant dashboard display
    - user_conf_override (hub-managed user-overrides.conf) so the spoke actually
      applies the flag to the client's config on next relay cycle
    Also attempts a best-effort push to GitHub if the tenant has it configured.
    """
    # Only tenant admins may use this endpoint; resolves the tenant id the caller is acting on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Load the tenant record (404 if it does not exist).
    tenant = _get_tenant(resolved_tenant_id)
    # Name of the simulation being switched on or off.
    sim = payload.simulation

    # ── 1. Update client_sim_overrides for instant dashboard display ──────────
    # Copy the override map and the client's current list so we can edit them safely.
    overrides: dict[str, list[str]] = dict(tenant.client_sim_overrides or {})
    current = list(overrides.get(hostname, []))
    # Enabling: add the simulation once; disabling: remove it.
    if payload.enabled:
        if sim not in current:
            current.append(sim)
    else:
        current = [s for s in current if s != sim]
    # Store the edited list back on the tenant.
    overrides[hostname] = current
    tenant.client_sim_overrides = overrides

    # ── 2. Persist the change ─────────────────────────────────────────────────
    # If GitHub is configured: GitHub is the source of truth. Write the change
    # there and leave user_conf_override alone (None = hub serves GitHub content).
    # If no GitHub: store in user_conf_override, which the hub pushes to spokes as
    # hub-user-overrides.conf and which is merged on top of the spoke's local file.
    # Assume GitHub was not updated until a push succeeds.
    github_pushed = False
    # Read GitHub settings to decide where the change is stored.
    cfg = _github_repo_settings(tenant)
    # GitHub configured: edit the repo's user-overrides.conf (best effort).
    if cfg.get("github_token"):
        try:
            # Fetch the current file and toggle the simulation flag for this host in the INI text.
            gh_content, gh_sha, gh_branch = await _fetch_user_overrides_conf_from_github(tenant)
            modified_gh = _modify_ini_content(gh_content, hostname, sim, payload.enabled)
            # Extract owner and repo name from the configured repository URL.
            m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", cfg.get("sim_repo_url", ""))
            if m:
                # Use the branch GitHub reported, else the configured one, else main.
                owner, repo = m.group(1), m.group(2)
                branch = gh_branch or cfg.get("sim_repo_branch", "main")
                # GitHub contents API endpoint for the file.
                url = f"https://api.github.com/repos/{owner}/{repo}/contents/configs/user-overrides.conf"
                # Commit request with a descriptive message and base64-encoded content.
                body: dict[str, Any] = {
                    "message": f"{'Enable' if payload.enabled else 'Disable'} {sim} for {hostname} via hub",
                    "content": base64.b64encode(modified_gh.encode("utf-8")).decode("ascii"),
                    "branch": branch,
                }
                # Include the existing file's SHA when updating (omitted when creating the file).
                if gh_sha:
                    body["sha"] = gh_sha
                # Send the update; any status below 400 counts as success.
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    resp = await hc.put(url, headers=_github_api_headers(cfg["github_token"]), json=body)
                github_pushed = resp.status_code < 400
        # Network/API failures are logged; the fallback below keeps the change.
        except Exception as exc:
            logger.warning("sim-override: GitHub push failed for %s/%s: %s", hostname, sim, exc)

        if not github_pushed:
            # GitHub failed — fall back to hub-managed override so the change isn't lost
            hub_content = _modify_ini_content(tenant.user_conf_override or "", hostname, sim, payload.enabled)
            tenant.user_conf_override = hub_content if hub_content.strip() else None
        # On success, leave user_conf_override as-is (None means "use GitHub")
    else:
        # No GitHub — hub override is the only store; pushed to spokes as hub-user-overrides.conf
        # Toggle the flag in the hub-managed override; an empty result clears the override.
        hub_content = _modify_ini_content(tenant.user_conf_override or "", hostname, sim, payload.enabled)
        tenant.user_conf_override = hub_content if hub_content.strip() else None

    # Save the tenant and push override files to approved spokes.
    store.save_tenant(tenant)
    pushed_spokes = _push_conf_overrides_to_spokes(resolved_tenant_id, current_user)
    # Report the new state and where it was written.
    return {
        "ok": True,
        "hostname": hostname,
        "simulation": sim,
        "enabled": payload.enabled,
        "active_overrides": current,
        "user_overrides_updated": True,
        "github_pushed": github_pushed,
        "pushed_to_spokes": pushed_spokes,
    }


@router.get("/{tenant_id}/usb-vidpids")
def get_tenant_usb_vidpids(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return the effective USB device list for a tenant (global + tenant-specific).

    Any user with access to the tenant can read this list.  Devices inherited from
    the global (superadmin) list are annotated with source='global'; devices
    added by the tenant admin have source='tenant'.
    """
    # Any tenant member may read; returns global + tenant devices with their source.
    auth.require_tenant_access(tenant_id, current_user)
    return {"usb_vidpids": store.get_effective_usb_vidpids(tenant_id)}


@router.post("/{tenant_id}/usb-vidpids")
def add_tenant_usb_vidpid(
    tenant_id: str,
    entry: UsbVidpidEntry,
    current_user: User = Depends(auth.get_current_user),
):
    """Add or update a device in the tenant-level certified USB list.

    Requires tenant admin role.  Global (superadmin) devices are already included
    in the effective list; adding the same vidpid here is a no-op.
    """
    # Only tenant admins (or superadmins) may change the certified USB list.
    _require_tenant_admin(tenant_id, current_user)

    # If the vidpid is already globally certified it is already effective for all spokes.
    # Set of device ids already certified globally by a superadmin.
    global_vidpids = {d.get("vidpid") for d in store.get_global_usb_vidpids()}
    if entry.vidpid in global_vidpids:
        return {
            "status": "already_global",
            "message": "Device is already globally certified; no tenant entry needed.",
        }

    # Current tenant-level list.
    current = store.get_tenant_usb_vidpids(tenant_id)
    # Replace existing entry for the same vidpid or append
    updated = [d for d in current if d.get("vidpid") != entry.vidpid]
    updated.append({"vidpid": entry.vidpid, "type": entry.type, "label": entry.label})
    # Persist the updated tenant list.
    store.set_tenant_usb_vidpids(tenant_id, updated)

    # Push updated USB config to all approved spokes.
    # USB cert changes always propagate regardless of hub_config_enabled.
    # Count of spokes that will receive the new USB list.
    pushed_count = 0
    # Only push if the tenant still exists.
    tenant = store.get_tenant(tenant_id)
    if tenant:
        # For every approved spoke: bump its config version, save it and queue a config_update
        # command
        # so it downloads the new USB list.
        for spoke in store.list_spokes(tenant_id):
            if spoke.status != "approved":
                continue
            spoke.config_version += 1
            store.save_spoke(spoke)
            store.ensure_config_update_command(tenant_id, spoke.id)
            pushed_count += 1

    # Report the save and how many spokes will receive it.
    return {"status": "saved", "pushed_to_spokes": pushed_count}


@router.delete("/{tenant_id}/usb-vidpids/{vidpid:path}")
def delete_tenant_usb_vidpid(
    tenant_id: str,
    vidpid: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Remove a device from the tenant-level certified USB list.

    Requires tenant admin role.  Globally certified devices cannot be removed
    here; use PUT /api/superadmin/global-usb-vidpids to manage those.
    """
    # Only tenant admins (or superadmins) may change the certified USB list.
    _require_tenant_admin(tenant_id, current_user)

    # Set of device ids already certified globally by a superadmin.
    global_vidpids = {d.get("vidpid") for d in store.get_global_usb_vidpids()}
    # Global devices must be managed through the superadmin endpoint.
    if vidpid in global_vidpids:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove a globally certified device via the tenant endpoint. "
                   "Use the superadmin global USB endpoint instead.",
        )

    # Remove the matching entry; if nothing was removed it was not in the list (404).
    current = store.get_tenant_usb_vidpids(tenant_id)
    updated = [d for d in current if d.get("vidpid") != vidpid]
    if len(updated) == len(current):
        raise HTTPException(status_code=404, detail="Device not found in tenant certified list")
    # Persist the shortened list.
    store.set_tenant_usb_vidpids(tenant_id, updated)

    # Push updated USB config to all approved spokes.
    # USB cert changes always propagate regardless of hub_config_enabled.
    # Count of spokes that will receive the new USB list.
    pushed_count = 0
    # Only push if the tenant still exists.
    tenant = store.get_tenant(tenant_id)
    if tenant:
        # For every approved spoke: bump its config version, save it and queue a config_update
        # command
        # so it downloads the new USB list.
        for spoke in store.list_spokes(tenant_id):
            if spoke.status != "approved":
                continue
            spoke.config_version += 1
            store.save_spoke(spoke)
            store.ensure_config_update_command(tenant_id, spoke.id)
            pushed_count += 1

    # Report the deletion and how many spokes will receive it.
    return {"status": "deleted", "pushed_to_spokes": pushed_count}


@router.post("/{tenant_id}/usb-vidpids/resync")
def resync_tenant_usb_vidpids(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Force-push the current USB certified list to all approved spokes in this tenant.

    No change is made to the list itself — the current effective list (global + tenant)
    is simply re-queued for delivery.  Useful when a spoke missed a previous push due
    to being offline, in isolation mode, or running outdated software.

    Requires tenant admin role.
    """
    # Only tenant admins (or superadmins) may change the certified USB list.
    _require_tenant_admin(tenant_id, current_user)
    # Count of spokes the list is re-queued for.
    pushed_count = 0
    # For every approved spoke: bump its config version, save it and queue a config_update command.
    for spoke in store.list_spokes(tenant_id):
        if spoke.status != "approved":
            continue
        spoke.config_version += 1
        store.save_spoke(spoke)
        store.ensure_config_update_command(tenant_id, spoke.id)
        pushed_count += 1
    # Report how many spokes were re-synced.
    return {"status": "resynced", "pushed_to_spokes": pushed_count}
