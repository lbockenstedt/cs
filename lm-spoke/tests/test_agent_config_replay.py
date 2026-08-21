"""Regression: _repush_agent_config must never replay a stale usb_config.

_agent_config_cache holds whatever the hub last pushed via SET_AGENT_CONFIG,
and is only refreshed BY a fresh hub push — never by an agent reconnect. A WS
reconnect (network blip, local TLS cert rebind) needs no agent process
restart, so it's invisible to the agent's own "hub config NOT confirmed"
staleness guard. Before this fix, _on_agent_registered/_repush_agent_config
blindly replayed the frozen cached blob on every reconnect — so a
usb_auto_provision (or t1/t3_exclude_hosts) value the hub pushed once, then
later changed via the UI, could resurrect on any reconnect for as long as the
cs-spoke process stayed up, with zero human action and zero agent restart.

Fix: refresh the usb_config leaf from the spoke's own live CSSettings store
(always current) right before every replay.
"""
import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from control_plane import CSControlPlane  # noqa: E402
from cs_settings import CSSettings  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bare_control_plane(settings):
    cp = object.__new__(CSControlPlane)
    cp._cs_spoke = type("Stub", (), {"settings": settings})()
    sent = {}

    async def _fake_send_to_agent(cmd, data, agent_id=None):
        sent["cmd"] = cmd
        sent["data"] = data
        sent["agent_id"] = agent_id
        return {"status": "SUCCESS"}

    cp.send_to_agent = _fake_send_to_agent
    return cp, sent


def test_stale_cached_auto_provision_on_is_refreshed_to_current_off(tmp_path):
    settings = CSSettings(tmp_path, tmp_path)
    assert settings.usb_config_payload()["auto_provision"] == "off"

    cp, sent = _bare_control_plane(settings)
    stale_cfg = {
        "client_simulation": {
            "enabled": True,
            "usb_config": {"auto_provision": "on", "t1_exclude_hosts": []},
        }
    }
    _run(cp._repush_agent_config("agent-1", stale_cfg))

    replayed_usb_cfg = sent["data"]["client_simulation"]["usb_config"]
    assert replayed_usb_cfg["auto_provision"] == "off", \
        "replay must reflect the CURRENT settings store, not the frozen cache"


def test_current_t1_exclude_hosts_wins_over_stale_cached_list(tmp_path):
    settings = CSSettings(tmp_path, tmp_path)
    settings.settings["t1_exclude_hosts"] = ["pxmx-cs-svr-06"]

    cp, sent = _bare_control_plane(settings)
    stale_cfg = {"client_simulation": {"usb_config": {"t1_exclude_hosts": []}}}
    _run(cp._repush_agent_config("agent-1", stale_cfg))

    replayed = sent["data"]["client_simulation"]["usb_config"]
    assert replayed["t1_exclude_hosts"] == ["pxmx-cs-svr-06"]


def test_non_usb_config_fields_are_untouched_by_the_refresh(tmp_path):
    settings = CSSettings(tmp_path, tmp_path)
    cp, sent = _bare_control_plane(settings)
    stale_cfg = {"client_simulation": {"enabled": True, "tenant_id": "acme"}}
    _run(cp._repush_agent_config("agent-1", stale_cfg))

    replayed = sent["data"]["client_simulation"]
    assert replayed["enabled"] is True
    assert replayed["tenant_id"] == "acme"
    assert "usb_config" in replayed  # freshly attached from settings


def test_no_client_simulation_key_passes_through_unchanged(tmp_path):
    settings = CSSettings(tmp_path, tmp_path)
    cp, sent = _bare_control_plane(settings)
    cfg = {"some_other_key": "value"}
    _run(cp._repush_agent_config("agent-1", cfg))

    assert sent["data"] == {"some_other_key": "value"}


def test_missing_cs_spoke_back_reference_does_not_crash(tmp_path):
    cp = object.__new__(CSControlPlane)
    cp._cs_spoke = None
    sent = {}

    async def _fake_send_to_agent(cmd, data, agent_id=None):
        sent["data"] = data
        return {"status": "SUCCESS"}

    cp.send_to_agent = _fake_send_to_agent
    stale_cfg = {"client_simulation": {"usb_config": {"auto_provision": "on"}}}
    _run(cp._repush_agent_config("agent-1", stale_cfg))

    # No settings store reachable — degrades to replaying the cached value
    # verbatim rather than crashing the reconnect handshake.
    assert sent["data"] == stale_cfg
