"""QA provisioning / health / teardown routes for the aggregate router package."""
from __future__ import annotations

from fastapi import APIRouter
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

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
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    spokes_out: list[dict[str, Any]] = []

    for spoke in _approved_spokes(resolved_tenant_id):
        _used_slots, _total_slots, dongle_count, auto_provision = _spoke_usb_capacity(spoke)
        proxmox = _telemetry_dict(spoke, "proxmox")
        vm_count = int(proxmox.get("vm_count") or 0)
        proxmox_connected = bool(proxmox.get("connected", False))
        reporting_clients = len(_telemetry_clients(spoke))
        spoke_online = _is_online(spoke)

        issues: list[str] = []
        if not spoke_online:
            issues.append("spoke is offline")
        if not proxmox_connected:
            issues.append("Proxmox agent is not connected")
        if auto_provision and dongle_count > 0 and vm_count != dongle_count:
            issues.append(
                f"VM count ({vm_count}) does not match dongle count ({dongle_count})"
            )
        if dongle_count > 0 and reporting_clients != dongle_count:
            issues.append(
                f"reporting clients ({reporting_clients}) does not match dongle count ({dongle_count})"
            )

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

    spokes_out.sort(key=lambda s: str(s.get("spoke_name") or "").lower())
    total_dongles = sum(s["dongle_count"] for s in spokes_out)
    total_clients = sum(s["reporting_clients"] for s in spokes_out)
    overall_pass = bool(spokes_out) and all(s["pass"] for s in spokes_out)

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
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    spokes = _approved_spokes(resolved_tenant_id)

    spokes_online = sum(1 for s in spokes if _is_online(s))
    proxmox_agents_connected = sum(
        1 for s in spokes if bool(_telemetry_dict(s, "proxmox").get("connected", False))
    )
    total_clients = sum(len(_telemetry_clients(s)) for s in spokes)

    # Spokes that have dongles but zero reporting clients are flagged.
    spokes_with_dongles_no_clients: list[str] = []
    for s in spokes:
        _u, _t, dongle_count, _ap = _spoke_usb_capacity(s)
        if dongle_count > 0 and len(_telemetry_clients(s)) == 0:
            spokes_with_dongles_no_clients.append(s.spoke_name or s.hostname)

    issues: list[str] = []
    if spokes_online < len(spokes):
        issues.append(f"{len(spokes) - spokes_online} spoke(s) offline")
    if proxmox_agents_connected < len(spokes):
        issues.append(f"{len(spokes) - proxmox_agents_connected} Proxmox agent(s) not connected")
    for name in spokes_with_dongles_no_clients:
        issues.append(f"spoke '{name}' has dongles but no clients reporting")

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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    spokes_out: list[dict[str, Any]] = []
    total_queued = 0

    for spoke in _approved_spokes(resolved_tenant_id):
        proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
        proxmox = _telemetry_dict(spoke, "proxmox")
        if not proxmox_vms and isinstance(proxmox.get("vms"), list):
            proxmox_vms = proxmox["vms"]

        sim_vms = [
            vm for vm in proxmox_vms
            if vm.get("vmid") is not None and int(vm.get("vmid", 0)) > 9000
            and not vm.get("is_template", False)
        ]

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

        queued = len(sim_vms)
        total_queued += queued
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "vms_found": queued,
            "vms_queued": queued,
        })

    # Push commands immediately rather than waiting for the next spoke telemetry cycle.
    from ...ws import notify_spoke_command
    for sp in spokes_out:
        if sp["vms_queued"] > 0:
            notify_spoke_command(resolved_tenant_id, sp["spoke_id"])

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
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    spokes_out: list[dict[str, Any]] = []
    total_remaining = 0

    for spoke in _approved_spokes(resolved_tenant_id):
        proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
        proxmox = _telemetry_dict(spoke, "proxmox")
        if not proxmox_vms and isinstance(proxmox.get("vms"), list):
            proxmox_vms = proxmox["vms"]

        sim_vms_remaining = [
            vm for vm in proxmox_vms
            if vm.get("vmid") is not None and int(vm.get("vmid", 0)) > 9000
            and not vm.get("is_template", False)
        ]
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
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    expected_clients = 0
    updated_spokes: list[dict[str, Any]] = []

    for spoke in _approved_spokes(resolved_tenant_id):
        _used, _total, dongle_count, _auto = _spoke_usb_capacity(spoke)
        expected_clients += dongle_count

        next_config = dict(spoke.config or {})
        next_config["usb_auto_provision"] = "on"
        spoke.config = next_config
        spoke.config_version = (spoke.config_version or 0) + 1
        store.save_spoke(spoke)
        store.enqueue_command(Command(
            spoke_id=spoke.id,
            tenant_id=resolved_tenant_id,
            type="config_update",
            payload={**next_config, "__config_version": spoke.config_version},
            expires_at=expires_at,
        ))
        updated_spokes.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "dongle_count": dongle_count,
        })

    return {
        "ok": True,
        "tenant_id": resolved_tenant_id,
        "auto_provision": "on",
        "expected_clients": expected_clients,
        "updated_spokes": len(updated_spokes),
        "spokes": updated_spokes,
    }
