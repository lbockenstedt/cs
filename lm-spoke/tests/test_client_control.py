"""Per-client override control-panel spoke handlers (CS_GET_CLIENT_OVERRIDES /
CS_SET_CLIENT_OVERRIDES / CS_CLEAR_CLIENT_OVERRIDES / CS_SET_ALL_CLIENT_OVERRIDES).

Ports the legacy cs webui-spoke "Control Panel": a per-client set of live sim-flag
overrides (kill_switch/dns_fail/iperf/download/www_traffic/ping_test/ssidpw_fail/
auth_fail/dhcp_fail/port_flap/assoc_fail) applied via Apply / Clear / Apply-to-ALL.
These wrap ``ClientRegistry.set_overrides`` / ``clear_overrides`` — the SAME
persisted store ``/api/config`` reads at delivery time (unlike the ephemeral demo
flags), so an applied override is sticky across reconnects and survives a reboot.
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
        # Leave a fresh open loop as the global, not None — set_event_loop(None)
        # poisons the process-wide loop state (Py3.9) for every sibling test
        # module that calls asyncio.get_event_loop() afterward, in the same
        # pytest session (see test_pxmx_site_map.py's spoke_loop fixture).
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_get_overrides_empty_when_unset(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command(
        "CS_GET_CLIENT_OVERRIDES", {"hostname": "host-a"}))
    assert resp["status"] == "SUCCESS"
    assert resp["overrides"] == {}


def test_set_then_get_roundtrip(spoke_loop):
    spoke, loop = spoke_loop
    flags = {"dns_fail": "on", "sim_load": "50"}
    resp = _run(loop, spoke.handle_command(
        "CS_SET_CLIENT_OVERRIDES", {"hostname": "host-a", "overrides": flags}))
    assert resp["status"] == "SUCCESS"
    assert resp["overrides"] == flags
    got = _run(loop, spoke.handle_command(
        "CS_GET_CLIENT_OVERRIDES", {"hostname": "host-a"}))
    assert got["overrides"] == flags


def test_set_overrides_persist_to_registry(spoke_loop):
    """The handler writes the SAME persisted store /api/config reads, so the
    override is sticky (not ephemeral like a demo flag)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command(
        "CS_SET_CLIENT_OVERRIDES",
        {"hostname": "host-a", "overrides": {"kill_switch": "on"}}))
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {"kill_switch": "on"}


def test_clear_overrides_removes_them(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command(
        "CS_SET_CLIENT_OVERRIDES",
        {"hostname": "host-a", "overrides": {"dns_fail": "on"}}))
    resp = _run(loop, spoke.handle_command(
        "CS_CLEAR_CLIENT_OVERRIDES", {"hostname": "host-a"}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] is True
    got = _run(loop, spoke.handle_command(
        "CS_GET_CLIENT_OVERRIDES", {"hostname": "host-a"}))
    assert got["overrides"] == {}


def test_set_all_applies_to_every_registered_client(spoke_loop):
    spoke, loop = spoke_loop
    # Register three clients first.
    for h in ("host-a", "host-b", "host-c"):
        _run(loop, spoke.registry.set_overrides(h, {"sim_load": "100"}))
    resp = _run(loop, spoke.handle_command(
        "CS_SET_ALL_CLIENT_OVERRIDES",
        {"overrides": {"dhcp_fail": "on"}}))
    assert resp["status"] == "SUCCESS"
    assert resp["applied"] == 3
    # set_overrides MERGES (see its docstring: a single-flag toggle must not
    # wipe a client's other overrides) — sim_load survives alongside the newly
    # applied dhcp_fail, it isn't replaced.
    for h in ("host-a", "host-b", "host-c"):
        assert spoke.registry.get(h)["overrides"] == {"sim_load": "100", "dhcp_fail": "on"}


def test_set_all_with_no_clients_applies_zero(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command(
        "CS_SET_ALL_CLIENT_OVERRIDES", {"overrides": {"dns_fail": "on"}}))
    assert resp["status"] == "SUCCESS"
    assert resp["applied"] == 0


def test_clear_all_wipes_every_registered_clients_overrides(spoke_loop):
    """CS_CLEAR_ALL_CLIENT_OVERRIDES must drop the legacy per-client REGISTRY
    override layer for every registered client in one shot — the hidden
    [username] sim-flag source /api/config bakes in. Without it, stale registry
    overrides (set by the old Control Panel / a prior bulk set / a since-removed
    SimQuotaEngine assignment) persist in clients.json, invisible in the
    User Overrides card (which reads user-overrides.conf), and the served
    simulation.conf keeps a stale [username] section no matter how many times
    the client re-fetches."""
    spoke, loop = spoke_loop
    # Register three clients with differing per-client registry overrides.
    _run(loop, spoke.registry.set_overrides("host-a", {"dns_fail": "on", "ssidpw_fail": "on"}))
    _run(loop, spoke.registry.set_overrides("host-b", {"dns_fail": "off"}))
    _run(loop, spoke.registry.set_overrides("host-c", {"wsite": "MIA"}))
    resp = _run(loop, spoke.handle_command("CS_CLEAR_ALL_CLIENT_OVERRIDES", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 3
    for h in ("host-a", "host-b", "host-c"):
        entry = spoke.registry.get(h)
        assert not entry.get("overrides"), f"{h} still has overrides: {entry.get('overrides')}"


def test_clear_all_with_no_clients_clears_zero(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CLEAR_ALL_CLIENT_OVERRIDES", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] == 0


def test_missing_hostname_errors(spoke_loop):
    spoke, loop = spoke_loop
    for cmd in ("CS_GET_CLIENT_OVERRIDES", "CS_SET_CLIENT_OVERRIDES",
                "CS_CLEAR_CLIENT_OVERRIDES"):
        resp = _run(loop, spoke.handle_command(cmd, {}))
        assert resp["status"] == "ERROR"
        assert "hostname" in resp["message"]


def test_bad_overrides_type_errors(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command(
        "CS_SET_CLIENT_OVERRIDES",
        {"hostname": "host-a", "overrides": "not-a-dict"}))
    assert resp["status"] == "ERROR"
    assert "object" in resp["message"]


def test_purge_clients_clears_memory_and_disk(spoke_loop):
    """CS_PURGE_CLIENTS drops every client from memory AND deletes clients.json
    (the legacy cs-webui "Purge Clients" button → DELETE /api/clients/history).
    Returns the count removed; the on-disk file is gone."""
    spoke, loop = spoke_loop
    for h in ("host-a", "host-b", "host-c"):
        _run(loop, spoke.registry.set_overrides(h, {"sim_load": "100"}))
    assert spoke.registry.count() == 3
    # Persistence is debounced (~5s coalesced flush) — flush explicitly so the
    # on-disk file exists before the purge deletes it.
    _run(loop, spoke.registry.aclose())
    assert spoke.registry._path.exists()
    resp = _run(loop, spoke.handle_command("CS_PURGE_CLIENTS", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["purged"] == 3
    assert spoke.registry.count() == 0
    assert spoke.registry.get_all() == {}
    assert not spoke.registry._path.exists()


def test_purge_clients_when_empty_reports_zero(spoke_loop):
    """Purging an already-empty registry is a no-op that reports 0 (and does
    not error when clients.json is already absent)."""
    spoke, loop = spoke_loop
    assert spoke.registry.count() == 0
    resp = _run(loop, spoke.handle_command("CS_PURGE_CLIENTS", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["purged"] == 0
    assert not spoke.registry._path.exists()


def test_purge_then_repopulate_works(spoke_loop):
    """After a purge the registry is fresh-empty and accepts new clients again
    (the file is recreated on the next flush)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.set_overrides("host-a", {"dns_fail": "on"}))
    _run(loop, spoke.handle_command("CS_PURGE_CLIENTS", {}))
    _run(loop, spoke.registry.set_overrides("host-b", {"kill_switch": "on"}))
    assert spoke.registry.count() == 1
    assert spoke.registry.get("host-b")["overrides"] == {"kill_switch": "on"}
    # Debounced persistence: flush explicitly, then the file is recreated.
    _run(loop, spoke.registry.aclose())
    assert spoke.registry._path.exists()