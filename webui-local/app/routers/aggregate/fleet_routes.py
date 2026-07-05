"""Fleet reclone, proxmox agent, provisioning, and config-push routes for the aggregate router package."""
from __future__ import annotations

from fastapi import APIRouter
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

router = APIRouter()



@router.post("/{tenant_id}/aggregate/fleet-reclone")
async def fleet_reclone(
    tenant_id: str,
    body: dict = Body(default={}),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    concurrency = _coerce_int(body.get("concurrency"), 3, minimum=1, maximum=10)
    tenant = _get_tenant(resolved_tenant_id)
    hub_config = dict(tenant.hub_config or {})
    hub_config["fleet_reclone_concurrency"] = concurrency
    if hub_config != tenant.hub_config:
        tenant.hub_config = hub_config
        store.save_tenant(tenant)

    queued = 0
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    for spoke in _approved_spokes(resolved_tenant_id):
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
    return {"tenant_id": resolved_tenant_id, "queued": queued, "concurrency": concurrency}


@router.post("/{tenant_id}/aggregate/fleet-reclone-clear")
async def fleet_reclone_clear(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a clear_reclone_state command on every approved spoke to dismiss stale error state."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)

    queued = 0
    for spoke in _approved_spokes(resolved_tenant_id):
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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    spoke_id = str(body.get("spoke_id") or "").strip()
    if not spoke_id:
        raise HTTPException(status_code=400, detail="spoke_id is required")

    spoke = store.get_spoke(resolved_tenant_id, spoke_id)
    if not spoke or spoke.status != "approved":
        raise HTTPException(status_code=404, detail="Approved spoke not found")

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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    requested_spoke_id = str(body.get("spoke_id") or "").strip()
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    if requested_spoke_id:
        approved_spokes = [spoke for spoke in approved_spokes if spoke.id == requested_spoke_id]
        if not approved_spokes:
            raise HTTPException(status_code=404, detail="Approved spoke not found")

    queued = 0
    for spoke in approved_spokes:
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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    spoke_id = str(body.get("spoke_id") or "").strip()
    hostname = str(body.get("hostname") or "").strip()
    if not spoke_id or not hostname:
        raise HTTPException(status_code=400, detail="spoke_id and hostname are required")
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    spoke = next((s for s in approved_spokes if s.id == spoke_id), None)
    if not spoke:
        raise HTTPException(status_code=404, detail="Approved spoke not found")
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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    spoke_id = str(body.get("spoke_id") or "").strip()
    hostname = str(body.get("hostname") or "").strip()
    if not spoke_id or not hostname:
        raise HTTPException(status_code=400, detail="spoke_id and hostname are required")
    approved_spokes = list(_approved_spokes(resolved_tenant_id))
    spoke = next((s for s in approved_spokes if s.id == spoke_id), None)
    if not spoke:
        raise HTTPException(status_code=404, detail="Approved spoke not found")
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
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    total_vms = 0
    completed = 0
    failed = 0
    any_running = False
    spokes_out: list[dict[str, Any]] = []
    for spoke in _approved_spokes(resolved_tenant_id):
        reclone = _telemetry_dict(spoke, "reclone_state")
        spoke_total = _coerce_int(reclone.get("total"), 0, minimum=0)
        spoke_completed = _coerce_int(reclone.get("completed"), 0, minimum=0)
        spoke_failed = _coerce_int(reclone.get("failed"), 0, minimum=0)
        status = str(reclone.get("status") or "idle")
        total_vms += spoke_total
        completed += spoke_completed
        failed += spoke_failed
        any_running = any_running or status == "running"
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "status": status,
            "total": spoke_total,
            "completed": spoke_completed,
            "failed": spoke_failed,
        })
    spokes_out.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
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
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    total_slots = 0
    used_slots = 0
    spokes_out: list[dict[str, Any]] = []
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
    spokes_out.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
    total_dongles = sum(s.get("dongle_count", 0) for s in spokes_out)
    # auto_provision_on: true only if ALL spokes with dongles have it enabled
    # (reflects toggle-all state accurately; any-spoke-on was misleading after disabling)
    enabled_spokes = [s for s in spokes_out if s["auto_provision"]]
    auto_provision_on = len(enabled_spokes) > 0 and len(enabled_spokes) == len(spokes_out)
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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
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

    updated = 0
    for spoke in _approved_spokes(resolved_tenant_id):
        next_config = dict(spoke.config or {})
        next_config["usb_auto_provision"] = new_val
        spoke.config = next_config
        spoke.config_version = (spoke.config_version or 0) + 1
        store.save_spoke(spoke)
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="config_update",
            payload={**next_config, "__config_version": spoke.config_version},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        ))
        updated += 1
    return {"ok": True, "auto_provision": new_val, "updated_spokes": updated}


@router.post("/{tenant_id}/aggregate/refresh-webui")
def aggregate_refresh_webui(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a refresh_webui command on all approved spokes so they download the latest cs-webui frontend."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    queued = 0
    for spoke in _approved_spokes(resolved_tenant_id):
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="refresh_webui",
            payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        queued += 1
    return {"ok": True, "queued": queued}


@router.post("/{tenant_id}/aggregate/update-all-spokes")
def aggregate_update_all_spokes(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue self_update and proxmox_agent_update commands on all approved spokes."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    queued = 0
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
    return {"ok": True, "spokes_queued": queued}


@router.post("/aggregate/config-push")
def push_tenant_config(
    payload: ConfigPushRequest,
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    requested_tenant_id = payload.tenant_id or tenant_id
    resolved_tenant_id = _require_tenant_admin(_resolve_tenant_id(requested_tenant_id, current_user), current_user)
    updated_spokes = _store_and_queue_tenant_config(resolved_tenant_id, payload.config or {})
    return {"tenant_id": resolved_tenant_id, "config": payload.config, "spokes": updated_spokes}
