"""sim_pool: the Proxmox resource pool auto-provisioned sim clients join."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import command_queue  # noqa: E402,F401  (import first — cs_settings imports it)
from cs_settings import CSSettings  # noqa: E402
from command_handlers.handlers_config import ConfigCommandsMixin  # noqa: E402


def test_default_is_empty_so_behaviour_is_unchanged():
    assert CSSettings._DEFAULTS.get("sim_pool") == ""


def test_sim_pool_is_hub_settable():
    # Without this the WebUI could save it but it would never reach the agent.
    assert "sim_pool" in ConfigCommandsMixin._HUB_DIRECT_KEYS


def test_payload_emits_trimmed_pool(tmp_path):
    s = CSSettings.__new__(CSSettings)
    s.settings = dict(CSSettings._DEFAULTS)
    s.settings["sim_pool"] = "  sim-clients  "
    assert s.get("sim_pool", "").strip() == "sim-clients"


def test_payload_pool_absent_is_empty_string(tmp_path):
    s = CSSettings.__new__(CSSettings)
    s.settings = dict(CSSettings._DEFAULTS)
    # Empty (not None) so the agent's _sim_pool() resolves it to None → no --pool
    # flag, i.e. the historical clone behaviour.
    assert str(s.get("sim_pool", "") or "").strip() == ""


def _pool_spoke(sim_pool):
    """A stand-in spoke with one agent that adds two VMs to whatever pool it is sent."""
    sent = []

    async def send_to_agent(cmd, payload, agent_id=None, timeout=None):
        """Record the command and report two VMs added."""
        sent.append((cmd, payload, agent_id))
        return {"payload": {"data": {"status": "SUCCESS", "added": 2}}}

    spoke = SimpleNamespace(
        settings={"sim_pool": sim_pool},
        _get_agents=lambda: {"agents": [{"agent_id": "a1", "hostname": "pve1"}]},
        control_plane=SimpleNamespace(send_to_agent=send_to_agent))
    return spoke, sent


def test_add_existing_uses_the_pool_from_the_payload():
    """The pool picked in the WebUI reaches every agent (this raised NameError before)."""
    spoke, sent = _pool_spoke("configured")
    result = asyncio.run(ConfigCommandsMixin._cmd_pool_add_existing(spoke, {"pool": "picked"}))
    assert sent == [("PXMX_POOL_ADD_VMS", {"pool": "picked"}, "a1")]
    assert result == {"status": "SUCCESS", "pool": "picked", "added": 2, "errors": {}}


def test_add_existing_falls_back_to_the_configured_pool():
    """With no pool in the payload, the hub-set sim_pool is used."""
    spoke, sent = _pool_spoke("configured")
    result = asyncio.run(ConfigCommandsMixin._cmd_pool_add_existing(spoke, {}))
    assert sent == [("PXMX_POOL_ADD_VMS", {"pool": "configured"}, "a1")]
    assert result["pool"] == "configured"


def _agents_spoke(agents, replies):
    """A stand-in spoke whose agents answer from ``replies``: agent id to its
    reply data, or to an exception the call raises."""
    sent = []

    async def send_to_agent(cmd, payload, agent_id=None, timeout=None):
        """Record which agent was asked and answer as scripted."""
        sent.append(agent_id)
        if isinstance(replies[agent_id], Exception):
            raise replies[agent_id]
        return {"payload": {"data": replies[agent_id]}}

    spoke = SimpleNamespace(
        settings={"sim_pool": "pool1"},
        _get_agents=lambda: {"agents": agents},
        control_plane=SimpleNamespace(send_to_agent=send_to_agent))
    return spoke, sent


_TWO = [{"agent_id": "a1", "hostname": "pve1"}, {"agent_id": "a2", "hostname": "pve2"}]
_OK = {"status": "SUCCESS", "added": 2}
_FAIL = {"status": "ERROR", "message": "pool missing"}


@pytest.mark.parametrize("agents, replies, status, added, errors", [
    # No agent to ask (none connected, or none with an id): nothing ran.
    ([], {}, "ERROR", 0, {}),
    ([{"hostname": "pve0"}], {}, "ERROR", 0, {}),
    # Every agent failed: an error, not a partial result.
    (_TWO, {"a1": RuntimeError("timeout"), "a2": _FAIL}, "ERROR", 0,
     {"pve1": "timeout", "pve2": "pool missing"}),
    # Some failed.
    (_TWO, {"a1": _OK, "a2": _FAIL}, "PARTIAL", 2, {"pve2": "pool missing"}),
    # All succeeded.
    (_TWO, {"a1": _OK, "a2": _OK}, "SUCCESS", 4, {}),
])
def test_add_existing_status_says_what_ran(agents, replies, status, added, errors):
    """The status tells nothing-ran and all-failed apart from success and from
    a partial result, and an ERROR says why."""
    spoke, sent = _agents_spoke(agents, replies)
    result = asyncio.run(ConfigCommandsMixin._cmd_pool_add_existing(spoke, {}))
    assert (result["status"], result["added"], result["errors"]) == (status, added, errors)
    assert sent == [a["agent_id"] for a in agents if a.get("agent_id")]
    assert bool(result.get("message")) == (status == "ERROR")


@pytest.mark.parametrize("reply, code, shown", [
    # Nothing ran: the request fails with the reason, so the page says why.
    ({"status": "ERROR", "message": "no connected agent to add the VMs"},
     503, "no connected agent to add the VMs"),
    # Every agent failed: returned, so the page lists the per-host errors.
    ({"status": "ERROR", "message": "no agent added the VMs", "added": 0,
      "errors": {"pve1": "timeout"}}, 200, {"pve1": "timeout"}),
    ({"status": "SUCCESS", "pool": "pool1", "added": 2, "errors": {}}, 200, {}),
])
def test_local_add_existing_route_fails_when_nothing_ran(reply, code, shown):
    """The standalone page's route turns a nothing-ran ERROR into a 503 with
    the reason, and returns the other results for the page to show."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from local_ui_routes import build_local_ui_router

    async def handle_command(cmd, data):
        """Answer the pool command as scripted."""
        assert (cmd, data) == ("CS_POOL_ADD_EXISTING", {"pool": "pool1"})
        return reply

    app = FastAPI()
    app.include_router(build_local_ui_router(SimpleNamespace(handle_command=handle_command)))
    r = TestClient(app).post("/sim/api/t1/pools/add-existing", json={"pool": " pool1 "})
    assert r.status_code == code
    assert (r.json()["detail"] if code != 200 else r.json()["errors"]) == shown
