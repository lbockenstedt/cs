"""
stale_client_reclone.py — auto-reclones a sim VM that Proxmox reports as
RUNNING but whose simulation client has stopped reporting to the API.

This is a distinct failure mode from everything else already watched:

  - guest_watchdog (pxmx agent) pings the QEMU GUEST AGENT (`qm agent ping`)
    — a signal about the GUEST OS, not the sim client PROCESS running inside
    it. A hung/crashed client process with a perfectly responsive OS/QGA
    passes every ping forever; guest_watchdog never sees it, by design (it
    exists specifically for "OS wedged", a different fault).
  - the dongle-health ladder (pxmx agent, T2/USB-dongle VMs only) watches
    per-USB-bus L2/L3 signals, and DELIBERATELY never escalates a
    "no_gateway" state (associated, but no route to the gateway) — treated
    as an infra fault, not a dongle fault, so reboot/reclone is explicitly
    withheld there. It also has no equivalent at all for T1/T3 (PCI
    passthrough) VMs.
  - CS_GET_SIM_TAG_HEALTH (this spoke, handlers_config.py) already computes
    exactly this same "VM running, client hasn't reported recently" fact —
    but only for the diagnostic panel (desired=[] / "client_stale_tags_cleared").
    Nothing ever ACTED on it.

This closes that gap: applies to every tier (whether the client can reach
the API doesn't depend on how its NIC is attached), and reuses the SAME
`reclone_vm` command the manual per-VM Reclone button already sends — no new
pxmx-agent-side code, no new command handler.

Debounce: a freshly (re)cloned VM is expected to be silent for its own
settle window (auto-prov's post-clone settle+reboot contract can run 15+
minutes on its own) — re-triggering on the SAME symptom immediately after a
reclone would just loop forever, destroying and rebuilding the same VM
every sweep. Per-vmid trigger timestamps are persisted to disk (survives a
spoke restart, unlike an in-memory dict) so this can't reset to "act
immediately" right after a restart mid-settle-window.

Only acts on a client that HAS reported before and then went silent — a VM
whose client has NEVER reported (no registry entry at all) is a DIFFERENT
problem (see CS_GET_SIM_TAG_HEALTH's "no_client_matches_vm_name" — usually a
hostname/clone-rename mismatch, which hostname_audit_and_restamp already
owns) and must not be reclone-looped by this check too.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("StaleClientReclone")

_SCAN_INTERVAL_S = 300.0          # 5 min — cheap (local-cache reads only, no
                                  # new agent calls until something is flagged)
_STALE_RECLONE_S = 25 * 60.0      # 25 min of client silence while the VM is
                                  # confirmed running (20-30 min window)
_RECLONE_COOLDOWN_S = 40 * 60.0   # after triggering, don't re-trigger the SAME
                                  # vmid for this long — covers auto-prov's own
                                  # post-clone settle+reboot window plus margin
                                  # for the fresh client's first heartbeat
_VMID_FLOOR = 90000
_STATE_FILE = "stale_client_reclone.json"


class StaleClientReclone:
    """Started by CSControlPlane.run(), same pattern as ClientRegistry.start()
    / DemoManager.start() / CentralPoller.start(). No-op start() if there's no
    running event loop (e.g. a spoke constructed standalone in a unit test)."""

    def __init__(self, spoke, data_dir: Path):
        self.spoke = spoke
        self._path = Path(data_dir) / _STATE_FILE
        self._triggered: Dict[str, float] = self._load()
        self._task = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            pass  # no current event loop

    def _load(self) -> Dict[str, float]:
        try:
            if self._path.exists():
                d = json.loads(self._path.read_text())
                if isinstance(d, dict):
                    return {str(k): float(v) for k, v in d.items()}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._triggered))
        except Exception as e:  # noqa: BLE001
            logger.debug("stale-client-reclone: state save failed: %s", e)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_SCAN_INTERVAL_S)
                await self._sweep()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 — a sweep must not kill the loop
                logger.warning("stale-client-reclone loop error: %s", e)

    async def _sweep(self) -> None:
        spoke = self.spoke
        cp = getattr(spoke, "control_plane", None)
        reg = getattr(spoke, "registry", None)
        deploy = getattr(spoke, "deploy", None)
        if cp is None or reg is None or deploy is None:
            return
        states = getattr(deploy, "proxmox_states", {}) or {}
        agents = getattr(cp, "connected_agents", {}) or {}
        # hostname -> agent_id, CONNECTED agents only — a reclone dispatched to
        # a disconnected agent just fails/times out; skip and let the next
        # sweep retry once it reconnects, rather than queuing/retry-storming.
        by_host: Dict[str, str] = {}
        for aid, info in agents.items():
            hn = str((info or {}).get("hostname") or "").strip()
            if hn:
                by_host[hn] = aid
        clients: Dict[str, Any] = {}
        try:
            for c in (reg.get_all() or {}).values():
                h = str((c or {}).get("hostname") or "").strip().lower()
                if h:
                    clients[h] = c
        except Exception:  # noqa: BLE001
            pass
        now = time.time()
        dirty = False
        for hn, st in states.items():
            agent_id = by_host.get(hn)
            if not agent_id:
                continue
            for vm in (st.get("vms") or []):
                if vm.get("is_template") or vm.get("type") == "lxc":
                    continue
                try:
                    vid = int(vm.get("vmid") or 0)
                except (TypeError, ValueError):
                    continue
                if vid <= _VMID_FLOOR:
                    continue
                if str(vm.get("status") or "").lower() != "running":
                    continue
                name = str(vm.get("name") or "").strip().lower()
                cl = clients.get(name)
                try:
                    ls = float((cl or {}).get("last_seen") or 0)
                except (TypeError, ValueError):
                    ls = 0.0
                if not ls:
                    continue  # never reported at all — a different problem
                             # (hostname/rename mismatch), not this loop's job
                age = now - ls
                if age < _STALE_RECLONE_S:
                    continue
                key = str(vid)
                last_trig = self._triggered.get(key, 0.0)
                if (now - last_trig) < _RECLONE_COOLDOWN_S:
                    continue
                logger.warning(
                    "stale-client-reclone: VM %s (%s) on %s running but client "
                    "silent %.0fs (> %.0fs) — reclone", vid, name, hn, age, _STALE_RECLONE_S)
                self._triggered[key] = now
                dirty = True
                try:
                    await cp.send_to_agent("reclone_vm", {"vmid": vid},
                                           agent_id=agent_id, timeout=60.0)
                except Exception as e:  # noqa: BLE001 — one failed dispatch must
                                         # not stop the sweep covering other VMs
                    logger.warning("stale-client-reclone: reclone dispatch for "
                                   "VM %s failed: %s", vid, e)
        if dirty:
            self._save()
