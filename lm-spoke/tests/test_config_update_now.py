"""A hub-side sim/user-override edit must reach the clients' local
``simulation.conf`` — i.e. ``CS_CONFIG_UPDATE`` with ``sim_conf_override`` /
``user_conf_override`` must enqueue an ``update_now`` command to every
registered client.

Why: ``update.sh`` (which re-fetches ``/api/config`` + ``/api/config/overrides``
and diffs before applying) runs ONLY on an ``update_now`` command or a VERSION
bump. The 1-min client watchdog runs ``sys_mon.sh`` (health check), NOT
``update.sh``. So without enqueueing ``update_now`` here, a WebUI override save
writes the spoke's override file (and ``/api/config`` would serve the new
content) but the client never re-fetches → its local ``simulation.conf`` stays
stale ("config was not deployed to the client"). Mirrors the kill_switch
"enqueue to all clients" pattern in the cs source.
"""

import asyncio
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke


def _make_spoke(data_dir: Path, config_dir: Path):
    """Build a CSSpoke with an isolated tmp data + config dir and a per-spoke
    event loop (asyncio.Lock needs a running loop). Returns (spoke, loop)."""
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
    cfg = tmp_path / "configs"
    data.mkdir()
    cfg.mkdir()
    s, loop = _make_spoke(data, cfg)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _pump(loop, secs=0.15):
    """Let fire-and-forget create_task()d coros (the update_now notify) run."""
    loop.run_until_complete(asyncio.sleep(secs))


def test_user_conf_override_enqueues_update_now_to_all_clients(spoke_loop):
    """A user_conf_override push must enqueue update_now for every registered
    client so each re-fetches /api/config and updates its simulation.conf."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("kbell-1", {"platform": "linux"}))
    _run(loop, spoke.registry.apply_status("kbell-2", {"platform": "linux"}))

    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "user_conf_override": "[kbell]\ndns_fail=on\nssidpw_fail=on\nwsite=site-a\n",
    }))
    assert resp["status"] == "SUCCESS"
    _pump(loop)

    cmds = _run(loop, spoke.queue.list_commands())
    updates = [c for c in cmds if c.get("action") == "update_now"]
    targets = sorted(c.get("target") for c in updates)
    assert targets == ["kbell-1", "kbell-2"], (
        f"expected update_now enqueued for both registered clients, got {targets}")


def test_sim_conf_override_enqueues_update_now(spoke_loop):
    """A sim_conf_override (global simulation.conf) push must also enqueue
    update_now — every client's bucket config may have shifted."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("node-a", {"platform": "linux"}))

    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "sim_conf_override": "[simulation]\nkill_switch=off\n",
    }))
    _pump(loop)

    cmds = _run(loop, spoke.queue.list_commands())
    assert any(c.get("action") == "update_now" and c.get("target") == "node-a"
               for c in cmds), "sim_conf_override did not enqueue update_now"


def test_no_override_no_update_now(spoke_loop):
    """A CS_CONFIG_UPDATE that doesn't touch sim/user overrides must NOT enqueue
    update_now (no spurious client refreshes on unrelated config pushes)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("node-a", {"platform": "linux"}))

    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "usb_vidpids": "[]",
    }))
    _pump(loop)

    cmds = _run(loop, spoke.queue.list_commands())
    assert not any(c.get("action") == "update_now" for c in cmds), (
        "update_now enqueued on a non-override CS_CONFIG_UPDATE")


def test_update_now_idempotent_per_client(spoke_loop):
    """A second override push must not duplicate a pending update_now for the
    same client (CommandQueue dedups by target+action+args)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("kbell-1", {"platform": "linux"}))

    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE",
        {"user_conf_override": "[kbell]\ndns_fail=on\n"}))
    _pump(loop)
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE",
        {"user_conf_override": "[kbell]\ndns_fail=off\n"}))
    _pump(loop)

    cmds = _run(loop, spoke.queue.list_commands())
    updates = [c for c in cmds if c.get("action") == "update_now"
               and c.get("target") == "kbell-1"]
    assert len(updates) == 1, f"expected one update_now for kbell-1, got {updates}"