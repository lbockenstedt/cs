"""Command-queue requeue-on-relay-timeout — the "retry 5 then give up" path.

The hub's CSBridgePoller relays a queued command to the agent via SPOKE_RELAY.
When the agent is too busy to ACCEPT within ``CS_RELAY_TIMEOUT_S`` the hub's
``request_response`` returns ``"Timed out waiting for spoke response"``; instead
of acking the command ``failed`` (terminal, the old behavior that killed
mass-deletes on a busy agent), the bridge calls ``CS_REQUEUE_COMMAND`` which
hits ``CommandQueue.requeue_command`` here — reset → ``pending`` (re-send next
tick) up to ``max_retries``, then ``failed``.
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


def _find(q, cid):
    return next((c for c in q.commands if c["id"] == cid), None)


def test_requeue_resets_to_pending_and_increments_attempts(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90075})["command"]
    # Simulate the bridge having polled+delivered it.
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    assert _find(q, c["id"])["status"] == "delivered"
    assert _find(q, c["id"])["relay_attempts"] == 0

    res = _run(loop, q.requeue_command(c["id"], max_retries=5,
                                       message="Timed out waiting for spoke response"))
    assert res["requeued"] is True
    assert res["attempts"] == 1
    assert _find(q, c["id"])["status"] == "pending"
    assert _find(q, c["id"])["relay_attempts"] == 1
    # The timeout message is NOT sticky on a requeue (it's transient); the
    # command stays runnable, not displayed as a failed row.
    assert _find(q, c["id"])["message"] in (None, "")


def test_requeue_exhausts_to_failed_after_max(spoke_loop):
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90076})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    # Re-queue max_retries-1 times (each requeued), then once more → failed.
    for i in range(1, 5):
        res = _run(loop, q.requeue_command(c["id"], max_retries=5, message="relay timed out"))
        assert res["requeued"] is True, f"attempt {i} should requeue"
        assert res["attempts"] == i
    # 5th requeue attempt → exhausted → failed. The bridge passes a message,
    # so the command's failed message is the timeout text (the default
    # "gave up after N" only applies when no message is passed).
    res = _run(loop, q.requeue_command(c["id"], max_retries=5, message="relay timed out"))
    assert res["requeued"] is False
    assert res["attempts"] == 5
    assert res["status"] == "failed"
    cmd = _find(q, c["id"])
    assert cmd["status"] == "failed"
    assert cmd["message"] == "relay timed out"


def test_requeue_exhausts_default_message_includes_attempt_count(spoke_loop):
    """When the bridge passes NO message, the failed row carries a count so the
    operator sees how many retries were tried."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90081})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    for _ in range(5):
        _run(loop, q.requeue_command(c["id"], max_retries=5))  # no message
    cmd = _find(q, c["id"])
    assert cmd["status"] == "failed"
    assert "5" in (cmd["message"] or "")


def test_requeue_max_retries_zero_is_fail_fast(spoke_loop):
    """max_retries<=0 preserves the old behavior: first timeout fails."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90077})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    res = _run(loop, q.requeue_command(c["id"], max_retries=0, message="relay timed out"))
    assert res["requeued"] is False
    assert res["status"] == "failed"
    assert _find(q, c["id"])["status"] == "failed"


def test_requeue_leaves_terminal_commands_alone(spoke_loop):
    """A command that already completed/failed isn't resurrected by a stale
    requeue (the bridge may double-call if a re-deliver races a requeue)."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90078})["command"]
    _run(loop, q.ack_command(c["id"], "completed", "VM 90078 destroyed"))
    res = _run(loop, q.requeue_command(c["id"], max_retries=5, message="late timeout"))
    assert res["requeued"] is False
    assert res["status"] == "completed"
    assert _find(q, c["id"])["status"] == "completed"


def test_requeued_command_is_redelivered_on_next_poll(spoke_loop):
    """A requeued (status=pending) command is returned by the next
    poll_agent_inbox and marked delivered again — the bridge re-relays it."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90079})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))  # → delivered
    _run(loop, q.requeue_command(c["id"], max_retries=5, message="timed out"))  # → pending
    assert _find(q, c["id"])["status"] == "pending"
    inbox = _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    ids = [x["id"] for x in inbox["commands"]]
    assert c["id"] in ids
    assert _find(q, c["id"])["status"] == "delivered"
    # The attempts counter survives the re-deliver (not reset by poll).
    assert _find(q, c["id"])["relay_attempts"] == 1


def test_cs_requeue_command_handler(spoke_loop):
    """The CS_REQUEUE_COMMAND hub→spoke command wires through to requeue_command
    with the operator's max_retries."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90080})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    res = _run(loop, spoke.handle_command("CS_REQUEUE_COMMAND",
            {"id": c["id"], "max_retries": 5, "message": "Timed out waiting for spoke response"}))
    assert res["status"] == "SUCCESS"
    assert res["requeued"] is True
    assert _find(q, c["id"])["status"] == "pending"

# ── env-tunable expiry (cloud-connected agents offline > old 15-min window) ──

def test_default_command_expire_is_two_hours():
    """The default COMMAND_EXPIRE_SECS is 7200 (2h) — a cloud agent offline
    past the old 15-min window must NOT have its queued delete_vm expire."""
    import command_queue as cq
    assert cq.COMMAND_EXPIRE_SECS == 7200


def test_command_expire_is_env_overridable(monkeypatch):
    """CS_COMMAND_EXPIRE_SECS overrides the default at import time."""
    import importlib
    import command_queue as cq
    monkeypatch.setenv("CS_COMMAND_EXPIRE_SECS", "3600")
    monkeypatch.setenv("CS_STALE_DELIVERED_SECS", "120")
    try:
        importlib.reload(cq)
        assert cq.COMMAND_EXPIRE_SECS == 3600
        assert cq.STALE_DELIVERED_SECS == 120
    finally:
        # Restore the module for the rest of the session.
        monkeypatch.delenv("CS_COMMAND_EXPIRE_SECS", raising=False)
        monkeypatch.delenv("CS_STALE_DELIVERED_SECS", raising=False)
        importlib.reload(cq)


def test_enqueued_command_expires_at_creation_plus_two_hours(spoke_loop):
    """A fresh command's expires_at is created_at + 7200 (2h), so it survives
    a cloud agent offline for up to 2h."""
    import command_queue as cq
    assert cq.COMMAND_EXPIRE_SECS == 7200
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cloud-01", "delete_vm", {"vmid": 90090})["command"]
    assert abs(c["expires_at"] - c["created_at"] - 7200) < 5
