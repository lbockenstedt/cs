"""Command-queue admin ops: clear-all / per-target expiry / per-row delete.

The hub's ``DELETE /sim/api/{t}/proxmx/commands`` (Clear Queue) forwards
``CS_CLEAR_COMMANDS``, and per-row delete forwards ``CS_DELETE_COMMAND``. The
``CS_CLEAR_COMMANDS`` handler MUST sit before the NOT_IMPLEMENTED matcher (whose
set includes ``"CLEAR"``) or the Clear-queue button hits a dead command; the
pre-teardown-expiry path (``DELETE .../commands/pending?target=``) reuses
``CS_CLEAR_COMMANDS`` scoped to one target so in-flight commands don't fire
against a gone VM.
"""

import asyncio
import json
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    s.registry = ClientRegistry(data_dir)
    s.queue = CommandQueue(data_dir, s.settings)
    return s, loop


def _run(loop, coro):
    return loop.run_until_complete(coro)


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    s, loop = _make_spoke(data, CONFIGS)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _enqueue(queue, loop, target, action, args=None):
    return _run(loop, queue.enqueue(target, action, args or {}))


def test_clear_commands_expires_non_terminal_leaves_terminal(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    c1 = _enqueue(q, loop, "proxmox", "start_vm", {"vmid": 90050})["command"]
    c2 = _enqueue(q, loop, "proxmox", "stop_vm", {"vmid": 90051})["command"]
    # Mark one terminal (simulating an ack) — it must survive a clear.
    _run(loop, q.ack_command(c2["id"], "completed", "ok"))
    res = _run(loop, q.clear_commands())
    assert res["cleared"] == 1  # only the pending start_vm expired
    statuses = {c["id"]: c["status"] for c in _run(loop, q.list_commands())}
    assert statuses[c1["id"]] == "expired"
    assert statuses[c2["id"]] == "completed"  # terminal retained


def test_clear_commands_scoped_to_target(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    a = _enqueue(q, loop, "proxmox", "start_vm", {"vmid": 90050})["command"]
    b = _enqueue(q, loop, "host-b", "start_vm", {"vmid": 90051})["command"]
    res = _run(loop, q.clear_commands("proxmox"))
    assert res["cleared"] == 1
    statuses = {c["id"]: c["status"] for c in _run(loop, q.list_commands())}
    assert statuses[a["id"]] == "expired"
    assert statuses[b["id"]] == "pending"  # other target untouched


def test_delete_command_removes_one(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "proxmox", "reboot_vm", {"vmid": 90050})["command"]
    res = _run(loop, q.delete_command(c["id"]))
    assert res["ok"] is True
    assert res["removed"] == 1
    ids = [x["id"] for x in _run(loop, q.list_commands())]
    assert c["id"] not in ids


def test_delete_command_missing_id(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    res = _run(loop, q.delete_command("nope"))
    assert res["ok"] is False


def test_cs_clear_commands_handler_is_not_not_implemented(spoke_loop):
    """The Clear-queue button forwards CS_CLEAR_COMMANDS; without a handler
    before the matcher the spoke returned NOT_IMPLEMENTED (dead button)."""
    spoke, loop = spoke_loop
    _enqueue(spoke.queue, loop, "proxmox", "start_vm", {"vmid": 90050})
    _enqueue(spoke.queue, loop, "proxmox", "stop_vm", {"vmid": 90051})
    resp = _run(loop, spoke.handle_command("CS_CLEAR_COMMANDS", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 2


def test_cs_clear_commands_handler_scoped_target(spoke_loop):
    spoke, loop = spoke_loop
    _enqueue(spoke.queue, loop, "proxmox", "start_vm", {"vmid": 90050})
    _enqueue(spoke.queue, loop, "host-b", "start_vm", {"vmid": 90051})
    resp = _run(loop, spoke.handle_command("CS_CLEAR_COMMANDS", {"target": "proxmox"}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 1


def test_cs_delete_command_handler_removes_row(spoke_loop):
    spoke, loop = spoke_loop
    c = _enqueue(spoke.queue, loop, "proxmox", "reclone_vm", {"vmid": 90050})["command"]
    resp = _run(loop, spoke.handle_command("CS_DELETE_COMMAND", {"id": c["id"]}))
    assert resp["status"] == "SUCCESS"
    assert resp["removed"] == 1
    ids = [x["id"] for x in _run(loop, spoke.queue.list_commands())]
    assert c["id"] not in ids


def test_cs_delete_command_handler_missing_returns_error(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_DELETE_COMMAND", {"id": "nope"}))
    assert resp["status"] == "ERROR"