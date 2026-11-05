"""Pure helpers for CS_GET_SIM_TAG_HEALTH: why a Proxmox server shows no or stale sim- tags.

Built from a real incident: three servers showed three different faults (no
tags, stale tags, correct) and every one was invisible. The dispatch skipped
unresolvable hosts silently, and a VM with no client match was simply absent
from the map. So each host reports whether its agent resolved, and each VM
reports DESIRED vs ACTUAL tags plus the reason it was excluded, so a mismatch
names itself.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Used to compute how many seconds ago something last reported.
import time
# Type hints only.
from typing import Any, Dict, List, Optional

# How recently a client must have reported for its sim- tags to stay on its VM; reuse the
# dispatcher's own window so this panel and the real tagging logic always agree.
from token_store import _ONLINE_WINDOW_S as TAG_LIVE_WINDOW_S
# Normalises a simulation name into a valid Proxmox tag, exactly as the tag dispatcher does.
from token_store import sanitize_tag

# A Proxmox host that has sent no telemetry for 15 minutes is treated as not live.
STALE_HOST_S = 900.0  # 15 min without telemetry = not a live host
# VM ids above this number are simulation clients; lower ids are infrastructure guests.
SIM_VMID_FLOOR = 90000  # same floor as usb_provision VMID_FLOOR / _protected_vmids


def _age_s(ts) -> Optional[float]:
    """Return how many seconds ago ``ts`` was, or None if ``ts`` is not a number."""
    # Seconds since the given epoch timestamp, or None when it is missing or not a number.
    return (time.time() - float(ts)) if isinstance(ts, (int, float)) else None


def client_index(registry_clients: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """client hostname -> {last_seen_age_s, sims}, read from the SAME registry
    the tag map uses, so the panel cannot disagree with the dispatcher."""
    # Build the lookup keyed by lower-cased hostname.
    clients = {}
    # Walk every client record in the registry (tolerating an empty/None registry).
    for c in (registry_clients or {}).values():
        # Treat a None record as an empty dict so .get() calls below are safe.
        c = c or {}
        # Normalise the hostname so it matches VM names case-insensitively.
        host = str(c.get("hostname") or "").strip().lower()
        # Skip records without a hostname; they cannot be matched to a VM.
        if not host:
            continue
        # How long ago the client last checked in.
        age = _age_s(c.get("last_seen"))
        # The sim- tags this client should carry, one per active simulation.
        sims = (sanitize_tag(x) for x in (c.get("active_simulations") or []))
        # Store the rounded age and drop any simulation names that sanitise to an empty tag.
        clients[host] = {"last_seen_age_s": round(age, 1) if age is not None else None,
                         "sims": [t for t in sims if t]}
    # Hand back the hostname -> details map.
    return clients


def vm_row(vm: Dict[str, Any], clients: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Desired vs actual sim- tags for one VM, plus why it was excluded (if it was)."""
    # VM names are matched to client hostnames case-insensitively.
    name = str(vm.get("name") or "").strip().lower()
    # Tags currently on the VM; only sim- tags matter here.
    actual = sorted(str(t) for t in (vm.get("tags") or []) if str(t).startswith("sim-"))
    # Find the client this VM belongs to, if any.
    client = clients.get(name)
    # Decide the desired tags and, when the VM is excluded, record why. No client has this VM's
    # name, so nothing should be tagged.
    if client is None:
        why, desired = "no_client_matches_vm_name", []
    # The client exists but has never sent a heartbeat.
    elif client["last_seen_age_s"] is None:
        why, desired = "client_never_reported", []
    # The client went quiet longer than the live window, so its tags are cleared.
    elif client["last_seen_age_s"] >= TAG_LIVE_WINDOW_S:
        why, desired = "client_stale_tags_cleared", []
    # Live client: its VM should carry exactly its active simulation tags.
    else:
        why, desired = None, sorted(client["sims"])
    # Build the comparison row; in_sync is True when desired and actual tags match.
    row = {"vmid": vm.get("vmid"), "name": vm.get("name"),
           "desired": desired, "actual": actual, "in_sync": desired == actual}
    # Only include the exclusion reason when there is one.
    if why:
        row["excluded"] = why
    # Show how stale the client is whenever a client was found.
    if client:
        row["client_last_seen_age_s"] = client["last_seen_age_s"]
    # Return the per-VM report row.
    return row


def _is_sim_vm(row: Dict[str, Any]) -> bool:
    """Return True when the row's VM id marks it as a simulation client VM."""
    # Simulation VMs are those whose id is above the sim floor.
    try:
        return int(row.get("vmid") or 0) > SIM_VMID_FLOOR
    # A missing or non-numeric vmid cannot be a sim VM.
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
    # Sim VM + excluded + no sim- tags = a VM that silently has no label.
    return _is_sim_vm(row) and bool(row.get("excluded")) and not row["actual"]


def host_report(hostname: str, state: Dict[str, Any], clients: Dict[str, Dict[str, Any]],
                agent_connected: bool, skip_reason: Optional[str],
                points_at: Optional[str]) -> Dict[str, Any]:
    """Build the tag-health report for one Proxmox host and all of its VMs.

    Args:
        hostname: Proxmox host name.
        state: Last telemetry stored for the host (VM list, last_seen, ...).
        clients: Output of client_index().
        agent_connected: Whether the host's agent is connected right now.
        skip_reason: Why the tag dispatch skipped this host, if it did.
        points_at: Where the host's agent says it is pointed (spoke URL/IP).
    """
    # Build one row per real VM, skipping templates and LXC containers (they never get sim tags).
    vms = [vm_row(vm, clients) for vm in (state.get("vms") or [])
           if not vm.get("is_template") and vm.get("type") != "lxc"]
    # proxmox_states keeps a host FOREVER once seen (deliberate: a briefly
    # offline server keeps its identity), so a rebuilt/renamed host leaves a
    # ghost whose old VM list still enters the tag map. Show when each host
    # last reported so a ghost is distinguishable from a live host.
    # Read when this host last reported; it may be missing or malformed.
    try:
        last_seen = float((state or {}).get("last_seen") or 0)
    # Treat a malformed timestamp like "never reported".
    except (TypeError, ValueError):
        last_seen = 0.0
    # Seconds since the last report, or None if it never reported.
    age = (time.time() - last_seen) if last_seen else None
    # Summarise the host: agent status, why it was skipped, drift/unlabeled counts, telemetry age,
    # staleness and the VM rows sorted by name.
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
    # The useful data may be wrapped in payload.data, or be the reply itself; non-dicts give {}.
    data = (reply or {}).get("payload", {}).get("data", reply) if isinstance(reply, dict) else {}
    # A malformed reply tells us nothing about where the agent points.
    if not isinstance(data, dict):
        return None
    # Prefer the explicit spoke URL, then the IP, otherwise the agent is auto-discovering.
    return (str(data.get("spoke_url") or "").strip()
            or str(data.get("spoke_ip") or "").strip()
            or "(auto-discovery)")
