"""A hub ``full_config`` CS_CONFIG_UPDATE (reconnect / CS_CONFIG_REQUEST / the
hourly backstop / drift repair) must apply ONLY keys whose value changed since
the last apply, so a periodic full refresh never re-fires update_now to every
client for an unchanged sim/user override. Partial pushes still apply as-is.
The hub's config_hash is remembered and echoed in telemetry.
"""

import asyncio
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke
from command_handlers.handlers_config import cs_config_diff, cs_value_hash


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    cfg = tmp_path / "configs"
    data.mkdir()
    cfg.mkdir()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data, cfg)
    s.registry = ClientRegistry(data)
    s.queue = CommandQueue(data, s.settings)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(loop, coro):
    return loop.run_until_complete(coro)


def _update_nows(spoke, loop):
    loop.run_until_complete(asyncio.sleep(0.15))
    return [c for c in _run(loop, spoke.queue.list_commands())
            if c.get("action") == "update_now"]


FULL = {"user_conf_override": "[kbell]\ndns_fail=on\n",
        "full_config": True, "config_hash": "h1"}


def test_repeated_full_push_does_not_refire_update_now(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.registry.apply_status("kbell-1", {"platform": "linux"}))
    r = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", dict(FULL)))
    assert r["status"] == "SUCCESS" and r["config_hash"] == "h1"
    assert len(_update_nows(spoke, loop)) == 1
    r2 = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {**FULL, "config_hash": "h2"}))
    assert r2["applied"] == []
    assert r2["config_hash"] == "h2"
    assert len(_update_nows(spoke, loop)) == 1, "unchanged override re-fired update_now"


def test_full_push_applies_changed_key(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", dict(FULL)))
    r = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        **FULL, "user_conf_override": "[kbell]\ndns_fail=off\n", "config_hash": "h3"}))
    assert "user_conf_override:updated" in r["applied"]


def test_partial_push_always_applies_and_does_not_set_hash(spoke_loop):
    spoke, loop = spoke_loop
    p = {"user_conf_override": "[kbell]\ndns_fail=on\n"}
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", dict(p)))
    r = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", dict(p)))
    assert "user_conf_override:updated" in r["applied"]
    assert r["config_hash"] == ""
    # ...but a later full push with the same value is a no-op.
    r2 = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", dict(FULL)))
    assert r2["applied"] == []


def test_diff_coupled_keys_ride_along():
    patch = {"central_sites_config": {"a": 2}, "tier_priority": "t2_first",
             "qt_exclude_sims": ["x"], "config_source": "hub",
             "sim_conf_override": "s", "other": 1}
    seen = {k: cs_value_hash(v) for k, v in patch.items()}
    seen["central_sites_config"] = "changed"
    out = cs_config_diff(patch, seen)
    assert set(out) == {"central_sites_config", "tier_priority", "qt_exclude_sims"}
    seen = {k: cs_value_hash(v) for k, v in patch.items()}
    seen["config_source"] = "changed"
    assert set(cs_config_diff(patch, seen)) == {"config_source", "sim_conf_override"}
    assert cs_config_diff(None, {}) == {}
    assert cs_config_diff({"a": 1}, None) == {"a": 1}
