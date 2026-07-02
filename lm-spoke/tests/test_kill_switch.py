"""CS_GET_KILL_SWITCH + CS_KILL_SWITCH spoke handlers.

The hub's kill-switch banner reads via CS_GET_KILL_SWITCH. "GET" is in the
NOT_IMPLEMENTED matcher set, so the handler MUST sit before the matcher or the
banner's read hits a dead command. CS_KILL_SWITCH toggles
engine.set_kill_switch (persists kill_switch.txt + short-circuits every sim
iteration to KILLED).
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
    # CSSpoke hardcodes the engine's config_dir to the real repo configs/; redirect
    # it to a tmp dir so set_kill_switch writes kill_switch.txt there instead of
    # mutating the tracked repo file (the engine already loaded its sim_conf at
    # construction; only kill_switch.txt reads/writes use config_dir after that).
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


def test_get_kill_switch_not_not_implemented(spoke_loop):
    """The banner's read forwards CS_GET_KILL_SWITCH; without a handler before
    the matcher the spoke returned NOT_IMPLEMENTED (dead read)."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_GET_KILL_SWITCH", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["kill_switch"] is False


def test_set_then_get_kill_switch_roundtrip(spoke_loop):
    spoke, loop = spoke_loop
    on = _run(loop, spoke.handle_command("CS_KILL_SWITCH", {"on": True}))
    assert on["status"] == "SUCCESS"
    assert on["kill_switch"] is True
    got = _run(loop, spoke.handle_command("CS_GET_KILL_SWITCH", {}))
    assert got["kill_switch"] is True
    # Toggle back off.
    _run(loop, spoke.handle_command("CS_KILL_SWITCH", {"on": False}))
    assert _run(loop, spoke.handle_command("CS_GET_KILL_SWITCH", {}))["kill_switch"] is False


def test_kill_switch_accepts_legacy_kill_switch_key(spoke_loop):
    """Legacy payloads used {kill_switch: true} rather than {on: true}."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_KILL_SWITCH", {"kill_switch": True}))
    assert resp["status"] == "SUCCESS"
    assert resp["kill_switch"] is True