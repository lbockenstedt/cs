"""Demo-scenario spoke handlers (CS_DEMO_SCENARIO / CS_DEMO_CLEAR /
CS_GET_DEMO_ACTIVE / CS_GET_DEMO_SCENARIOS).

Ports the legacy cs webui-spoke demo system: a named per-client failure preset
(dns_fail/dhcp_fail/assoc_fail/auth_fail/ssidpw_fail/port_flap, or 'normal' to
clear) with a 120-min TTL + auto-expiry. The override is EPHEMERAL + in-memory
on the spoke's DemoManager (layered on top of persisted registry overrides at
config delivery), so it never mutates data/client-status.json and clears back to
the operator's prior setting on expiry.
"""

import asyncio
import time
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke
from demo_scenarios import DEMO_SCENARIOS, DEMO_TTL_SECONDS, FAILURE_FLAGS, DemoManager

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


def test_scenario_catalog_has_normal_plus_each_failure(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_GET_DEMO_SCENARIOS", {}))
    assert resp["status"] == "SUCCESS"
    sc = resp["scenarios"]
    assert sc["normal"] == {f: "off" for f in FAILURE_FLAGS}
    for f in FAILURE_FLAGS:
        assert sc[f][f] == "on"
        for other in FAILURE_FLAGS:
            if other != f:
                assert sc[f][other] == "off"


def test_apply_demo_scenario_records_active(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "dns_fail", "triggered_by": "admin"}))
    assert resp["status"] == "SUCCESS"
    assert resp["scenario"] == "dns_fail"
    active = _run(loop, spoke.handle_command("CS_GET_DEMO_ACTIVE", {}))["active"]
    assert len(active) == 1
    assert active[0]["hostname"] == "host-a"
    assert active[0]["scenario"] == "dns_fail"
    assert active[0]["triggered_by"] == "admin"
    # ~120 min remaining (allow slack).
    assert 118 <= active[0]["minutes_remaining"] <= 120


def test_effective_flags_layer_on_registry_overrides(spoke_loop):
    """The demo flag must reach /api/config ON TOP of persisted overrides, and
    must NOT mutate the persisted registry."""
    spoke, loop = spoke_loop
    # Operator had a persisted override (e.g. sim_load=50).
    _run(loop, spoke.registry.set_overrides("host-a", {"sim_load": "50"}))
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "dhcp_fail"}))
    # Effective flags = demo only (dhcp_fail=on) — sim_load stays in registry.
    eff = spoke.demo.effective_flags("host-a")
    assert eff == {"dns_fail": "off", "dhcp_fail": "on", "assoc_fail": "off",
                   "auth_fail": "off", "ssidpw_fail": "off", "port_flap": "off"}
    # Registry override untouched by the demo.
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {"sim_load": "50"}


def test_normal_scenario_clears_active(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "auth_fail"}))
    assert spoke.demo.effective_flags("host-a")  # something active
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "normal"}))
    assert spoke.demo.effective_flags("host-a") == {}
    assert _run(loop, spoke.handle_command("CS_GET_DEMO_ACTIVE", {}))["active"] == []


def test_demo_clear_handler(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "port_flap"}))
    resp = _run(loop, spoke.handle_command("CS_DEMO_CLEAR", {"hostname": "host-a"}))
    assert resp["status"] == "SUCCESS"
    assert resp["cleared"] is True
    # Second clear → cleared:False.
    resp2 = _run(loop, spoke.handle_command("CS_DEMO_CLEAR", {"hostname": "host-a"}))
    assert resp2["cleared"] is False


def test_unknown_scenario_returns_error(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "bogus"}))
    assert resp["status"] == "ERROR"
    assert "Unknown scenario" in resp["message"]


def test_expired_demo_auto_clears_on_summary(spoke_loop):
    """An expired demo is swept on CS_GET_DEMO_ACTIVE (lazy expiry) so the client
    reverts even without the background loop (which tests don't start)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "dns_fail"}))
    # Force expiry.
    spoke.demo._active["host-a"]["expires_at"] = time.time() - 1
    active = _run(loop, spoke.handle_command("CS_GET_DEMO_ACTIVE", {}))["active"]
    assert active == []
    assert spoke.demo.effective_flags("host-a") == {}


# ── propagation: a demo change must enqueue update_now so the client re-fetches ─

def _update_now_targets(spoke, loop):
    cmds = _run(loop, spoke.queue.list_commands())
    return sorted(c.get("target") for c in cmds
                  if c.get("action") == "update_now")


def test_demo_apply_enqueues_update_now(spoke_loop):
    """Triggering a demo (Demo column "Go") changes the spoke's served config —
    the client must be told to re-fetch, else the demo never takes effect
    (update.sh runs only on update_now / a VERSION bump)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("host-a", {"platform": "linux"}))
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "dns_fail"}))
    assert _update_now_targets(spoke, loop) == ["host-a"]


def test_demo_clear_enqueues_update_now(spoke_loop):
    """Clearing a demo ("normal" / CS_DEMO_CLEAR) must also re-fetch the client
    so the stale [username] demo flags leave its local simulation.conf."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("host-a", {"platform": "linux"}))
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "dns_fail"}))
    _run(loop, spoke.queue.list_commands())  # drain
    # Clear the pending apply update_now so the next enqueue is unambiguous.
    _run(loop, spoke.handle_command("CS_DEMO_CLEAR", {"hostname": "host-a"}))
    assert "host-a" in _update_now_targets(spoke, loop)


def test_demo_expiry_enqueues_update_now(spoke_loop):
    """The 120-min auto-clear must reach the client: when the expiry sweep drops
    a demo, the on_change callback enqueues update_now so the client re-fetches
    a clean config (the bug: expiry left the client serving a stale [username])."""
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("host-a", {"platform": "linux"}))
    _run(loop, spoke.handle_command(
        "CS_DEMO_SCENARIO", {"hostname": "host-a", "scenario": "ssidpw_fail"}))
    _run(loop, spoke.queue.list_commands())  # drain the apply update_now
    # Force expiry + run the sweep directly (the background loop isn't started
    # in tests).
    spoke.demo._active["host-a"]["expires_at"] = time.time() - 1
    _run(loop, spoke.demo.sweep_expired())
    assert "host-a" in _update_now_targets(spoke, loop)
    assert spoke.demo.effective_flags("host-a") == {}


def test_demo_on_change_callback_fires_on_apply_and_clear():
    """DemoManager fires its on_change callback on apply + clear (unit-level,
    no spoke). Verifies the callback contract the spoke relies on."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fired = []
    async def _cb(h):
        fired.append(h)
    dm = DemoManager(on_change=_cb)
    try:
        _run(loop, dm.apply("host-a", "dns_fail"))
        _run(loop, dm.clear("host-a"))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert fired == ["host-a", "host-a"]