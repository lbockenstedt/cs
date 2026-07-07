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


# ── kill_switch_active mtime cache (Request-Timeout fix) ──────────────────────
# kill_switch_active runs on the cs spoke's shared event loop (engine every ~5 s
# + /api/kill-switch). It's mtime-cached so the sync open/read/close of
# kill_switch.txt doesn't stall the loop. set_kill_switch writes the file → mtime
# changes → cache misses → re-reads, so toggles never return a stale cached value.


def test_kill_switch_active_cache_picks_up_external_file_toggle(spoke_loop):
    """An external edit to kill_switch.txt (mtime change) must invalidate the
    cache — simulating an operator toggling the file directly."""
    spoke, loop = spoke_loop
    ks = spoke.engine.config_dir / "kill_switch.txt"
    # Cold read: file absent → False; cache populated with (mtime=-1, False).
    assert spoke.engine.kill_switch_active() is False
    # External toggle on.
    ks.write_text("on\n", encoding="utf-8")
    assert spoke.engine.kill_switch_active() is True
    # External toggle off.
    ks.write_text("off\n", encoding="utf-8")
    assert spoke.engine.kill_switch_active() is False


def test_kill_switch_active_in_memory_override_short_circuits(spoke_loop):
    """CS_KILL_SWITCH sets an in-memory _kill_switch that short-circuits the
    file read entirely (and stays correct regardless of cache state)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_KILL_SWITCH", {"on": True}))
    # File says "on" too, but the override is what makes it deterministic.
    assert spoke.engine.kill_switch_active() is True
    spoke.engine._kill_switch = True
    ks = spoke.engine.config_dir / "kill_switch.txt"
    if ks.exists():
        ks.write_text("off\n", encoding="utf-8")  # file says off
    assert spoke.engine.kill_switch_active() is True  # override wins


# ── get_version mtime cache (Request-Timeout fix) ─────────────────────────────
# /ws/client connects call spoke.get_version() per connect on the shared event
# loop; it's mtime-cached so the VERSION read only happens on an autobump
# release. Verify it returns the tracked VERSION content, populates the cache,
# and is stable across calls (cache hit).


def test_get_version_returns_tracked_version_and_caches(spoke_loop):
    spoke, loop = spoke_loop
    v1 = spoke.get_version()
    assert v1 and v1 != "unknown", "repo VERSION file should be present + tracked"
    cache = getattr(spoke, "_version_cache", None)
    assert cache is not None, "cache must be populated after first read"
    mtime, cached_v = cache
    assert cached_v == v1
    # Second call is a cache hit (same mtime) → same value, no re-read.
    v2 = spoke.get_version()
    assert v2 == v1
    assert spoke._version_cache[0] == mtime