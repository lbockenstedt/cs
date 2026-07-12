"""CS_GET/SET_PXMX_SITE_MAP — operator assigns each pxmx server (agent host)
to a site; the SimQuotaEngine resolves a client's site via its hosting
server's entry. Persists in local_store.pxmx_site_map, validates sites against
simulation.conf + Central site_mappings, and nudges the engine to re-reconcile.
"""
import asyncio
from pathlib import Path

import pytest

from client_registry import ClientRegistry
from command_queue import CommandQueue, CSSettings
from cs_spoke import CSSpoke
from local_store import LocalStore

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    s.registry = ClientRegistry(data_dir)
    s.queue = CommandQueue(data_dir, s.settings)
    # Isolate local_store on the tmp data dir (CSSpoke.__init__ points it at the
    # repo's lm-spoke/data; rebind so the test never touches real state).
    s.local_store = LocalStore(data_dir)
    # Stub a control_plane so _get_agents (CS_GET_PXMX_SITE_MAP) has one.
    s.control_plane = _FakeCP()
    return s, loop


class _FakeCP:
    connected_agents = {"px1": {"hostname": "px1", "last_seen": 0,
                                "version": "1.0", "status": "connected"}}
    pending_agents = {}

    def approve_pending_agent(self, *a, **k):
        return False


def _run(loop, coro):
    return loop.run_until_complete(coro)


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    s, loop = _make_spoke(data, CONFIGS)
    # SimQuotaEngine.trigger() may fire a reconcile task; stub the engine so the
    # SET handler's nudge doesn't spin up a real loop task against empty state.
    s._trigger_sim_quota_reconcile = lambda: None
    try:
        yield s, loop
    finally:
        loop.close()
        # Leave a fresh open loop as the global so sibling test modules that call
        # asyncio.get_event_loop() at fixture/setup time (e.g. test_client_api)
        # don't hit "There is no current event loop" — set_event_loop(None)
        # poisons the process-wide loop state for the rest of the session.
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_get_returns_empty_map_and_connected_agents(spoke_loop):
    s, loop = spoke_loop
    res = _run(loop, s.handle_command("CS_GET_PXMX_SITE_MAP", {}))
    assert res["status"] == "SUCCESS"
    assert res["pxmx_site_map"] == {}
    assert any(a["agent_id"] == "px1" for a in res["agents"])


def test_set_persists_and_round_trips(spoke_loop):
    s, loop = spoke_loop
    res = _run(loop, s.handle_command(
        "CS_SET_PXMX_SITE_MAP", {"pxmx_site_map": {"px1": "MIA", "px2": "DFW"}}))
    assert res["status"] == "SUCCESS"
    assert res["pxmx_site_map"] == {"px1": "MIA", "px2": "DFW"}
    assert res["errors"] == []
    # Persisted to local_store.
    assert s.local_store.get_pxmx_site_map() == {"px1": "MIA", "px2": "DFW"}
    # Round-trips via GET.
    got = _run(loop, s.handle_command("CS_GET_PXMX_SITE_MAP", {}))
    assert got["pxmx_site_map"] == {"px1": "MIA", "px2": "DFW"}


def test_set_drops_blank_entries(spoke_loop):
    s, loop = spoke_loop
    res = _run(loop, s.handle_command(
        "CS_SET_PXMX_SITE_MAP",
        {"pxmx_site_map": {"px1": "MIA", "": "DFW", "px2": ""}}))
    assert res["pxmx_site_map"] == {"px1": "MIA"}


def test_set_flags_unknown_site_but_keeps_host(spoke_loop):
    # "ZZZ" isn't in simulation.conf buckets or site_mappings → flagged but the
    # host mapping is retained so the operator can fix the typo in-place.
    s, loop = spoke_loop
    res = _run(loop, s.handle_command(
        "CS_SET_PXMX_SITE_MAP", {"pxmx_site_map": {"px1": "ZZZ"}}))
    assert res["status"] == "SUCCESS"
    assert res["pxmx_site_map"] == {"px1": "ZZZ"}
    assert any("px1" in e and "ZZZ" in e for e in res["errors"])


def test_local_store_set_normalizes(spoke_loop):
    s, loop = spoke_loop
    clean = s.local_store.set_pxmx_site_map(
        {"px1": "MIA", "  px2  ": "  DFW  ", "": "X", "px3": ""})
    assert clean == {"px1": "MIA", "px2": "DFW"}