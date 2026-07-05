"""Aggregate dashboard / clients / simulations / proxmox summary routes for the aggregate router package."""
from __future__ import annotations

from fastapi import APIRouter
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

router = APIRouter()



@router.get("/aggregate/dashboard")
def get_aggregate_dashboard(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    spokes = _approved_spokes(resolved_tenant_id)
    clients = [client for spoke in spokes for client in _telemetry_clients(spoke)]
    hardware_breakdown = dict(sorted(Counter(_hardware_type(client) for client in clients).items()))
    return {
        "tenant_id": resolved_tenant_id,
        "client_count": len(clients),
        "hardware_breakdown": hardware_breakdown,
        "checks_summary": _build_checks_summary(spokes),
        "spokes_online": sum(1 for spoke in spokes if _is_online(spoke)),
        "spokes_total": len(spokes),
    }


@router.get("/aggregate/clients")
def get_aggregate_clients(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    # Load hub-managed per-client sim overrides once (no GitHub needed).
    tenant = _get_tenant(resolved_tenant_id)
    admin_sim_overrides: dict[str, list[str]] = dict(tenant.client_sim_overrides or {})
    # Collect all candidate rows keyed by MAC (or hostname fallback) so we can
    # deduplicate VMs that appear on multiple spokes due to client_history.json
    # retention.  A client with active simulations beats a stale historical one.
    candidates: dict[str, dict[str, Any]] = {}
    for spoke in _approved_spokes(resolved_tenant_id):
        spoke_name = spoke.spoke_name or spoke.hostname
        usb_vmids, usb_hostnames, vmids_by_hostname = _spoke_usb_lookup(spoke)
        t3_vmids = _spoke_t3_lookup(spoke)
        proxmox_tel = _telemetry_dict(spoke, "proxmox")
        t3_pci_devices: list[dict[str, Any]] = []
        if isinstance(proxmox_tel.get("t3_pci_devices"), list):
            t3_pci_devices = proxmox_tel["t3_pci_devices"]
        t3_pci_count = int(proxmox_tel.get("t3_pci_count") or len(t3_pci_devices))
        for client in _telemetry_clients(spoke):
            row = dict(client)
            # Merge hub-managed permanent sim overrides into active_simulations.
            # These are set by admins via the UI and stored locally — no GitHub key needed.
            hostname = str(row.get("hostname") or "")
            extra_sims = admin_sim_overrides.get(hostname, [])
            if extra_sims:
                merged = list(set(list(row.get("active_simulations") or []) + list(extra_sims)))
                row["active_simulations"] = merged
            row.update({
                "tenant_id": resolved_tenant_id,
                "spoke_id": spoke.id,
                "spoke_name": spoke_name,
                "spoke_hostname": spoke.hostname,
                "spoke_label": spoke.label,
                # Trust the spoke's own has_usb if True (serialize_client sets it via
                # _hostname_has_usb which has direct access to proxmox_state). Fall back
                # to the hub's telemetry-based lookup in case the spoke hasn't set it.
                "has_usb": bool(row.get("has_usb")) or _client_has_usb(row, usb_vmids, usb_hostnames, vmids_by_hostname),
                # T3 stays node-scoped for section counts, but client classification must
                # follow the VM's own PCI passthrough config.
                "has_t3_pci": _client_has_t3_pci(row, t3_vmids, vmids_by_hostname),
                "t3_pci_count": t3_pci_count,
                "t3_pci_devices": t3_pci_devices,
            })
            # Dedup key: prefer MAC, fall back to hostname. VMs that appear in
            # multiple spokes' client_history.json should only be counted once —
            # on whichever spoke has them actively simulated.
            dedup_key = str(row.get("mac") or row.get("hostname") or "").lower().strip()
            if not dedup_key:
                candidates[id(row)] = row  # no key available, always include
                continue
            existing = candidates.get(dedup_key)
            if existing is None:
                candidates[dedup_key] = row
            else:
                # Prefer the spoke that has an active simulation running
                existing_active = bool(existing.get("active_simulations"))
                new_active = bool(row.get("active_simulations"))
                if new_active and not existing_active:
                    candidates[dedup_key] = row
    rows = sorted(candidates.values(),
                  key=lambda item: (str(item.get("spoke_name") or "").lower(),
                                    str(item.get("hostname") or "").lower()))
    return {"tenant_id": resolved_tenant_id, "clients": rows}


@router.get("/aggregate/simulations")
def get_aggregate_simulations(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    rows: list[dict[str, Any]] = []
    for spoke in _approved_spokes(resolved_tenant_id):
        counts: Counter[str] = Counter()
        for client in _telemetry_clients(spoke):
            names = [str(name).strip() for name in (client.get("active_simulations") or []) if str(name).strip()]
            if not names:
                fallback = str(client.get("simulation_id") or "").strip()
                if fallback:
                    names = [fallback]
            for name in names:
                counts[name] += 1

        spoke_name = spoke.spoke_name or spoke.hostname
        online = _is_online(spoke)
        status = "Running" if online else "Spoke Offline"
        if counts:
            for simulation_name, client_count in sorted(counts.items()):
                rows.append({
                    "tenant_id": resolved_tenant_id,
                    "spoke_id": spoke.id,
                    "spoke_name": spoke_name,
                    "spoke_hostname": spoke.hostname,
                    "simulation_name": simulation_name,
                    "status": status,
                    "client_count": client_count,
                    "spoke_online": online,
                })
        else:
            rows.append({
                "tenant_id": resolved_tenant_id,
                "spoke_id": spoke.id,
                "spoke_name": spoke_name,
                "spoke_hostname": spoke.hostname,
                "simulation_name": "—",
                "status": "Idle" if online else "Spoke Offline",
                "client_count": 0,
                "spoke_online": online,
            })

    rows.sort(key=lambda item: (str(item.get("spoke_name") or "").lower(), str(item.get("simulation_name") or "").lower()))
    return {"tenant_id": resolved_tenant_id, "simulations": rows}


@router.get("/aggregate/proxmox")
def get_aggregate_proxmox(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    hosts: list[dict[str, Any]] = []
    # Fetch effective USB list (global + tenant) once — same for all spokes.
    effective_usb_vidpids = [{k: v for k, v in d.items() if k != "source"}
                              for d in store.get_effective_usb_vidpids(resolved_tenant_id)]
    for spoke in _approved_spokes(resolved_tenant_id):
        proxmox = _telemetry_dict(spoke, "proxmox")
        vms = _telemetry_list(spoke, "proxmox_vms") or (proxmox.get("vms") if isinstance(proxmox.get("vms"), list) else [])
        usb_devices = _telemetry_list(spoke, "usb_devices") or (proxmox.get("usb_state") if isinstance(proxmox.get("usb_state"), list) else [])
        # Join prov_status from usb_state into each VM — the agent reports them separately.
        # usb_state entries have vmid + prov_status (active/provisioning/tearing_down/missing).
        _usb_prov_by_vmid: dict[int, str] = {
            int(u["vmid"]): u["prov_status"]
            for u in usb_devices
            if isinstance(u, dict) and u.get("vmid") is not None and u.get("prov_status")
        }
        if _usb_prov_by_vmid:
            vms = [
                {**vm, "prov_status": _usb_prov_by_vmid.get(int(vm["vmid"]), vm.get("prov_status"))}
                if isinstance(vm, dict) and vm.get("vmid") is not None
                else vm
                for vm in vms
            ]
        _used_slots, _total_slots, _dongle_count, auto_provision = _spoke_usb_capacity(spoke)
        tel = spoke.telemetry or {}
        hosts.append({
            "tenant_id": resolved_tenant_id,
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "spoke_online": _is_online(spoke),
            "last_seen": spoke.last_seen,
            "hub_rtt_ms": tel.get("hub_rtt_ms"),
            "hub_processing_ms": tel.get("hub_processing_ms"),
            "hub_loop_lag_ms": tel.get("hub_loop_lag_ms"),
            "telemetry_build_ms": tel.get("telemetry_build_ms"),
            "ws_reconnect_count": tel.get("ws_reconnect_count"),
            "ws_last_error": tel.get("ws_last_error"),
            "sim_conf_read_error": tel.get("sim_conf_read_error"),
            "node": proxmox.get("node") if isinstance(proxmox.get("node"), dict) else {},
            "proxmox": proxmox,
            "proxmox_vms": vms,
            "usb_devices": usb_devices,
            "vm_count": int(proxmox.get("vm_count") or len(vms)),
            "usb_count": int(proxmox.get("usb_count") or len(usb_devices)) or sum(
                1 for vm in (proxmox.get("vms") or [])
                if isinstance(vm, dict)
                and (vm.get("has_usb_config") or vm.get("reclone_bus_path"))
                and not vm.get("is_template")
                and vm.get("prov_status") not in ("tearing_down", "provisioning")
            ),
            "reclone_state": _telemetry_dict(spoke, "reclone_state"),
            "api_server": _telemetry_dict(spoke, "api_server"),
            "pending_command_count": sum(
                1 for c in store.list_commands(resolved_tenant_id, spoke.id)
                if c.status in ("queued", "delivered")
            ),
            "spoke_config": {
                "usb_max_slots": str((spoke.config or {}).get("usb_max_slots", "24")),
                "vmid_start": int((spoke.config or {}).get("vmid_start", 0) or 0),
                "usb_vidpids": effective_usb_vidpids,
                "hostname": spoke.hostname or "",
                # Read auto_provision from telemetry via _spoke_usb_capacity so the
                # hub reflects the spoke's actual runtime state, not just hub DB config.
                "usb_auto_provision": "on" if auto_provision else "off",
                "usb_missing_timeout": str((spoke.config or {}).get("usb_missing_timeout", "60")),
                "usb_sim_phy": (spoke.config or {}).get("usb_sim_phy", "wireless"),
                "usb_ignored_vidpids": (spoke.config or {}).get("usb_ignored_vidpids", "[]"),
                "reclone_concurrency": str((spoke.config or {}).get("reclone_concurrency", "1")),
            },
        })
    hosts.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
    return {"tenant_id": resolved_tenant_id, "hosts": hosts}
