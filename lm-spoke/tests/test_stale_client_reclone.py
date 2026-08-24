"""stale_client_reclone — a VM Proxmox reports RUNNING but whose sim client
has stopped reporting to the API gets reclonded. Distinct from guest_watchdog
(QGA-only, pxmx agent) and the dongle-health ladder (T2-only, and never
escalates a no_gateway infra fault) — see the module docstring.

This is an unattended, machine-driven, destructive (destroy+reclone) code
path, so its guardrails get direct coverage rather than narrative claims in
a docstring:
  1. Running VM + client silent past the threshold -> reclone dispatched.
  2. Running VM + client silent but INSIDE the threshold -> not touched.
  3. Running VM + client NEVER reported (no last_seen at all): the VM/client
     gap. The clock starts on first sight; it is recloned only after the
     (longer) grace window AND only when correlation is proven (some OTHER
     VM's client IS reporting). Without correlation it is left alone.
  3b. Never-reported reclone is capped per sweep and its per-vmid grace clock
     persists across a restart (can't be reset to "act immediately").
  4. VM not running -> not touched, regardless of client staleness.
  5. Cooldown: a vmid already triggered recently is not re-triggered even if
     it's still stale on the next sweep (would otherwise loop forever on a
     VM that's mid-settle from the reclone this loop itself just kicked off).
  6. Agent not connected for that host -> skipped (never dispatch to a dead
     agent), and does not consume/poison the cooldown for a legitimate later
     retry once the agent reconnects.
  7. Cooldown state persists across a fresh instance pointed at the same
     data_dir (survives a spoke restart mid-settle-window).
"""
import asyncio
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stale_client_reclone import (  # noqa: E402
    StaleClientReclone, _STALE_RECLONE_S, _RECLONE_COOLDOWN_S,
    _NEVER_REPORTED_GRACE_S, _NEVER_REPORTED_MAX_PER_SWEEP,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Registry:
    def __init__(self, clients):
        self._c = clients

    def get_all(self):
        return self._c


class _Deploy:
    def __init__(self, states):
        self.proxmox_states = states


class _ControlPlane:
    def __init__(self, connected_agents):
        self.connected_agents = connected_agents
        self.sent = []  # [(command, data, agent_id)]

    async def send_to_agent(self, command, data, agent_id=None, timeout=None):
        self.sent.append((command, dict(data), agent_id))
        return {"status": "ACCEPTED"}


class _Spoke:
    def __init__(self, states, clients, connected_agents):
        self.deploy = _Deploy(states)
        self.registry = _Registry(clients)
        self.control_plane = _ControlPlane(connected_agents)


def _vm(vmid, name, status="running", is_template=False, vm_type="qemu"):
    return {"vmid": vmid, "name": name, "status": status,
           "is_template": is_template, "type": vm_type}


def _client(hostname, age_s):
    return {"hostname": hostname,
           "last_seen": (time.time() - age_s) if age_s is not None else None}


_AGENTS = {"agent-1": {"hostname": "pxmx-cs-svr-02"}}
_STATES_ONE_VM_KEY = "pxmx-cs-svr-02"


def _sweep(spoke, tmp_path):
    inst = StaleClientReclone(spoke, tmp_path)
    _run(inst._sweep())
    return inst


def test_running_vm_silent_past_threshold_triggers_reclone(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S + 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == [("reclone_vm", {"vmid": 90046}, "agent-1")]


def test_running_vm_silent_inside_threshold_not_touched(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S - 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == []


def test_client_never_reported_starts_clock_but_not_immediate(tmp_path):
    # No last_seen at all -> the VM/client gap. First sight only STARTS the
    # per-vmid grace clock; nothing is recloned yet even though a reporting
    # peer proves correlation.
    states = {_STATES_ONE_VM_KEY: {"vms": [
        _vm(90046, "qpeterson"),           # never reported
        _vm(90047, "qhealthy"),            # reporting peer -> correlation proven
    ]}}
    clients = {"qpeterson": _client("qpeterson", None),
              "qhealthy": _client("qhealthy", 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == []
    assert "90046" in inst._missing_since, "grace clock must start on first sight"


def test_never_reported_reclones_after_grace_when_correlated(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [
        _vm(90046, "qpeterson"),           # never reported, past grace (seeded)
        _vm(90047, "qhealthy"),            # reporting peer -> correlation proven
    ]}}
    clients = {"qpeterson": _client("qpeterson", None),
              "qhealthy": _client("qhealthy", 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = StaleClientReclone(spoke, tmp_path)
    # Simulate the grace window having already elapsed for the dead VM.
    inst._missing_since["90046"] = time.time() - _NEVER_REPORTED_GRACE_S - 60
    _run(inst._sweep())
    assert spoke.control_plane.sent == [("reclone_vm", {"vmid": 90046}, "agent-1")]


def test_never_reported_not_touched_without_correlation(tmp_path):
    # NO VM anywhere is reporting -> correlation unproven (could be a fleet-wide
    # telemetry glitch or a global name mismatch) -> never-reported VMs are left
    # alone even past grace, so we can't mass-reclone the pool.
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", None)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = StaleClientReclone(spoke, tmp_path)
    inst._missing_since["90046"] = time.time() - _NEVER_REPORTED_GRACE_S - 60
    _run(inst._sweep())
    assert spoke.control_plane.sent == []


def test_never_reported_reclone_capped_per_sweep(tmp_path):
    # Many dead VMs all past grace + one reporting peer -> at most the cap is
    # dispatched in a single sweep (bleed off slowly, never storm).
    dead = [_vm(90100 + i, "qdead%d" % i) for i in range(_NEVER_REPORTED_MAX_PER_SWEEP + 3)]
    states = {_STATES_ONE_VM_KEY: {"vms": dead + [_vm(90047, "qhealthy")]}}
    clients = {"qhealthy": _client("qhealthy", 60)}
    for v in dead:
        clients[v["name"]] = _client(v["name"], None)
    spoke = _Spoke(states, clients, _AGENTS)
    inst = StaleClientReclone(spoke, tmp_path)
    for v in dead:
        inst._missing_since[str(v["vmid"])] = time.time() - _NEVER_REPORTED_GRACE_S - 60
    _run(inst._sweep())
    assert len(spoke.control_plane.sent) == _NEVER_REPORTED_MAX_PER_SWEEP


def test_never_reported_grace_clock_persists_across_restart(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [
        _vm(90046, "qpeterson"), _vm(90047, "qhealthy")]}}
    clients = {"qpeterson": _client("qpeterson", None),
              "qhealthy": _client("qhealthy", 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = _sweep(spoke, tmp_path)          # first sight -> clock started + saved
    started = inst._missing_since.get("90046")
    assert started is not None
    # A brand-new instance (spoke restart) must LOAD the persisted clock rather
    # than reset it to now (which would delay the reclone forever).
    spoke2 = _Spoke(states, clients, _AGENTS)
    inst2 = StaleClientReclone(spoke2, tmp_path)
    assert inst2._missing_since.get("90046") == started


def test_never_reported_clock_cleared_when_client_starts_reporting(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    # Client now reports fresh -> the stale missing-clock must be cleared so a
    # later silence starts fresh, not from the old (already-elapsed) timestamp.
    clients = {"qpeterson": _client("qpeterson", 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = StaleClientReclone(spoke, tmp_path)
    inst._missing_since["90046"] = time.time() - _NEVER_REPORTED_GRACE_S - 60
    _run(inst._sweep())
    assert spoke.control_plane.sent == []
    assert "90046" not in inst._missing_since


def test_vm_not_running_is_not_touched_regardless_of_staleness(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson", status="stopped")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S + 3600)}
    spoke = _Spoke(states, clients, _AGENTS)
    _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == []


def test_cooldown_prevents_immediate_retrigger(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S + 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    inst = StaleClientReclone(spoke, tmp_path)
    _run(inst._sweep())
    assert len(spoke.control_plane.sent) == 1
    # Same spoke instance, same instance of the checker (mirrors a second
    # sweep tick 5 min later) — still stale, but within the cooldown window.
    _run(inst._sweep())
    assert len(spoke.control_plane.sent) == 1, "must not re-trigger within the cooldown"


def test_disconnected_agent_is_skipped_and_not_cooled_down(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S + 60)}
    spoke = _Spoke(states, clients, connected_agents={})  # no agent connected
    inst = _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == []
    assert inst._triggered == {}, "a skipped VM must not consume the cooldown"


def test_cooldown_state_persists_across_a_fresh_instance(tmp_path):
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", _STALE_RECLONE_S + 60)}
    spoke = _Spoke(states, clients, _AGENTS)
    _sweep(spoke, tmp_path)
    assert len(spoke.control_plane.sent) == 1
    # Simulate a spoke restart: a BRAND NEW StaleClientReclone instance
    # pointed at the same data_dir must load the persisted cooldown and
    # still refuse to re-trigger immediately.
    spoke2 = _Spoke(states, clients, _AGENTS)
    _sweep(spoke2, tmp_path)
    assert spoke2.control_plane.sent == []


def test_cooldown_value_covers_the_post_clone_settle_window():
    # Sanity pin: the cooldown must comfortably outlast auto-prov's own
    # post-clone settle+reboot window (documented elsewhere as running to
    # ~15-20 min) or a freshly reclonded VM gets reclonded again before its
    # own client has had a fair chance to boot and report.
    assert _RECLONE_COOLDOWN_S >= 30 * 60.0
