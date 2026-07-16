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


# ── verified-report safety net: delete_vm/reclone_vm lost-long-op bound ──────

def _age_last_contact(q, cid, seconds):
    """Push last_contact back in time to simulate an agent gone silent — the
    verify-sweep's clock. updated_at is left fresh: in reality the 30s stale
    reset re-probes and the collect step refresh updated_at every ~30s, so a
    silent agent ages last_contact (real-contact clock) while updated_at stays
    recent. Aging updated_at too would make the stale reset fire every poll and
    preempt the sweep (which only runs on a delivered command), masking the
    bound the test is asserting."""
    c = _find(q, cid)
    c["last_contact"] = c["last_contact"] - seconds


def test_verify_sweep_requeues_silent_delivered_delete(spoke_loop):
    """A delivered delete_vm with no agent contact for DELETE_VERIFY_TIMEOUT_SECS
    is requeued — the sweep consumes one retry and re-sends (the poll re-marks
    it delivered for the bridge to relay), instead of spinning until 2h."""
    import command_queue as cq
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90100})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))  # delivered
    # Agent goes silent past the verify window.
    _age_last_contact(q, c["id"], cq.DELETE_VERIFY_TIMEOUT_SECS + 5)
    inbox = _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    assert inbox["verify_requeued"] == 1
    cmd = _find(q, c["id"])
    assert cmd["relay_attempts"] == 1
    assert cmd["status"] == "delivered"        # re-marked for re-send
    assert cmd["last_contact"] >= c["created_at"]  # window reset for the retry


def test_verify_sweep_gives_up_after_max_retries(spoke_loop):
    """After DELETE_VERIFY_MAX_RETRIES silent windows the command is failed."""
    import command_queue as cq
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90101})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    maxr = cq.DELETE_VERIFY_MAX_RETRIES
    for i in range(1, maxr):
        _age_last_contact(q, c["id"], cq.DELETE_VERIFY_TIMEOUT_SECS + 5)
        _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
        assert _find(q, c["id"])["relay_attempts"] == i
        assert _find(q, c["id"])["status"] == "delivered"  # re-sent, not failed
    # One more silent window → exhausted → failed.
    _age_last_contact(q, c["id"], cq.DELETE_VERIFY_TIMEOUT_SECS + 5)
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    cmd = _find(q, c["id"])
    assert cmd["status"] == "failed"
    assert "verified report" in (cmd["message"] or "")


def test_touch_keeps_running_delete_off_the_verify_sweep(spoke_loop):
    """A delivered delete_vm that is TOUCHED (the bridge on ACCEPTED) within the
    window is NOT requeued — a slow-but-alive delete isn't penalized."""
    import command_queue as cq
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90102})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))  # delivered
    # Bridge touches every poll (ACCEPTED re-ack) — last_contact stays fresh.
    for _ in range(cq.DELETE_VERIFY_TIMEOUT_SECS // 10 + 1):
        _run(loop, q.touch_command(c["id"]))
    inbox = _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    assert inbox["verify_requeued"] == 0
    assert _find(q, c["id"])["status"] == "delivered"


def test_stale_reset_reprobes_delete_without_consuming_verify_budget(spoke_loop):
    """The 30s stale reset re-probes a silent delete (re-send) but does NOT
    increment relay_attempts — the verify-sweep owns the few-minutes budget."""
    import command_queue as cq
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90103})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))  # delivered
    # Age updated_at past the 30s stale window but keep last_contact younger than
    # the verify window (a re-probe that didn't reach the agent).
    cmd = _find(q, c["id"])
    cmd["updated_at"] = cmd["updated_at"] - (cq.STALE_DELIVERED_SECS + 5)
    inbox = _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    assert inbox["reset"] >= 1
    assert _find(q, c["id"])["status"] == "delivered"  # re-probed + re-delivered
    assert _find(q, c["id"])["relay_attempts"] == 0   # budget NOT consumed


def test_cs_touch_command_handler_refreshes_last_contact(spoke_loop):
    """CS_TOUCH_COMMAND (bridge on ACCEPTED) refreshes last_contact via the
    spoke handler so a running long op ages toward neither bound."""
    spoke, loop = spoke_loop
    q = spoke.queue
    c = _enqueue(q, loop, "pxmx-cs-svr-04", "delete_vm", {"vmid": 90104})["command"]
    _run(loop, q.poll_agent_inbox("pxmx-cs-svr-04"))
    before = _find(q, c["id"])["last_contact"]
    # Simulate time passing then a touch.
    _find(q, c["id"])["last_contact"] = before - 60
    res = _run(loop, spoke.handle_command("CS_TOUCH_COMMAND", {"id": c["id"]}))
    assert res["status"] == "SUCCESS"
    assert res["touched"] is True
    assert _find(q, c["id"])["last_contact"] > before - 60
