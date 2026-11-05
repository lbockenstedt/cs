"""QA provisioning / health / teardown routes for the aggregate router package."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# APIRouter collects this module's endpoints.
from fastapi import APIRouter
# Shared helpers, models, imports and state.
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

# Sub-router merged into the aggregate router.
router = APIRouter()



# ── QA endpoints ──────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/qa/provisioning-check")
def get_qa_provisioning_check(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """QA check: verify the dongle → VM → reporting-client pipeline per spoke.

    For each approved spoke the response reports:
    - dongle_count   : USB dongles physically present (from Proxmox agent telemetry)
    - vm_count       : VMs currently tracked by Proxmox
    - reporting_clients : clients that have posted a status update recently
    - pass           : True when all three counts match and the spoke is online

    Typical assertion: 3 spokes × 10 dongles → overall_pass=true, actual_clients=30.
    """
    # Read-only check: any tenant member; resolves and access-checks the tenant id.
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Per-spoke results.
    spokes_out: list[dict[str, Any]] = []

    # Check each approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # USB dongles present and whether auto-provisioning is on.
        _used_slots, _total_slots, dongle_count, auto_provision = _spoke_usb_capacity(spoke)
        # Proxmox VM count and whether its agent is connected.
        proxmox = _telemetry_dict(spoke, "proxmox")
        vm_count = int(proxmox.get("vm_count") or 0)
        proxmox_connected = bool(proxmox.get("connected", False))
        # Clients currently reporting, and whether the spoke itself is online.
        reporting_clients = len(_telemetry_clients(spoke))
        spoke_online = _is_online(spoke)

        # Human-readable reasons this spoke fails the check.
        issues: list[str] = []
        if not spoke_online:
            issues.append("spoke is offline")
        if not proxmox_connected:
            issues.append("Proxmox agent is not connected")
        # With auto-provisioning on, each dongle should have exactly one VM.
        if auto_provision and dongle_count > 0 and vm_count != dongle_count:
            issues.append(
                f"VM count ({vm_count}) does not match dongle count ({dongle_count})"
            )
        # Each dongle should also produce exactly one reporting client.
        if dongle_count > 0 and reporting_clients != dongle_count:
            issues.append(
                f"reporting clients ({reporting_clients}) does not match dongle count ({dongle_count})"
            )

        # Per-spoke result; it passes only when online with no issues.
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "spoke_online": spoke_online,
            "proxmox_connected": proxmox_connected,
            "auto_provision": auto_provision,
            "dongle_count": dongle_count,
            "vm_count": vm_count,
            "reporting_clients": reporting_clients,
            "pass": spoke_online and len(issues) == 0,
            "issues": issues,
        })

    # Sort by spoke name; totals; overall pass needs at least one spoke and all passing.
    spokes_out.sort(key=lambda s: str(s.get("spoke_name") or "").lower())
    total_dongles = sum(s["dongle_count"] for s in spokes_out)
    total_clients = sum(s["reporting_clients"] for s in spokes_out)
    overall_pass = bool(spokes_out) and all(s["pass"] for s in spokes_out)

    # Expected vs actual client counts, plus the per-spoke breakdown.
    return {
        "tenant_id": resolved_tenant_id,
        "overall_pass": overall_pass,
        "expected_clients": total_dongles,
        "actual_clients": total_clients,
        "delta": total_clients - total_dongles,
        "spokes": spokes_out,
    }


@router.get("/aggregate/qa/system-health")
def get_qa_system_health(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    """Full-stack QA health check across hub, spokes, Proxmox agents, and clients.

    Returns all_ok=true only when every approved spoke is online, every Proxmox
    agent is connected, and at least one client is reporting for any spoke that
    has dongles present.
    """
    # Read-only check: any tenant member; resolves and access-checks the tenant id.
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Approved spokes in the tenant.
    spokes = _approved_spokes(resolved_tenant_id)

    # Count spokes online and Proxmox agents connected.
    spokes_online = sum(1 for s in spokes if _is_online(s))
    proxmox_agents_connected = sum(
        1 for s in spokes if bool(_telemetry_dict(s, "proxmox").get("connected", False))
    )
    # Total clients reporting across all spokes.
    total_clients = sum(len(_telemetry_clients(s)) for s in spokes)

    # Spokes that have dongles but zero reporting clients are flagged.
    spokes_with_dongles_no_clients: list[str] = []
    # Find spokes with dongles but no clients.
    for s in spokes:
        _u, _t, dongle_count, _ap = _spoke_usb_capacity(s)
        if dongle_count > 0 and len(_telemetry_clients(s)) == 0:
            spokes_with_dongles_no_clients.append(s.spoke_name or s.hostname)

    # Collect a human-readable issue for each failing category.
    issues: list[str] = []
    if spokes_online < len(spokes):
        issues.append(f"{len(spokes) - spokes_online} spoke(s) offline")
    if proxmox_agents_connected < len(spokes):
        issues.append(f"{len(spokes) - proxmox_agents_connected} Proxmox agent(s) not connected")
    for name in spokes_with_dongles_no_clients:
        issues.append(f"spoke '{name}' has dongles but no clients reporting")

    # The hub answering means hub_ok; all_ok only when there are no issues.
    return {
        "hub_ok": True,
        "tenant_id": resolved_tenant_id,
        "spokes_total": len(spokes),
        "spokes_online": spokes_online,
        "proxmox_agents_connected": proxmox_agents_connected,
        "total_clients": total_clients,
        "issues": issues,
        "all_ok": len(issues) == 0,
    }


@router.post("/{tenant_id}/qa/teardown-all-vms")
def qa_teardown_all_vms(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """QA: Queue deletion of every auto-provisioned sim VM (vmid > 9000) across all spokes.

    Each VM found in spoke telemetry is queued as a `proxmox_agent_command` so the
    spoke forwards a `delete_vm` action to its local Proxmox agent.

    Returns the number of VMs queued per spoke so the caller can poll
    `GET /{tenant_id}/qa/teardown-status` until complete.
    """
    # Destructive/config-changing QA action: tenant admins only; resolves the tenant id.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Commands expire after 1 hour; per-spoke results and a running total.
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    spokes_out: list[dict[str, Any]] = []
    total_queued = 0

    # Find and queue deletion of the simulation VMs on each approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # VM list from telemetry; older spokes only report it inside the proxmox section.
        proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
        proxmox = _telemetry_dict(spoke, "proxmox")
        if not proxmox_vms and isinstance(proxmox.get("vms"), list):
            proxmox_vms = proxmox["vms"]

        # Simulation VMs use ids above 9000; templates are never deleted.
        sim_vms = [
            vm for vm in proxmox_vms
            if vm.get("vmid") is not None and int(vm.get("vmid", 0)) > 9000
            and not vm.get("is_template", False)
        ]

        # Queue a delete_vm agent command per VM, defaulting the VM type to qemu.
        for vm in sim_vms:
            vmid = int(vm["vmid"])
            vm_type = str(vm.get("type") or "qemu").strip().lower()
            if vm_type not in {"qemu", "lxc"}:
                vm_type = "qemu"
            store.enqueue_command(Command(
                spoke_id=spoke.id,
                tenant_id=resolved_tenant_id,
                type="proxmox_agent_command",
                payload={"action": "delete_vm", "args": {"vmid": vmid, "vm_type": vm_type}},
                expires_at=expires_at,
            ))

        # Record how many VMs were queued for this spoke.
        queued = len(sim_vms)
        total_queued += queued
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "vms_found": queued,
            "vms_queued": queued,
        })

    # Push commands immediately rather than waiting for the next spoke telemetry cycle.
    # Imported lazily to avoid a circular import with the ws module.
    from ...ws import notify_spoke_command
    # Wake only spokes that actually have deletions queued.
    for sp in spokes_out:
        if sp["vms_queued"] > 0:
            notify_spoke_command(resolved_tenant_id, sp["spoke_id"])

    # Total and per-spoke counts so the caller can poll teardown-status.
    return {
        "ok": True,
        "tenant_id": resolved_tenant_id,
        "total_vms_queued": total_queued,
        "spokes": spokes_out,
    }


@router.get("/{tenant_id}/qa/teardown-status")
def qa_teardown_status(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """QA: Check whether all auto-provisioned VMs have been deleted across all spokes.

    Reads the current proxmox telemetry from each spoke and counts VMs with vmid > 9000.
    Returns complete=true when every spoke reports zero sim VMs remaining.
    """
    # Read-only check: any tenant member; resolves and access-checks the tenant id.
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Per-spoke results and a total of VMs still present.
    spokes_out: list[dict[str, Any]] = []
    total_remaining = 0

    # Count remaining simulation VMs on each spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # VM list from telemetry; older spokes only report it inside the proxmox section.
        proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
        proxmox = _telemetry_dict(spoke, "proxmox")
        if not proxmox_vms and isinstance(proxmox.get("vms"), list):
            proxmox_vms = proxmox["vms"]

        # Simulation VMs (id above 9000, not templates) that still exist.
        sim_vms_remaining = [
            vm for vm in proxmox_vms
            if vm.get("vmid") is not None and int(vm.get("vmid", 0)) > 9000
            and not vm.get("is_template", False)
        ]
        # Add to the total and record the per-spoke result.
        remaining = len(sim_vms_remaining)
        total_remaining += remaining
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "spoke_online": _is_online(spoke),
            "proxmox_connected": bool(proxmox.get("connected", False)),
            "sim_vms_remaining": remaining,
            "complete": remaining == 0,
        })

    # Complete only when no simulation VMs remain anywhere.
    return {
        "tenant_id": resolved_tenant_id,
        "complete": total_remaining == 0,
        "total_remaining": total_remaining,
        "spokes": spokes_out,
    }


@router.post("/{tenant_id}/qa/enable-autoprov")
def qa_enable_autoprov(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """QA: Enable Auto-Provisioning on all approved spokes and return expected client count.

    Pushes `usb_auto_provision=on` to every spoke via config_update command.
    Returns the expected number of clients (= total dongle count across all spokes)
    so the caller can poll `GET /{tenant_id}/qa/provisioning-check` until
    actual_clients matches expected_clients.
    """
    # Destructive/config-changing QA action: tenant admins only; resolves the tenant id.
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Config updates expire after 24 hours; track expected clients and updated spokes.
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    expected_clients = 0
    updated_spokes: list[dict[str, Any]] = []

    # Turn on auto-provisioning on each approved spoke.
    for spoke in _approved_spokes(resolved_tenant_id):
        # Each dongle should become one client once provisioning finishes.
        _used, _total, dongle_count, _auto = _spoke_usb_capacity(spoke)
        expected_clients += dongle_count

        # Copy the config with auto-provisioning on and bump its version.
        next_config = dict(spoke.config or {})
        next_config["usb_auto_provision"] = "on"
        spoke.config = next_config
        spoke.config_version = (spoke.config_version or 0) + 1
        # Save and queue a config_update with the full config and version.
        store.save_spoke(spoke)
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="config_update",
            payload={**next_config, "__config_version": spoke.config_version},
            expires_at=expires_at,
        ))
        # Record this spoke and its dongle count.
        updated_spokes.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "dongle_count": dongle_count,
        })

    # Report how many clients the caller should expect to see.
    return {
        "ok": True,
        "tenant_id": resolved_tenant_id,
        "auto_provision": "on",
        "expected_clients": expected_clients,
        "updated_spokes": len(updated_spokes),
        "spokes": updated_spokes,
    }
