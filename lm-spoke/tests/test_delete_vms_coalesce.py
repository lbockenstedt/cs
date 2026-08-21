"""Coalescing contract for the ``CS_QUEUE_COMMAND`` bulk (``items``) path.

The mass-delete UI sends one request per spoke carrying an ``items`` list. To
stop the "only some VMs deleted" drop (N per-VM ``delete_vm`` commands each
racing the relay ACCEPT window on a saturated host), the spoke coalesces every
``delete_vm`` item into ONE ``delete_vms`` batch PER HOST, carrying that host's
vmid list. Non-delete items still enqueue individually.

These lock in:
  * a same-host delete batch → a single ``delete_vms`` command with all vmids;
  * cross-host deletes → one ``delete_vms`` per host (VMIDs route to their host);
  * a mixed batch → deletes coalesced, other actions enqueued as-is;
  * the reported ``queued`` stays a VM count (so the UI toast is correct).
"""
import asyncio

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke
import command_handlers.handlers_ingest as ingest
from pathlib import Path

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """push_pending live-delivers over the WS — stub it (no agent here)."""
    async def _noop(spoke, target):
        return None
    monkeypatch.setattr(ingest.client_api, "push_pending", _noop)


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data, CONFIGS)
    s.registry = ClientRegistry(data)
    s.queue = CommandQueue(data, s.settings)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _dispatch(spoke, loop, items):
    return loop.run_until_complete(
        spoke._dispatch_ingest("CS_QUEUE_COMMAND", {"items": items}))


def _cmds(spoke, loop):
    return loop.run_until_complete(spoke.queue.list_commands())


def _del(vmid, target="proxmox"):
    return {"action": "delete_vm", "args": {"vmid": vmid}, "target": target}


def test_same_host_deletes_coalesced_into_one_delete_vms(spoke_loop):
    spoke, loop = spoke_loop
    res = _dispatch(spoke, loop, [_del(90001), _del(90002), _del(90003)])
    cmds = _cmds(spoke, loop)
    dv = [c for c in cmds if c["action"] == "delete_vms"]
    assert len(dv) == 1                                   # ONE batch, not 3 commands
    assert not [c for c in cmds if c["action"] == "delete_vm"]
    assert sorted(dv[0]["args"]["vmids"]) == [90001, 90002, 90003]
    assert res["queued"] == 3                             # UI toast still VM count


def test_cross_host_deletes_one_batch_per_host(spoke_loop):
    spoke, loop = spoke_loop
    _dispatch(spoke, loop, [_del(90001, "host-a"), _del(90002, "host-a"),
                            _del(90003, "host-b")])
    dv = [c for c in _cmds(spoke, loop) if c["action"] == "delete_vms"]
    by_target = {c["target"]: sorted(c["args"]["vmids"]) for c in dv}
    assert by_target == {"host-a": [90001, 90002], "host-b": [90003]}


def test_mixed_batch_coalesces_deletes_keeps_others(spoke_loop):
    spoke, loop = spoke_loop
    items = [_del(90001), _del(90002),
             {"action": "stop_vm", "args": {"vmid": 90003}, "target": "proxmox"}]
    _dispatch(spoke, loop, items)
    cmds = _cmds(spoke, loop)
    dv = [c for c in cmds if c["action"] == "delete_vms"]
    sv = [c for c in cmds if c["action"] == "stop_vm"]
    assert len(dv) == 1 and sorted(dv[0]["args"]["vmids"]) == [90001, 90002]
    assert len(sv) == 1 and sv[0]["args"]["vmid"] == 90003
