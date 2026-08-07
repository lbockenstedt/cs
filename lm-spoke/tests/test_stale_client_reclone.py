"""stale_client_reclone — a VM Proxmox reports RUNNING but whose sim client
has stopped reporting to the API gets reclonded. Distinct from guest_watchdog
(QGA-only, pxmx agent) and the dongle-health ladder (T2-only, and never
escalates a no_gateway infra fault) — see the module docstring.

This is an unattended, machine-driven, destructive (destroy+reclone) code
path, so its guardrails get direct coverage rather than narrative claims in
a docstring:
  1. Running VM + client silent past the threshold -> reclone dispatched.
  2. Running VM + client silent but INSIDE the threshold -> not touched.
  3. Running VM + client NEVER reported (no last_seen at all) -> not touched
     (a different problem — hostname/rename mismatch, not this loop's job).
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

from stale_client_reclone import StaleClientReclone, _STALE_RECLONE_S, _RECLONE_COOLDOWN_S  # noqa: E402


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


def test_client_never_reported_is_not_touched(tmp_path):
    # No last_seen at all -> a hostname/rename mismatch problem, not this
    # loop's job (would otherwise reclone-loop a VM whose sim never started).
    states = {_STATES_ONE_VM_KEY: {"vms": [_vm(90046, "qpeterson")]}}
    clients = {"qpeterson": _client("qpeterson", None)}
    spoke = _Spoke(states, clients, _AGENTS)
    _sweep(spoke, tmp_path)
    assert spoke.control_plane.sent == []


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
