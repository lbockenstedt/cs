"""CS spoke Central On-Prem commands + on-prem poller + push path (Phase 5).

Central On-Prem is a second Aruba Central instance on the cs spoke — the SAME
ArubaClient/API as cloud Central, but a separate config + sites-config + status
slot + tracker shard files so the two never step on each other. These pin the
spoke-side wiring: the CS_CENTRAL_ON_PREM_* commands read/write their OWN
local_store slots + drive the on-prem poller, the hub-pushed
central_on_prem_config / central_on_prem_sites_config reach the on-prem poller
via _apply_hub_config, and the parameterized CentralPoller routes each instance
to its own slots (default "central" unchanged).
"""
import asyncio
import json
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from central_poller import CentralPoller
from cs_spoke import CSSpoke
from local_store import LocalStore

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path):
    """Build a CSSpoke with an isolated tmp data dir + an explicit event loop,
    then re-point local_store + the Central pollers at the tmp dir so the on-prem
    slots + tracker shard files are isolated from the real repo data dir.

    Returns ``(spoke, loop)``. Mirrors test_hub_config._make_spoke."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    s.registry = ClientRegistry(data_dir)
    s.queue = CommandQueue(data_dir, s.settings)
    # Re-point local_store + rebuild the Central pollers against the tmp dir so
    # the on-prem tracker shard files land in tmp (not the repo data dir) and the
    # pollers read the isolated local_store. The pollers read
    # spoke.local_store dynamically (_cfg/_sites_cfg/reload), so swapping the
    # store reroutes them; rebuilding also moves their tracker files into tmp.
    s.local_store = LocalStore(data_dir)
    s.central_poller = CentralPoller(s)  # default instance → cloud Central
    s.central_on_prem_poller = CentralPoller(s, instance="central_on_prem")
    return s, loop


def _run(loop, coro):
    return loop.run_until_complete(coro)


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "configs"
    cfg.mkdir()
    s, loop = _make_spoke(data, cfg)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ── poller instance wiring (the no-stepping guarantee at the spoke layer) ──
def test_default_poller_instance_is_cloud_central(spoke_loop):
    spoke, _ = spoke_loop
    p = CentralPoller(spoke)  # default
    assert p._inst_name == "central"
    assert p._inst["status_attr"] == "central_status"
    assert p._inst["config_getter"] == "get_central_config"
    assert p._inst["sites_getter"] == "get_central_sites_config"
    # Cloud Central tracker shard filenames (unchanged).
    assert p._inst["cc_baseline"] == "client_count_baseline.json"
    assert p._inst["health_file"] == "check_health_history.json"


def test_on_prem_poller_instance_uses_on_prem_slots(spoke_loop):
    spoke, _ = spoke_loop
    p = CentralPoller(spoke, instance="central_on_prem")
    assert p._inst_name == "central_on_prem"
    assert p._inst["status_attr"] == "central_on_prem_status"
    assert p._inst["config_getter"] == "get_central_on_prem_config"
    assert p._inst["sites_getter"] == "get_central_on_prem_sites_config"
    # Separate shard filenames → separate persisted baselines / health history.
    assert p._inst["cc_baseline"] == "central_on_prem_client_count_baseline.json"
    assert p._inst["health_file"] == "central_on_prem_check_health_history.json"


def test_unknown_poller_instance_rejected(spoke_loop):
    spoke, _ = spoke_loop
    with pytest.raises(ValueError):
        CentralPoller(spoke, instance="bogus")


def test_poller_writes_to_its_own_status_slot(spoke_loop):
    """The on-prem poller writes central_on_prem_status, never central_status —
    the core no-stepping guarantee. Cloud Central's slot is untouched."""
    spoke, _ = spoke_loop
    spoke.central_on_prem_poller._set_status({"fetched_at": 1})
    assert spoke.central_on_prem_status == {"fetched_at": 1}
    assert spoke.central_status == {}  # cloud Central untouched


# ── CS_SET_CENTRAL_ON_PREM_CONFIG merges + reloads the on-prem poller ──────
def test_set_central_on_prem_config_persists_and_reloads(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_SET_CENTRAL_ON_PREM_CONFIG", {
        "central_on_prem_config": {"cluster_url": "https://onprem.central.local",
            "api_version": "classic", "client_id": "cid", "client_secret": "sec",
            "refresh_token": "rt"}}))
    assert resp["status"] == "SUCCESS"
    cfg = spoke.local_store.get_central_on_prem_config()
    assert cfg["client_id"] == "cid"
    # Cloud Central config untouched (no stepping).
    assert spoke.local_store.get_central_config() == {}


def test_set_central_on_prem_config_sentinel_merge_keeps_existing(spoke_loop):
    """A partial save (empty values) keeps the stored creds — same sentinel rule
    as cloud Central. A subsequent set with only a new client_id keeps the
    existing client_secret."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_SET_CENTRAL_ON_PREM_CONFIG", {
        "central_on_prem_config": {"cluster_url": "https://onprem.central.local",
            "api_version": "classic", "client_id": "cid", "client_secret": "sec"}}))
    # Partial save: client_id present (overwrites), client_secret empty (KEEPS).
    _run(loop, spoke.handle_command("CS_SET_CENTRAL_ON_PREM_CONFIG", {
        "central_on_prem_config": {"client_id": "cid2", "client_secret": ""}}))
    cfg = spoke.local_store.get_central_on_prem_config()
    assert cfg["client_id"] == "cid2"
    assert cfg["client_secret"] == "sec"  # kept (empty is a sentinel, not a wipe)


def test_get_central_on_prem_config_round_trips(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_SET_CENTRAL_ON_PREM_CONFIG", {
        "central_on_prem_config": {"client_id": "X"}}))
    resp = _run(loop, spoke.handle_command("CS_GET_CENTRAL_ON_PREM_CONFIG", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["central_on_prem_config"]["client_id"] == "X"


# ── CS_CENTRAL_ON_PREM_BROWSE / AVAILABLE / TEST (not configured → empty) ───
def test_browse_not_configured_returns_empty_set(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CENTRAL_ON_PREM_BROWSE", {}))
    assert resp["status"] == "SUCCESS"
    for k in ("sites", "alerts", "insights", "clients"):
        assert resp[k] == []
    assert "not configured" in (resp.get("warning") or "").lower()


def test_test_central_on_prem_not_configured_returns_missing(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_TEST_CENTRAL_ON_PREM", {}))
    assert resp["status"] == "SUCCESS"
    row = resp["spokes"][0]
    assert row["token_valid"] is False
    assert row["token_state"] is None


def test_available_not_configured_returns_warning(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_GET_CENTRAL_ON_PREM_AVAILABLE", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["alerts"] == [] and resp["insights"] == []
    assert "not configured" in (resp.get("warning") or "").lower()


# ── CS_SET_CENTRAL_ON_PREM_SITES_CONFIG validates + reloads ─────────────────
def test_set_central_on_prem_sites_config_validates_sim_quotas(spoke_loop):
    spoke, loop = spoke_loop
    # A sim_quotas row with an unknown sim_id is dropped (validate against the
    # tenant's simulation.conf sims — here the empty tmp configs/ offers none).
    resp = _run(loop, spoke.handle_command("CS_SET_CENTRAL_ON_PREM_SITES_CONFIG", {
        "sim_quotas": [{"sim_id": "nope_not_a_sim", "alert_id": "Central On-Prem:x",
                        "site": "MIA", "count": 3, "enabled": True}],
        "site_mappings": {"MIA": "OnPremMIA"}}))
    assert resp["status"] == "SUCCESS"
    cfg = spoke.local_store.get_central_on_prem_sites_config()
    assert cfg["site_mappings"] == {"MIA": "OnPremMIA"}
    # The bogus sim_quota was dropped by validate_sim_quotas.
    assert cfg["sim_quotas"] == []
    # Cloud Central sites config untouched.
    assert spoke.local_store.get_central_sites_config() == {}


# ── _apply_hub_config: hub-pushed on-prem config reaches the on-prem poller ─
def test_config_update_applies_central_on_prem_config(spoke_loop):
    """CS_CONFIG_UPDATE with central_on_prem_config → sentinel-merge into the
    on-prem local_store slot + reload the on-prem poller. Cloud Central is
    untouched (the no-stepping guarantee on the push path)."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "central_on_prem_config": {"cluster_url": "https://onprem.central.local",
            "api_version": "new_central", "client_id": "cid", "client_secret": "sec"},
    }))
    assert resp["status"] == "SUCCESS"
    assert "central_on_prem_config" in resp["applied"]
    cfg = spoke.local_store.get_central_on_prem_config()
    assert cfg["client_id"] == "cid"
    # Cloud Central config + sites config untouched.
    assert spoke.local_store.get_central_config() == {}


def test_config_update_applies_central_on_prem_sites_config(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "central_on_prem_sites_config": {"site_mappings": {"DFW": "OnPremDFW"},
            "sim_quotas": []},
    }))
    assert resp["status"] == "SUCCESS"
    assert "central_on_prem_sites_config" in resp["applied"]
    assert spoke.local_store.get_central_on_prem_sites_config()["site_mappings"] == {"DFW": "OnPremDFW"}
    # Cloud Central sites config untouched.
    assert spoke.local_store.get_central_sites_config() == {}


def test_config_update_central_on_prem_does_not_touch_cloud_central(spoke_loop):
    """Setting BOTH cloud Central + on-prem creds in one push keeps them in
    separate slots — the two instances coexist without stepping on each other."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "central_config": {"client_id": "CLOUD", "api_version": "new_central",
            "client_secret": "cs"},
        "central_on_prem_config": {"client_id": "ONPREM", "api_version": "new_central",
            "client_secret": "os"},
    }))
    assert spoke.local_store.get_central_config()["client_id"] == "CLOUD"
    assert spoke.local_store.get_central_on_prem_config()["client_id"] == "ONPREM"