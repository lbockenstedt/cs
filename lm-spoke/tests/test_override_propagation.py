"""Override-change propagation: every spoke handler that changes a client's
served ``[username]`` section must enqueue ``update_now`` to that client so it
re-fetches ``/api/config`` and its LOCAL ``simulation.conf`` picks up the
change.

Why: ``update.sh`` (which re-fetches ``/api/config`` and diffs before applying)
runs ONLY on an ``update_now`` command or a VERSION bump — the 1-min client
watchdog runs ``sys_mon.sh`` (health), NOT ``update.sh``. So a click that changes
the spoke's registry / demo layer (sim-bar toggle, Demo column Go/normal, the
per-client Control Panel, the bulk "Clear All Overrides") would edit the
served config but the client would keep its stale local file forever — the
"overrides still there after they should have cleared" symptom. Each mutator
now pushes ``update_now`` to the affected client(s).
"""
import asyncio
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
    cfg = data_dir / "cfg"
    cfg.mkdir()
    s.engine.config_dir = cfg
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


def _update_now_targets(spoke, loop):
    cmds = _run(loop, spoke.queue.list_commands())
    return sorted(c.get("target") for c in cmds
                  if c.get("action") == "update_now")


def test_set_client_overrides_enqueues_update_now(spoke_loop):
    """A sim-bar toggle (CS_SET_CLIENT_OVERRIDES) must re-fetch the client."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("kbell-1", {"platform": "linux"}))
    _run(loop, spoke.handle_command("CS_SET_CLIENT_OVERRIDES",
        {"hostname": "kbell-1", "overrides": {"dns_fail": "on"}}))
    assert _update_now_targets(spoke, loop) == ["kbell-1"]


def test_clear_client_overrides_enqueues_update_now(spoke_loop):
    """The per-client Clear (CS_CLEAR_CLIENT_OVERRIDES) must re-fetch."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("kbell-1", {"platform": "linux"}))
    _run(loop, spoke.handle_command("CS_CLEAR_CLIENT_OVERRIDES",
        {"hostname": "kbell-1"}))
    assert _update_now_targets(spoke, loop) == ["kbell-1"]


def test_clear_all_overrides_clears_demos_and_returns_counts(spoke_loop):
    """Bulk Clear All wipes the registry layer AND the ephemeral demo layer
    (the actual source of the FAILURE_FLAGS the user was seeing — demos survive
    a registry-only clear), and reports both counts."""
    spoke, loop = spoke_loop
    for h in ("kbell-1", "ibennett-1", "node-a"):
        _run(loop, spoke.registry.apply_status(h, {"platform": "linux"}))
    # Seed a registry override + an active demo on two clients.
    _run(loop, spoke.registry.set_overrides("kbell-1", {"dns_fail": "on"}))
    _run(loop, spoke.registry.set_overrides("ibennett-1", {"dns_fail": "off"}))
    _run(loop, spoke.demo.apply("kbell-1", "ssidpw_fail"))
    _run(loop, spoke.demo.apply("ibennett-1", "dns_fail"))
    assert spoke.demo.effective_flags("kbell-1")  # demo is live

    resp = _run(loop, spoke.handle_command("CS_CLEAR_ALL_CLIENT_OVERRIDES", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 3               # registry: all 3 clients
    assert resp["demos_cleared"] == 2         # demos: kbell-1 + ibennett-1
    # Both layers gone.
    for h in ("kbell-1", "ibennett-1", "node-a"):
        assert not (spoke.registry.get(h) or {}).get("overrides")
    assert spoke.demo.effective_flags("kbell-1") == {}
    assert spoke.demo.effective_flags("ibennett-1") == {}


def test_clear_all_overrides_enqueues_update_now_to_all(spoke_loop):
    """Clear All must tell EVERY registered client to re-fetch, else each keeps
    its stale local [username] (the "still there after Clear All" symptom)."""
    spoke, loop = spoke_loop
    for h in ("kbell-1", "ibennett-1", "node-a"):
        _run(loop, spoke.registry.apply_status(h, {"platform": "linux"}))
    _run(loop, spoke.handle_command("CS_CLEAR_ALL_CLIENT_OVERRIDES", {}))
    assert _update_now_targets(spoke, loop) == ["ibennett-1", "kbell-1", "node-a"]


def test_set_all_client_overrides_enqueues_update_now_to_all(spoke_loop):
    """Apply-to-ALL changes every client's served config → re-fetch all."""
    spoke, loop = spoke_loop
    for h in ("kbell-1", "kbell-2"):
        _run(loop, spoke.registry.apply_status(h, {"platform": "linux"}))
    _run(loop, spoke.handle_command("CS_SET_ALL_CLIENT_OVERRIDES",
        {"overrides": {"kill_switch": "on"}}))
    assert _update_now_targets(spoke, loop) == ["kbell-1", "kbell-2"]


def test_clear_all_overrides_with_no_clients_clears_zero(spoke_loop):
    """An empty registry + no demos clears 0/0 and doesn't error."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CLEAR_ALL_CLIENT_OVERRIDES", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 0
    assert resp["demos_cleared"] == 0