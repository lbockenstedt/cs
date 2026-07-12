"""CS_CONFIG_UPDATE / CS_UPDATE_CONFIG re-reconcile wiring (Chunk 4 / #151).

A SimQuotaEngine reconcile must be triggered when a hub-pushed
CS_CONFIG_UPDATE changes anything the engine cares about — the effective
quota list, central_sites_config (sim_quotas / site_mappings), or the
sim/user-override INI text (bucket-default wsite + sim flags shift a quota's
site pool + exclusivity eligibility) — and when the local sim-config editors
save. Uses a recording stub for _trigger_sim_quota_reconcile so no real engine
task spins up.
"""
import asyncio
from pathlib import Path

import pytest

from command_queue import CSSettings
from cs_spoke import CSSpoke
from local_store import LocalStore


def _make_spoke(data_dir: Path, config_dir: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    s.local_store = LocalStore(data_dir)
    return s, loop


class _TriggerRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "configs"     # isolate config writes away from the repo
    cfg.mkdir()
    s, loop = _make_spoke(data, cfg)
    rec = _TriggerRecorder()
    s._trigger_sim_quota_reconcile = rec
    try:
        yield s, loop, rec
    finally:
        loop.close()
        # Leave a fresh open loop as the global so sibling test modules that call
        # asyncio.get_event_loop() at fixture/setup time (e.g. test_client_api)
        # don't hit "There is no current event loop" — set_event_loop(None)
        # poisons the process-wide loop state for the rest of the session.
        asyncio.set_event_loop(asyncio.new_event_loop())


def _run(loop, coro):
    return loop.run_until_complete(coro)


def test_config_update_effective_sim_quotas_triggers(spoke_loop):
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE",
                                      {"effective_sim_quotas": [
                                          {"alert_id": "A", "sim_id": "dns_fail",
                                           "count": 1, "site": "MIA", "enabled": True}]}))
    assert res["status"] == "SUCCESS"
    assert "effective_sim_quotas" in res["applied"]
    assert rec.calls == 1


def test_config_update_sim_conf_override_triggers(spoke_loop):
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE",
                                      {"sim_conf_override": "[s0]\nwsite=MIA\n"}))
    assert res["status"] == "SUCCESS"
    assert any(a.startswith("sim_conf_override") for a in res["applied"])
    assert rec.calls == 1


def test_config_update_user_conf_override_triggers(spoke_loop):
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE",
                                      {"user_conf_override": "[jsmith]\nwsite=DFW\n"}))
    assert res["status"] == "SUCCESS"
    assert any(a.startswith("user_conf_override") for a in res["applied"])
    assert rec.calls == 1


def test_config_update_central_sites_config_triggers(spoke_loop):
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE",
                                      {"central_sites_config": {
                                          "sim_quotas": [], "site_mappings": {},
                                          "monitored_checks": [], "hardware_checks": []}}))
    assert res["status"] == "SUCCESS"
    assert "central_sites_config" in res["applied"]
    assert rec.calls == 1


def test_config_update_unrelated_keys_do_not_trigger(spoke_loop):
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE",
                                      {"usb_vidpids": ["1111:2222"]}))
    assert res["status"] == "SUCCESS"
    assert rec.calls == 0


def test_config_update_multiple_reconcile_keys_trigger_once(spoke_loop):
    # One push carrying several reconcile-triggering keys → a single reconcile,
    # not one per key (the reconcile lock would serialize them anyway).
    s, loop, rec = spoke_loop
    res = _run(loop, s.handle_command("CS_CONFIG_UPDATE", {
        "effective_sim_quotas": [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 1, "site": "MIA", "enabled": True}],
        "sim_conf_override": "[s0]\nwsite=MIA\n",
        "central_sites_config": {"sim_quotas": [], "site_mappings": {},
                                 "monitored_checks": [], "hardware_checks": []},
    }))
    assert res["status"] == "SUCCESS"
    assert rec.calls == 1