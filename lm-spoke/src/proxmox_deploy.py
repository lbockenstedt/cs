"""ProxmoxDeploy — per-host Proxmox state for the cs (Client-Simulation) spoke.

Phase D1 port of the legacy ``cs/webui-spoke/server.py`` telemetry path. The
unified pxmx agent emits ``CS_TELEMETRY`` (carrying a per-host Proxmox snapshot)
up through its pxmx spoke → hub ``AGENT_RELAY_UP`` ``CS_*`` dispatcher → here as
``CS_INGEST_TELEMETRY``. This module owns the per-host state, ingests each frame
into ``proxmox_states[hostname]``, and builds the relay payload the cs spoke
re-emits as ``CS_TELEMETRY`` to the hub, which caches it for the
Simulations/VM Server view.

What is ported now (D1): the per-host state shape, telemetry ingest (enrich VMs
with ``_agent_hostname`` + template flag, normalize USB state, compute
vm/usb/running counts, store node/versions/vmid-range/provision-halt/last-seen),
and the relay payload (``proxmox`` summary + flat ``proxmox_vms`` + per-host
``proxmox_hosts`` list the VM Server view expands into one row per host).

What is deferred to later phases: the auto-provision gate, VMID-gap audit,
reclone auto-reset (E), and Proxmox-token / sim-tag sync (F).

Source of truth: ``cs/webui-spoke/server.py``
  - per-host state write:  ``_apply_proxmox_telemetry_state`` (lines 9667-9787)
  - relay payload build:   ``_build_relay_telemetry_payload`` (lines 6011-6161)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from dhcp_status import collect_dhcp_status
except ImportError:  # package-context import (core.src.base_spoke-style dual path)
    from .dhcp_status import collect_dhcp_status

logger = logging.getLogger("ProxmoxDeploy")

# Rolling resource-sample window for the per-host cpu_1h_avg / mem_1h_avg the
# VM Server Details header reads. Mirrors the legacy webui-spoke
# _RESOURCE_SAMPLE_WINDOW (server.py:3940). Samples are pruned to this window
# on every ingest, so the average always reflects the last hour (no warm-up
# delay — the mean of whatever samples exist is returned as soon as the first
# frame arrives).
_RESOURCE_SAMPLE_WINDOW = 3600.0


def _s(v: Any) -> Optional[str]:
    """Coerce to a stripped str-or-None (mirrors the legacy ``str(...).strip() or None``)."""
    if v is None:
        return None
    try:
        out = str(v).strip()
    except Exception:
        return None
    return out or None


def _is_template(vm: Dict[str, Any]) -> bool:
    if bool(vm.get("is_template")):
        return True
    tags = [str(t).lower() for t in (vm.get("tags") or [])]
    if any(t in ("template", "tmpl", "is-template") for t in tags):
        return True
    name = str(vm.get("name", "") or "").lower()
    return name.startswith(("template-", "tmpl-"))


def _tag_usb(item: Any, hostname: str) -> Any:
    """Stamp a USB entry with its source host (mirrors the legacy tagged_usb_state)."""
    if isinstance(item, dict):
        item = dict(item)
        item.setdefault("_agent_hostname", hostname)
        return item
    return item


class ProxmoxDeploy:
    """Owns the cs spoke's per-host Proxmox state and the relay payload builder.

    One ``ProxmoxDeploy`` instance lives on the ``CSSpoke`` module; the cs
    control plane's telemetry relay loop calls ``relay_payload`` on a timer and
    the ``CS_INGEST_*`` handlers call ``ingest_*`` as agent events arrive.
    """

    # Keys exported into the per-host ``proxmox`` summary (subset of the legacy
    # ``proxmox`` relay block; fields with no D1 source are defaulted).
    _SUMMARY_KEYS = (
        "connected", "last_seen", "node", "vm_count", "running_count", "vms",
        "usb_state", "present_usb", "unknown_usb", "usb_count", "agent_version",
        "pve_version", "provision_halt", "template_lock", "vmid_range",
        "vm_set_override", "effective_vm_set", "provision",
        # Rolling 1h averages (computed from per-host sample rings); the VM
        # Server Details header renders px.cpu_1h_avg / px.mem_1h_avg, falling
        # back to "—" when None. Projected via _SUMMARY_KEYS so they relay to
        # the hub cache alongside the rest of the per-host ``proxmox`` block.
        "cpu_1h_avg", "mem_1h_avg",
    )

    def __init__(self) -> None:
        # hostname → per-host state entry (see ingest_telemetry for the shape).
        self.proxmox_states: Dict[str, Dict[str, Any]] = {}
        # Rolling event buffers per host (Phase E fills these; D1 keeps empty).
        self.events: Dict[str, List[Dict[str, Any]]] = {}
        # Per-host resource-sample rings → cpu_1h_avg / mem_1h_avg. Each sample
        # is (timestamp, value); pruned to _RESOURCE_SAMPLE_WINDOW on every
        # ingest so the mean always reflects the last hour. In-memory (matches
        # proxmox_states); a spoke restart resets the window, which is fine —
        # the averages repopulate from the next telemetry frame within ~10s.
        self.cpu_samples: Dict[str, List[Tuple[float, float]]] = {}
        self.mem_samples: Dict[str, List[Tuple[float, float]]] = {}

    # ── ingest ───────────────────────────────────────────────────────────────

    def ingest_telemetry(self, hostname: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a CS_TELEMETRY frame to ``proxmox_states[hostname]``.

        Ports the essential parts of ``_apply_proxmox_telemetry_state``
        (server.py:9667-9787): enrich VMs with ``_agent_hostname`` + template
        flag, normalize USB state, compute counts, and store the host summary.
        Returns the stored entry.
        """
        if not hostname:
            logger.warning("CS_INGEST_TELEMETRY: missing hostname — dropping")
            return {}
        body = body or {}
        now = time.time()

        raw_vms = body.get("vms", []) or []
        node = body.get("node", {}) or {}
        usb_state = body.get("usb_state", []) or []
        present_usb = body.get("present_usb", []) or []
        unknown_usb = body.get("unknown_usb", []) or []

        enriched_vms: List[Dict[str, Any]] = []
        for v in raw_vms:
            v = dict(v or {})
            v["_agent_hostname"] = hostname
            if "is_template" not in v:
                v["is_template"] = _is_template(v)
            # Relay-payload VM fields the UI expects (server.py:6080-6093); the
            # agent already supplies most; default the cs-specific ones.
            v.setdefault("has_usb_config", False)
            v.setdefault("reclone_bus_path", None)
            v.setdefault("pci_passthrough_addrs", [])
            v.setdefault("prov_status", "")
            enriched_vms.append(v)

        non_template = [v for v in enriched_vms if not v.get("is_template")]
        running = [v for v in non_template if str(v.get("status", "")).lower() == "running"]

        vr = body.get("vmid_range")
        vmid_range = None
        if isinstance(vr, dict):
            try:
                vmid_range = {"start": int(vr.get("start", 0) or 0),
                              "end":   int(vr.get("end", 0) or 0)}
            except (TypeError, ValueError):
                vmid_range = None

        entry: Dict[str, Any] = {
            "connected":        True,
            "last_seen":        now,
            "agent_version":    _s(body.get("agent_version")),
            "pve_version":      _s(body.get("pve_version")),
            "vm_count":         len(non_template),
            "running_count":    len(running),
            "usb_count":        len(usb_state),
            "node":             node,
            "provision_halt":   bool(body.get("provision_halt")),
            "template_lock":    _s(body.get("template_lock")),
            "vmid_range":       vmid_range,
            "vm_set_override":  body.get("vm_set_override", 0),
            "effective_vm_set": max(1, int(body.get("effective_vm_set", 1) or 1)),
            "vms":              enriched_vms,
            "usb_state":        [_tag_usb(u, hostname) for u in usb_state],
            "present_usb":      [_tag_usb(u, hostname) for u in present_usb],
            "unknown_usb":      [_tag_usb(u, hostname) for u in unknown_usb],
            # Auto-provision diagnostic from the pxmx agent (cs_enabled,
            # loop_running heartbeat, auto_provision_on, reason, halt, config
            # snapshot). Projected into the per-host ``proxmox`` block via
            # _SUMMARY_KEYS so the hub cache → /usb-provisioning-status → WebUI
            # Auto-Provisioning card can show WHY nothing provisions.
            "provision":        body.get("provision") or {},
        }
        # Roll a CPU + mem sample from this frame's node block, then read the
        # 1h averages back into the entry so they ride the relay payload's
        # per-host ``proxmox`` summary (Details header CPU 1h / Mem 1h).
        self._record_resource_samples(hostname, node, now)
        entry["cpu_1h_avg"] = self._resource_1h_average(self.cpu_samples.get(hostname, []))
        entry["mem_1h_avg"] = self._resource_1h_average(self.mem_samples.get(hostname, []))
        self.proxmox_states[hostname] = entry
        logger.debug("CS_INGEST_TELEMETRY: %s — %d VMs (%d running, %d templates), %d USB",
                     hostname, len(enriched_vms), len(running),
                     len(enriched_vms) - len(non_template), len(usb_state))
        return entry

    # ── resource samples (cpu_1h_avg / mem_1h_avg) ───────────────────────────

    def _record_resource_samples(self, hostname: str, node: Dict[str, Any],
                                 now: float) -> None:
        """Append a CPU and a memory sample from this frame's ``node`` block.

        Ports ``_record_resource_samples`` (legacy server.py:3966). CPU is
        ``node.cpu_percent`` directly; mem is ``mem_used_kb / mem_total_kb * 100``
        (a percentage, so hosts with different RAM compare on the same axis).
        Each ring is pruned to ``_RESOURCE_SAMPLE_WINDOW`` on every append so
        the mean always reflects the last hour. Best-effort: a missing/zero
        field just skips that sample rather than raising.
        """
        if not hostname or not isinstance(node, dict):
            return
        cutoff = now - _RESOURCE_SAMPLE_WINDOW
        cpu_pct = node.get("cpu_percent")
        if cpu_pct is not None:
            try:
                ring = self.cpu_samples.setdefault(hostname, [])
                ring.append((now, float(cpu_pct)))
                ring[:] = [(ts, v) for ts, v in ring if ts >= cutoff]
            except (TypeError, ValueError):
                pass
        mem_used = node.get("mem_used_kb")
        mem_total = node.get("mem_total_kb")
        try:
            if mem_used is not None and mem_total:
                mem_total_f = float(mem_total)
                if mem_total_f > 0:
                    mem_pct = (float(mem_used) / mem_total_f) * 100.0
                    ring = self.mem_samples.setdefault(hostname, [])
                    ring.append((now, mem_pct))
                    ring[:] = [(ts, v) for ts, v in ring if ts >= cutoff]
            else:
                self.mem_samples.setdefault(hostname, [])
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    @staticmethod
    def _resource_1h_average(samples: List[Tuple[float, float]]) -> Optional[float]:
        """Rolling mean of all samples within the last hour, or None if none.

        Ports ``_resource_1h_average`` (legacy server.py:3940). Returns the
        average of whatever samples exist as soon as the first arrives — no
        warm-up delay; None only when no sample has been recorded yet (which
        is what the UI renders as "—").
        """
        if not samples:
            return None
        cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
        recent = [v for ts, v in samples if ts >= cutoff]
        return (sum(recent) / len(recent)) if recent else None

    # ── relay payload ────────────────────────────────────────────────────────

    def _host_summary(self, st: Dict[str, Any]) -> Dict[str, Any]:
        """The per-host ``proxmox`` block the VM Server view reads (vm_count/
        usb_count come from here). Falls back to per-entry keys for any field
        a host hasn't reported yet."""
        return {k: st.get(k) for k in self._SUMMARY_KEYS}

    def relay_payload(self, spoke_id: str, spoke_name: str = "") -> Dict[str, Any]:
        """Build the CS_TELEMETRY frame the cs spoke pushes to the hub.

        Ports ``_build_relay_telemetry_payload`` (server.py:6011-6161) at the
        D1 level: a backward-compatible single-host ``proxmox``/``proxmox_vms``/
        ``usb_devices`` shape (populated from the freshest host) PLUS a
        ``proxmox_hosts`` list (one entry per known host) that the VM Server
        view expands into one row per Proxmox host. Stale hosts (no frame in
        ``STALE_SECS``) are marked disconnected but retained so a briefly-offline
        agent keeps its row until it reconnects or ages out.
        """
        STALE_SECS = 180
        now = time.time()
        hosts: List[Dict[str, Any]] = []
        all_vms: List[Dict[str, Any]] = []
        all_usb: List[Any] = []
        freshest: Optional[Dict[str, Any]] = None

        for hn, st in self.proxmox_states.items():
            st = st or {}
            age = now - float(st.get("last_seen", 0) or 0)
            connected = bool(st.get("connected")) and age <= STALE_SECS
            summary = self._host_summary(st)
            summary["connected"] = connected
            vms = st.get("vms", []) or []
            usb = st.get("usb_state", []) or []
            all_vms.extend(vms)
            all_usb.extend(usb)
            hosts.append({
                "hostname":      hn,
                "proxmox":       summary,
                "proxmox_vms":   vms,
                "usb_devices":   usb,
                "reclone_state": {},
            })
            if freshest is None or float(st.get("last_seen", 0) or 0) > \
                    float((freshest or {}).get("last_seen", 0) or 0):
                freshest = {**st, "last_seen": st.get("last_seen", 0)}

        # Backward-compatible single-host block (freshest host), so legacy
        # single-row readers and the new per-host reader both work.
        primary = self._host_summary(freshest) if freshest else {}
        primary_host = (freshest or {}).get("node", {}) or {}
        primary_hostname = ""
        if freshest:
            primary_hostname = (freshest.get("node", {}) or {}).get("hostname") or \
                next(iter(self.proxmox_states), "")

        payload: Dict[str, Any] = {
            "spoke_id":    spoke_id,
            "spoke_name":  spoke_name or spoke_id,
            "hostname":    primary_hostname,
            "timestamp":   now,
            "proxmox":     primary,
            "proxmox_vms": all_vms,
            "usb_devices": all_usb,
            "proxmox_hosts": hosts,
            "reclone_state": {},
            "api_server":  {},
            "central":     {},
            # dnsmasq DHCP-server status for the isolated sim-client network
            # (provisioned by install_cs.sh on the spoke's second NIC). Cheap,
            # defensive probe — never raises; degrades to {installed: False}
            # when dnsmasq isn't configured. Read by the hub's
            # /sim/api/superadmin/dhcp-status route → Setup → Simulations card.
            "dhcp":        collect_dhcp_status(),
        }
        return payload

    # ── event ingest stubs (Phase E fills these) ─────────────────────────────

    def ingest_event(self, hostname: str, kind: str, data: Dict[str, Any]) -> None:
        """Store a CS_LOG / CS_PROGRESS / CS_WATCHDOG_EVENT / CS_HW_RESET_EVENT /
        CS_COMMAND_RESULT into the per-host event buffer. Five kinds: ``log``,
        ``progress``, ``watchdog_event``, ``hw_reset``, ``command_result``.
        Recorded best-effort; ``command_result`` is ALSO used by the cs spoke to
        close the deferred long-op ack loop (``CS_ACK_COMMAND``), and Phase E
        wires the buffered events into the broadcaster for live UI updates."""
        if not hostname:
            return
        buf = self.events.setdefault(hostname, [])
        buf.append({"ts": time.time(), "kind": kind, "data": data or {}})
        # keep the last 200 per host
        del buf[:-200]