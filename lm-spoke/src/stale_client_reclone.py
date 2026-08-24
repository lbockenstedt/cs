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

Only acts on a client that HAS reported before and then went silent, AND on a
VM whose client has NEVER reported once it has been running-yet-silent past the
(longer) grace window — both are "the sim isn't running in this VM, rebuild it".
The never-reported path is correlation-gated (it only fires when some OTHER VM's
client IS reporting, proving the VM-name↔hostname match works) and rate-capped,
so a fleet-wide telemetry hiccup or a global name-mismatch can't mass-reclone.
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
# A running sim VM whose client has NEVER checked into the API (no registry row,
# or a row that never got a last_seen) is a DEAD clone — the sim never came up.
# We reclone it too, but only after it has been observed running-yet-silent for
# this GRACE window (longer than the silent-client threshold: a fresh clone gets
# its full auto-prov settle+reboot window AND first-heartbeat margin before we
# assume it's dead). Persisted per-vmid so a spoke restart can't reset the clock
# to "act immediately". Gated on correlation working (some OTHER VM's client IS
# reporting) so a fleet-wide telemetry/name-mismatch glitch can't mass-reclone.
_NEVER_REPORTED_GRACE_S = 45 * 60.0
# Safety cap: at most this many NEVER-reported VMs recloned per sweep, so even if
# the correlation gate is somehow satisfied during a partial outage we bleed off
# dead VMs slowly instead of destroying the whole pool in one tick.
_NEVER_REPORTED_MAX_PER_SWEEP = 3
_VMID_FLOOR = 90000
_STATE_FILE = "stale_client_reclone.json"
_MISSING_STATE_FILE = "stale_client_reclone_missing.json"


class StaleClientReclone:
    """Started by CSControlPlane.run(), same pattern as ClientRegistry.start()
    / DemoManager.start() / CentralPoller.start(). No-op start() if there's no
    running event loop (e.g. a spoke constructed standalone in a unit test)."""

    def __init__(self, spoke, data_dir: Path):
        self.spoke = spoke
        self._path = Path(data_dir) / _STATE_FILE
        self._missing_path = Path(data_dir) / _MISSING_STATE_FILE
        self._triggered: Dict[str, float] = self._load(self._path)
        # {vmid: epoch we FIRST saw this running sim VM with no reporting client}
        self._missing_since: Dict[str, float] = self._load(self._missing_path)
        self._task = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            pass  # no current event loop

    def _load(self, path: Path) -> Dict[str, float]:
        try:
            if path.exists():
                d = json.loads(path.read_text())
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

    def _save_missing(self) -> None:
        try:
            self._missing_path.write_text(json.dumps(self._missing_since))
        except Exception as e:  # noqa: BLE001
            logger.debug("stale-client-reclone: missing-state save failed: %s", e)

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
        missing_dirty = False

        # ── Pass 1: collect the running sim VMs on CONNECTED hosts. ──────────
        # (vid, name, hn, agent_id, last_seen) per VM; also count how many have a
        # client that HAS reported (ls>0) — proof the VM-name↔client-hostname
        # correlation is working, which gates the never-reported reclone below.
        candidates = []
        reporting_count = 0
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
                if ls > 0:
                    reporting_count += 1
                candidates.append((vid, name, hn, agent_id, ls))

        seen_vids = {str(v[0]) for v in candidates}

        async def _reclone(vid, name, hn, agent_id, reason):
            nonlocal dirty
            self._triggered[str(vid)] = now
            dirty = True
            logger.warning("stale-client-reclone: VM %s (%s) on %s %s — reclone",
                           vid, name, hn, reason)
            try:
                await cp.send_to_agent("reclone_vm", {"vmid": vid},
                                       agent_id=agent_id, timeout=60.0)
            except Exception as e:  # noqa: BLE001 — one failed dispatch must not
                                     # stop the sweep covering other VMs
                logger.warning("stale-client-reclone: reclone dispatch for "
                               "VM %s failed: %s", vid, e)

        # ── Pass 2a: clients that REPORTED then went silent (existing path). ─
        never_reported = []
        for vid, name, hn, agent_id, ls in candidates:
            key = str(vid)
            if ls <= 0:
                never_reported.append((vid, name, hn, agent_id))
                continue
            # Reported at least once → not a "never checked in" case; clear any
            # missing-clock it may have carried before its first heartbeat.
            if key in self._missing_since:
                self._missing_since.pop(key, None)
                missing_dirty = True
            age = now - ls
            if age < _STALE_RECLONE_S:
                continue
            if (now - self._triggered.get(key, 0.0)) < _RECLONE_COOLDOWN_S:
                continue
            await _reclone(vid, name, hn, agent_id,
                           "running but client silent %.0fs (> %.0fs)"
                           % (age, _STALE_RECLONE_S))

        # ── Pass 2b: VMs whose client NEVER checked in (the VM/client gap). ──
        # Correlation gate: only act when SOME VM's client is reporting, so a
        # fleet-wide telemetry glitch (every match fails at once) can't wipe the
        # pool. Each dead VM must persist running-yet-silent for the grace window
        # (tracked per-vmid, survives restart) before we assume it's dead.
        if never_reported and reporting_count > 0:
            fired = 0
            for vid, name, hn, agent_id in never_reported:
                key = str(vid)
                first = self._missing_since.get(key)
                if first is None:
                    self._missing_since[key] = now
                    missing_dirty = True
                    continue                       # start the clock; act later
                if (now - first) < _NEVER_REPORTED_GRACE_S:
                    continue
                if (now - self._triggered.get(key, 0.0)) < _RECLONE_COOLDOWN_S:
                    continue
                if fired >= _NEVER_REPORTED_MAX_PER_SWEEP:
                    break                          # bleed off slowly, never storm
                fired += 1
                await _reclone(vid, name, hn, agent_id,
                               "running but client NEVER checked in for %.0fs (> %.0fs)"
                               % (now - first, _NEVER_REPORTED_GRACE_S))

        # Prune missing-clocks for VMs no longer present (deleted/recloned/stopped)
        # so the map can't grow without bound.
        for gone in [k for k in self._missing_since if k not in seen_vids]:
            self._missing_since.pop(gone, None)
            missing_dirty = True

        if dirty:
            self._save()
        if missing_dirty:
            self._save_missing()
