"""Fleet reclone, proxmox agent, provisioning, and config-push routes for the aggregate router package."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# APIRouter collects this module's endpoints.
from fastapi import APIRouter
# Shared helpers, models, imports (Body, Query, datetime, Command...) and state.
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

# Sub-router merged into the aggregate router.
router = APIRouter()



@router.post("/{tenant_id}/aggregate/fleet-reclone")
async def fleet_reclone(
    tenant_id: str,
    body: dict = Body(default={}),
    current_user: User = Depends(auth.get_current_user),
):
    """Ask every approved spoke to re-clone all its VMs from the template.

    The requested concurrency (1-10, default 3) is saved as the tenant default and sent along.
    """
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Clamp requested parallel clones per spoke to 1..10 (default 3).
    concurrency = _coerce_int(body.get("concurrency"), 3, minimum=1, maximum=10)
    # Remember the concurrency as the tenant default; only save if it actually changed.
    tenant = _get_tenant(resolved_tenant_id)
    hub_config = dict(tenant.hub_config or {})
    hub_config["fleet_reclone_concurrency"] = concurrency
    if hub_config != tenant.hub_config:
        tenant.hub_config = hub_config
        store.save_tenant(tenant)

    # Number of spokes a command was queued for.
    queued = 0
    # Commands expire after 24 hours so offline spokes do not run stale reclones.
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    # Queue a reclone-all command for every approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # Queue the command for this spoke (picked up on its next poll or WebSocket push).
        store.enqueue_command(
            Command(
                spoke_id=spoke.id,
                tenant_id=resolved_tenant_id,
                type="proxmox_reclone_all",
                payload={"concurrency": concurrency},
                expires_at=expires_at,
            )
        )
        queued += 1
    # Report how many spokes were queued.
    return {"tenant_id": resolved_tenant_id, "queued": queued, "concurrency": concurrency}


@router.post("/{tenant_id}/aggregate/fleet-reclone-clear")
async def fleet_reclone_clear(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a clear_reclone_state command on every approved spoke to dismiss stale error state."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)

    # Number of spokes a command was queued for.
    queued = 0
    # Queue a clear command for every approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # Queue the command for this spoke (picked up on its next poll or WebSocket push).
        store.enqueue_command(
            Command(
                spoke_id=spoke.id,
                tenant_id=resolved_tenant_id,
                type="clear_reclone_state",
                payload={},
            )
        )
        queued += 1
    return {"tenant_id": resolved_tenant_id, "queued": queued}


@router.post("/{tenant_id}/aggregate/fleet-reclone-clear-spoke")
async def fleet_reclone_clear_spoke(
    tenant_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a clear_reclone_state command on a single spoke."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # A spoke id is required.
    spoke_id = str(body.get("spoke_id") or "").strip()
    if not spoke_id:
        raise HTTPException(status_code=400, detail="spoke_id is required")

    # The spoke must exist and be approved.
    spoke = store.get_spoke(resolved_tenant_id, spoke_id)
    if not spoke or spoke.status != "approved":
        raise HTTPException(status_code=404, detail="Approved spoke not found")

    # Queue the clear command with a 24-hour expiry.
    store.enqueue_command(
        Command(
            spoke_id=spoke_id,
            tenant_id=resolved_tenant_id,
            type="clear_reclone_state",
            payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    return {"tenant_id": resolved_tenant_id, "spoke_id": spoke_id, "queued": 1}


@router.post("/{tenant_id}/aggregate/unlock-template")
async def unlock_template(
    tenant_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    current_user: User = Depends(auth.get_current_user),
):
    """Release a stuck template lock on one spoke (if spoke_id given) or on every approved spoke."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Optional spoke id; empty means all spokes.
    requested_spoke_id = str(body.get("spoke_id") or "").strip()
    # Approved spokes in the tenant (only those may receive commands).
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    # Narrow to the requested spoke; 404 if it is not an approved spoke in this tenant.
    if requested_spoke_id:
        approved_spokes = [spoke for spoke in approved_spokes if spoke.id == requested_spoke_id]
        if not approved_spokes:
            raise HTTPException(status_code=404, detail="Approved spoke not found")

    # Number of spokes a command was queued for.
    queued = 0
    # Queue an unlock_template command for each selected spoke.
    for spoke in approved_spokes:
        # Queue the command for this spoke (picked up on its next poll or WebSocket push).
        store.enqueue_command(
            Command(
                spoke_id=spoke.id,
                tenant_id=resolved_tenant_id,
                type="unlock_template",
                payload={},
            )
        )
        queued += 1
    return {"tenant_id": resolved_tenant_id, "queued": queued, "spoke_id": requested_spoke_id or None}


@router.post("/{tenant_id}/aggregate/proxmox-approve-agent")
async def hub_proxmox_approve_agent(
    tenant_id: str,
    body: dict[str, Any] = Body(...),
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a proxmox_approve_agent command to a specific spoke so the hub can approve a
    Proxmox agent from the VM Server screen without logging into the spoke directly."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Spoke and Proxmox agent hostname are both required.
    spoke_id = str(body.get("spoke_id") or "").strip()
    hostname = str(body.get("hostname") or "").strip()
    if not spoke_id or not hostname:
        raise HTTPException(status_code=400, detail="spoke_id and hostname are required")
    # Approved spokes in the tenant (only those may receive commands).
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    # Find the requested spoke among them; 404 if it is not approved or not in this tenant.
    spoke = next((s for s in approved_spokes if s.id == spoke_id), None)
    if not spoke:
        raise HTTPException(status_code=404, detail="Approved spoke not found")
    # Queue the approval; it expires after 1 hour because it is an interactive action.
    store.enqueue_command(Command(
        spoke_id=spoke.id,
        tenant_id=resolved_tenant_id,
        type="proxmox_approve_agent",
        payload={"hostname": hostname},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    return {"tenant_id": resolved_tenant_id, "spoke_id": spoke_id, "hostname": hostname, "queued": 1}


@router.post("/{tenant_id}/aggregate/proxmox-revoke-agent")
async def hub_proxmox_revoke_agent(
    tenant_id: str,
    body: dict[str, Any] = Body(...),
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a proxmox_revoke_agent command to a specific spoke to revoke an approved agent key."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Spoke and Proxmox agent hostname are both required.
    spoke_id = str(body.get("spoke_id") or "").strip()
    hostname = str(body.get("hostname") or "").strip()
    if not spoke_id or not hostname:
        raise HTTPException(status_code=400, detail="spoke_id and hostname are required")
    # Approved spokes in the tenant (only those may receive commands).
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    # Find the requested spoke among them; 404 if it is not approved or not in this tenant.
    spoke = next((s for s in approved_spokes if s.id == spoke_id), None)
    if not spoke:
        raise HTTPException(status_code=404, detail="Approved spoke not found")
    # Queue the revocation with a 1-hour expiry.
    store.enqueue_command(Command(
        spoke_id=spoke.id,
        tenant_id=resolved_tenant_id,
        type="proxmox_revoke_agent",
        payload={"hostname": hostname},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    return {"tenant_id": resolved_tenant_id, "spoke_id": spoke_id, "hostname": hostname, "queued": 1}


@router.get("/{tenant_id}/aggregate/fleet-reclone-status")
def get_fleet_reclone_status(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Summarise reclone progress across all approved spokes in the tenant."""
    # Any tenant member may read status; resolves and access-checks the tenant id.
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Tenant record, used for the default concurrency setting.
    tenant = _get_tenant(resolved_tenant_id)
    # Fleet-wide totals and per-spoke rows.
    total_vms = 0
    completed = 0
    failed = 0
    any_running = False
    spokes_out: list[dict[str, Any]] = []
    # Add each spoke's reclone progress (from its telemetry) to the totals.
    for spoke in _approved_spokes(resolved_tenant_id):
        # Reclone counters reported by the spoke (non-numbers become 0).
        reclone = _telemetry_dict(spoke, "reclone_state")
        spoke_total = _coerce_int(reclone.get("total"), 0, minimum=0)
        spoke_completed = _coerce_int(reclone.get("completed"), 0, minimum=0)
        spoke_failed = _coerce_int(reclone.get("failed"), 0, minimum=0)
        status = str(reclone.get("status") or "idle")
        # Accumulate totals; the fleet is "running" if any spoke is.
        total_vms += spoke_total
        completed += spoke_completed
        failed += spoke_failed
        any_running = any_running or status == "running"
        # Per-spoke row for the progress table.
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "status": status,
            "total": spoke_total,
            "completed": spoke_completed,
            "failed": spoke_failed,
        })
    # Sort per-spoke rows by name for stable display.
    spokes_out.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
    # Totals, per-spoke rows and the default concurrency (1..10, default 3).
    return {
        "tenant_id": resolved_tenant_id,
        "any_running": any_running,
        "total_vms": total_vms,
        "completed": completed,
        "failed": failed,
        "default_concurrency": _coerce_int((tenant.hub_config or {}).get("fleet_reclone_concurrency"), 3, minimum=1, maximum=10),
        "spokes": spokes_out,
    }


@router.get("/{tenant_id}/aggregate/usb-provisioning-status")
def get_usb_provisioning_status(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Summarise USB dongle slot usage and auto-provisioning across the tenant's spokes."""
    # Any tenant member may read status; resolves and access-checks the tenant id.
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Fleet-wide slot totals and per-spoke rows.
    total_slots = 0
    used_slots = 0
    spokes_out: list[dict[str, Any]] = []
    # Add each spoke's USB capacity to the totals.
    for spoke in _approved_spokes(resolved_tenant_id):
        used, total, dongles, auto_provision = _spoke_usb_capacity(spoke)
        total_slots += total
        used_slots += used
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "used": used,
            "total": total,
            "dongle_count": dongles,
            "auto_provision": auto_provision,
        })
    # Sort per-spoke rows by name for stable display.
    spokes_out.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
    # Total dongles plugged in across all spokes.
    total_dongles = sum(s.get("dongle_count", 0) for s in spokes_out)
    # auto_provision_on: true only if ALL spokes with dongles have it enabled
    # (reflects toggle-all state accurately; any-spoke-on was misleading after disabling)
    enabled_spokes = [s for s in spokes_out if s["auto_provision"]]
    auto_provision_on = len(enabled_spokes) > 0 and len(enabled_spokes) == len(spokes_out)
    # Fleet totals plus per-spoke rows.
    return {
        "tenant_id": resolved_tenant_id,
        "total_slots": total_slots,
        "used_slots": used_slots,
        "total_dongles": total_dongles,
        "auto_provision_on": auto_provision_on,
        "spokes": spokes_out,
    }


@router.post("/{tenant_id}/aggregate/toggle-auto-provision")
def toggle_auto_provision(
    tenant_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    current_user: User = Depends(auth.get_current_user),
):
    """Toggle usb_auto_provision on/off for all approved spokes in this tenant."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Desired state from the body ("on"/"off" strings are what spokes expect).
    enable: bool = bool(body.get("enable", False))
    new_val = "on" if enable else "off"

    # Persist the tenant-level override so that _build_spoke_config_payload always
    # includes usb_auto_provision.  Without this, a spoke re-registration (reboot /
    # reconnect) would overwrite spoke.config with the registration payload (which
    # still says "on"), and the subsequent ensure_config_update_command push would
    # not carry usb_auto_provision at all — effectively losing the toggle.
    tenant = _get_tenant(resolved_tenant_id)
    hub_config = dict(tenant.hub_config or {})
    hub_config["usb_auto_provision"] = new_val
    tenant.hub_config = hub_config
    store.save_tenant(tenant)

    # Count of spokes updated.
    updated = 0
    # Update each approved spoke's config and push it.
    for spoke in _approved_spokes(resolved_tenant_id):
        # Copy the spoke config with the new setting and bump its version.
        next_config = dict(spoke.config or {})
        next_config["usb_auto_provision"] = new_val
        spoke.config = next_config
        spoke.config_version = (spoke.config_version or 0) + 1
        # Save and queue a config_update carrying the full config and its version (24-hour expiry).
        store.save_spoke(spoke)
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="config_update",
            payload={**next_config, "__config_version": spoke.config_version},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        ))
        updated += 1
    # Report the new value and how many spokes were updated.
    return {"ok": True, "auto_provision": new_val, "updated_spokes": updated}


@router.post("/{tenant_id}/aggregate/refresh-webui")
def aggregate_refresh_webui(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a refresh_webui command on all approved spokes so they download the latest cs-webui frontend."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Count of spokes queued.
    queued = 0
    # Queue a refresh_webui command (1-hour expiry) for each approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="refresh_webui",
            payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        queued += 1
    # Report how many spokes were queued.
    return {"ok": True, "queued": queued}


@router.post("/{tenant_id}/aggregate/update-all-spokes")
def aggregate_update_all_spokes(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue self_update and proxmox_agent_update commands on all approved spokes."""
    # Only tenant admins may run fleet actions; resolves the tenant id being acted on.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Count of spokes queued.
    queued = 0
    # For each approved spoke, queue the Proxmox agent update and the spoke self-update (1-hour
    # expiry).
    for spoke in _approved_spokes(resolved_tenant_id):
        for cmd_type in ("proxmox_agent_update", "self_update"):
            store.enqueue_command(Command(
                spoke_id=spoke.id,
                tenant_id=resolved_tenant_id,
                type=cmd_type,
                payload={},
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
        queued += 1
    # Report how many spokes were queued.
    return {"ok": True, "spokes_queued": queued}


@router.post("/aggregate/config-push")
def push_tenant_config(
    payload: ConfigPushRequest,
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    """Store a tenant-wide config and queue it for every approved spoke (tenant admins only)."""
    # Tenant id may come from the body or the query string.
    requested_tenant_id = payload.tenant_id or tenant_id
    # Resolve and access-check it, then require admin rights on it.
    resolved_tenant_id = _require_tenant_admin(_resolve_tenant_id(requested_tenant_id, current_user), current_user)
    # Save the config and queue config_update commands; returns the affected spokes.
    updated_spokes = _store_and_queue_tenant_config(resolved_tenant_id, payload.config or {})
    return {"tenant_id": resolved_tenant_id, "config": payload.config, "spokes": updated_spokes}
