"""Command-queue safeguard and ownership at the queue level.

The enqueue safeguard checks VM ids for every target (not only "proxmox"),
reads ``vm_id`` as well as ``vmid``, checks every id of a ``delete_vms`` and
refuses ids that are not whole numbers. ``fleet=False`` polls and acks (sim
clients) only ever see their own sim-client commands; the hub's Proxmox
agent poll (``fleet=True``, the default) is unchanged.
"""

import asyncio
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


@pytest.fixture
def q(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    loop = asyncio.new_event_loop()
    queue = CommandQueue(data, CSSettings(data, CONFIGS))
    queue.run = lambda coro: loop.run_until_complete(coro)
    try:
        yield queue
    finally:
        loop.close()


@pytest.mark.parametrize("target,action,args", [
    ("pve1", "start_vm", {"vmid": 100}),
    ("pve1", "delete_vm", {"vmid": 1001}),
    ("proxmox", "stop_vm", {"vm_id": 100}),
    ("proxmox", "reboot_vm", {"vmid": "abc"}),
    ("proxmox", "reboot_vm", {"vmid": True}),
    ("proxmox", "reboot_vm", {"vmid": 91000.5}),
    ("pve1", "delete_vms", {"vmids": [91000, 100]}),
    ("pve1", "delete_vms", {"vm_ids": [91000, 1001]}),
    ("pve1", "delete_vms", {"vmids": [91000, "x"]}),
    ("pve1", "delete_vms", {"vmids": "100"}),
    ("proxmox", "delete_vm", {"vmid": None, "vm_id": 1001}),
    ("proxmox", "delete_vm", {"vmid": "", "vm_id": 1001}),
    ("proxmox", "delete_vm", {"vmid": 91000, "vm_id": 1001}),
    ("pve1", "delete_vms", {"vmids": [], "vm_ids": [1001]}),
    ("pve1", "delete_vms", {"vmids": None, "vm_ids": [1001]}),
    ("pve1", "delete_vms", {"vmids": [91000], "vm_ids": "1001"}),
])
def test_safeguard_refuses(q, target, action, args):
    with pytest.raises(ValueError):
        q.run(q.enqueue(target, action, args))


@pytest.mark.parametrize("target,action,args", [
    ("pve1", "start_vm", {"vmid": 91000}),
    ("proxmox", "start_vm", {"vmid": "91000"}),
    ("proxmox", "delete_vm", {"vm_id": 91001}),
    ("proxmox", "delete_vm", {}),
    ("pve1", "delete_vms", {"vmids": [91000, "91001"]}),
    ("sim-1", "reboot", {"vmid": 5}),
    ("proxmox", "delete_vm", {"vmid": None, "vm_id": 91001}),
    ("pve1", "delete_vms", {"vmids": [], "vm_ids": [91000]}),
])
def test_safeguard_allows(q, target, action, args):
    assert q.run(q.enqueue(target, action, args))["created"] is True


def test_fleet_poll_unchanged_sim_poll_filtered(q):
    """The hub's Proxmox agent poll still gets "proxmox" and its own VM
    commands; a sim-client poll for the same hostname gets neither."""
    q.run(q.enqueue("proxmox", "start_vm", {"vmid": 91000}))
    q.run(q.enqueue("pve1", "delete_vm", {"vmid": 91001}))
    q.run(q.enqueue("pve1", "reboot", {}))
    sim = q.run(q.poll_agent_inbox("pve1", fleet=False))
    assert [c["action"] for c in sim["commands"]] == ["reboot"]
    fleet = q.run(q.poll_agent_inbox("pve1"))
    assert sorted(c["action"] for c in fleet["commands"]) == ["delete_vm", "start_vm"]


def test_sim_ack_only_own_sim_command(q):
    """A non-fleet ack of a Proxmox command, or of another host's command,
    is "not found"; the hub's fleet ack is unchanged."""
    pve = q.run(q.enqueue("proxmox", "delete_vm", {"vmid": 91000}))["command"]
    own = q.run(q.enqueue("sim-1", "reboot", {}))["command"]
    for cid, host in ((pve["id"], "sim-1"), (pve["id"], "proxmox"), (own["id"], "sim-2")):
        res = q.run(q.ack_command(cid, "completed", hostname=host, fleet=False))
        assert res == {"ok": False, "message": "Command not found"}
    assert q.run(q.ack_command(own["id"], "completed", hostname="SIM-1.", fleet=False))["ok"]
    assert q.run(q.ack_command(pve["id"], "completed"))["ok"]
