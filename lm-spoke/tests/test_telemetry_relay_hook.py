"""The CS telemetry relay lives on the ``CSSpoke`` MODULE (``create_spoke_tasks``
+ ``_cs_telemetry_relay_loop`` + ``_relay_content_sig``), not on the standalone
control plane, so it runs identically whether driven by the standalone
``CSControlPlane`` OR hosted as a role by the generic multi-role agent
(``RoleConnection``). Before this move a role-hosted simulation spoke never
emitted ``CS_TELEMETRY`` and was invisible in the hub's Simulations view.

These tests pin: (1) the module exposes ``create_spoke_tasks`` returning the
relay task, (2) ``CSControlPlane._create_spoke_tasks`` delegates to that hook,
and (3) the moved ``_relay_content_sig`` static helper still hashes state the
same change-sensitive way.
"""
import asyncio

from cs_spoke import CSSpoke


class _FakeCP:
    """Minimal control plane the relay loop reads through ``self.control_plane``."""
    def __init__(self, spoke_id="agent-cs-test-simulation"):
        self.spoke_id = spoke_id
        self._relay_wake = None
        self._draining = False

    def _encode_frame(self, msg):
        return msg

    def _bp_send_interval(self, interval):
        return interval


def test_module_create_spoke_tasks_returns_relay_task():
    async def _run():
        s = CSSpoke("test-cs", {})
        s.control_plane = _FakeCP()
        tasks = s.create_spoke_tasks(websocket=object())
        assert isinstance(tasks, list) and len(tasks) == 1
        assert isinstance(tasks[0], asyncio.Task)
        for t in tasks:
            t.cancel()
        # Drain the cancellation so pytest doesn't warn about pending tasks.
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
    asyncio.run(_run())


def test_control_plane_delegates_to_module_hook():
    """CSControlPlane._create_spoke_tasks must forward to the cs module's hook —
    that single delegation is what keeps standalone + role-hosting in lockstep."""
    from control_plane import CSControlPlane

    class _StubModule:
        def create_spoke_tasks(self, websocket):
            return ["sentinel-task"]

    stub_cp = CSControlPlane.__new__(CSControlPlane)
    stub_cp.modules = {"cs": _StubModule()}
    assert stub_cp._create_spoke_tasks(websocket=object()) == ["sentinel-task"]

    # No cs module registered → empty (never raises).
    stub_cp.modules = {}
    assert stub_cp._create_spoke_tasks(websocket=object()) == []


def _payload(vm_status="running"):
    return {
        "proxmox_hosts": [{
            "proxmox": {"connected": True, "vmid_range": "9000-9099"},
            "proxmox_vms": [{"vmid": 9001, "status": vm_status,
                             "prov_status": "ready", "tags": ["sim"]}],
            "usb_devices": [],
        }],
        "clients": [],
    }


def test_relay_content_sig_stable_and_change_sensitive():
    sig = CSSpoke._relay_content_sig
    a = sig(_payload("running"))
    b = sig(_payload("running"))
    c = sig(_payload("stopped"))
    assert a is not None and a == b        # idle fleet → identical sig (no re-send)
    assert a != c                          # a VM state transition flips the sig
