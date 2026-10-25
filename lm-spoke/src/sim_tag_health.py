"""Pure helpers for CS_GET_SIM_TAG_HEALTH: why a Proxmox server shows no or stale sim- tags.

Built from a real incident: three servers showed three different faults (no
tags, stale tags, correct) and every one was invisible. The dispatch skipped
unresolvable hosts silently, and a VM with no client match was simply absent
from the map. So each host reports whether its agent resolved, and each VM
reports DESIRED vs ACTUAL tags plus the reason it was excluded, so a mismatch
names itself.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from token_store import _ONLINE_WINDOW_S as TAG_LIVE_WINDOW_S
from token_store import sanitize_tag

STALE_HOST_S = 900.0  # 15 min without telemetry = not a live host
SIM_VMID_FLOOR = 90000  # same floor as usb_provision VMID_FLOOR / _protected_vmids


def _age_s(ts) -> Optional[float]:
    return (time.time() - float(ts)) if isinstance(ts, (int, float)) else None


def client_index(registry_clients: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """client hostname -> {last_seen_age_s, sims}, read from the SAME registry
    the tag map uses, so the panel cannot disagree with the dispatcher."""
    clients = {}
    for c in (registry_clients or {}).values():
        c = c or {}
        host = str(c.get("hostname") or "").strip().lower()
        if not host:
            continue
        age = _age_s(c.get("last_seen"))
        sims = (sanitize_tag(x) for x in (c.get("active_simulations") or []))
        clients[host] = {"last_seen_age_s": round(age, 1) if age is not None else None,
                         "sims": [t for t in sims if t]}
    return clients


def vm_row(vm: Dict[str, Any], clients: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Desired vs actual sim- tags for one VM, plus why it was excluded (if it was)."""
    name = str(vm.get("name") or "").strip().lower()
    actual = sorted(str(t) for t in (vm.get("tags") or []) if str(t).startswith("sim-"))
    client = clients.get(name)
    if client is None:
        why, desired = "no_client_matches_vm_name", []
    elif client["last_seen_age_s"] is None:
        why, desired = "client_never_reported", []
    elif client["last_seen_age_s"] >= TAG_LIVE_WINDOW_S:
        why, desired = "client_stale_tags_cleared", []
    else:
        why, desired = None, sorted(client["sims"])
    row = {"vmid": vm.get("vmid"), "name": vm.get("name"),
           "desired": desired, "actual": actual, "in_sync": desired == actual}
    if why:
        row["excluded"] = why
    if client:
        row["client_last_seen_age_s"] = client["last_seen_age_s"]
    return row


def _is_sim_vm(row: Dict[str, Any]) -> bool:
    try:
        return int(row.get("vmid") or 0) > SIM_VMID_FLOOR
    except (TypeError, ValueError):
        return False


def is_unlabeled(row: Dict[str, Any]) -> bool:
    """A sim VM that maps to no live client and carries no sim- tag.

    Such a VM has desired == actual == [], so it reads as in_sync and vanished
    from drift_count and from the UI: exactly the "some clients have no label in
    Proxmox" case this panel exists to explain. Counted separately because it is
    NOT drift. Only sim VMs count: infrastructure guests (the cs spoke LXC,
    hand-built VMs) match no client by design and would bury the real finding.
    """
    return _is_sim_vm(row) and bool(row.get("excluded")) and not row["actual"]


def host_report(hostname: str, state: Dict[str, Any], clients: Dict[str, Dict[str, Any]],
                agent_connected: bool, skip_reason: Optional[str],
                points_at: Optional[str]) -> Dict[str, Any]:
    vms = [vm_row(vm, clients) for vm in (state.get("vms") or [])
           if not vm.get("is_template") and vm.get("type") != "lxc"]
    # proxmox_states keeps a host FOREVER once seen (deliberate: a briefly
    # offline server keeps its identity), so a rebuilt/renamed host leaves a
    # ghost whose old VM list still enters the tag map. Show when each host
    # last reported so a ghost is distinguishable from a live host.
    try:
        last_seen = float((state or {}).get("last_seen") or 0)
    except (TypeError, ValueError):
        last_seen = 0.0
    age = (time.time() - last_seen) if last_seen else None
    return {
        "hostname": hostname,
        "agent_connected": agent_connected,
        "skip_reason": skip_reason or (None if agent_connected else "agent_not_connected"),
        "vm_count": len(vms),
        "drift_count": sum(1 for v in vms if not v["in_sync"]),
        "unlabeled_count": sum(1 for v in vms if is_unlabeled(v)),
        "telemetry_age_s": round(age, 1) if age is not None else None,
        "agent_points_at": points_at,
        "stale": bool(age is not None and age > STALE_HOST_S),
        "vms": sorted(vms, key=lambda v: str(v.get("name") or "")),
    }


def pointing_from_identity(reply: Any) -> Optional[str]:
    """Where an agent's PXMX_GET_IDENTITY reply says it is pointed."""
    data = (reply or {}).get("payload", {}).get("data", reply) if isinstance(reply, dict) else {}
    if not isinstance(data, dict):
        return None
    return (str(data.get("spoke_url") or "").strip()
            or str(data.get("spoke_ip") or "").strip()
            or "(auto-discovery)")
