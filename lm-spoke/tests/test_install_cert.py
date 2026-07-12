"""CSSpoke INSTALL_CERT handler — relays a hub-delivered TLS cert to each
managed pxmx agent (→ ``pvenode cert set`` on that node's pveproxy).

In the split topology the cs/lm-spoke owns the pxmx agents (they dial
``wss://<cs>:443/ws/agent``), so the hub routes the ``simulation`` cert target
here. This mirrors ProxmoxSpoke's INSTALL_CERT branch
(``proxmox_spoke.py:203-213``); the agent's ``install_cert`` (pvenode +
fingerprint verify) is covered by the pxmx agent test suite — here we assert
the cs spoke's RELAY + aggregation only. (Local-webui HTTPS apply is added in
a later phase and covered separately.)
"""
import asyncio

from cs_spoke import CSSpoke

_PEM = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n"
_KEY = "-----BEGIN PRIVATE KEY-----\nY\n-----END PRIVATE KEY-----\n"


class _FakeCP:
    """Stand-in for CSControlPlane: just connected_agents + send_to_agent."""
    def __init__(self, agents, responses=None, exc=None):
        self.connected_agents = agents
        self._responses = responses or {}
        self._exc = exc
        self.sent = []

    async def send_to_agent(self, command, data, agent_id, timeout=None):
        self.sent.append({"agent_id": agent_id, "cmd": command,
                          "data": data, "timeout": timeout})
        if self._exc:
            raise self._exc
        return self._responses.get(
            agent_id,
            {"payload": {"data": {"status": "ERROR", "message": "no stub"}}})


def _spoke() -> CSSpoke:
    return CSSpoke("test-cs", {})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cert_data():
    return {"domain": "a.example.com", "fullchain": _PEM, "privkey": _KEY,
            "chain": "", "identifier": "", "module_type": "simulation"}


def test_install_cert_broadcasts_to_all_connected_agents():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={
            "node-a": {"payload": {"data": {"status": "SUCCESS", "message": "ok-a"}}},
            "node-b": {"payload": {"data": {"status": "SUCCESS", "message": "ok-b"}}},
        })
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "SUCCESS"
    assert res["message"].startswith("deployed to 2/2")
    # Every connected agent was contacted.
    sids = {c["agent_id"] for c in s.control_plane.sent}
    assert sids == {"node-a", "node-b"}
    # The cert material + 620s relay window are passed through to each agent.
    for c in s.control_plane.sent:
        assert c["cmd"] == "INSTALL_CERT"
        assert c["data"]["fullchain"] == _PEM
        assert c["data"]["module_type"] == "simulation"
        assert c["timeout"] == 620.0  # > agent 600s pvenode wait; < hub 640s
    nodes = {n["agent_id"]: n for n in res["nodes"]}
    assert nodes["node-a"]["status"] == "SUCCESS"
    assert nodes["node-b"]["status"] == "SUCCESS"


def test_install_cert_one_agent_error_does_not_abort_others():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={
            "node-a": {"payload": {"data": {"status": "SUCCESS"}}},
            "node-b": {"payload": {"data": {"status": "ERROR",
                                            "message": "pvenode failed"}}},
        })
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"  # not all succeeded
    assert "1/2" in res["message"]
    nodes = {n["agent_id"]: n for n in res["nodes"]}
    assert nodes["node-a"]["status"] == "SUCCESS"
    assert nodes["node-b"]["status"] == "ERROR"
    # Both agents were still contacted (no short-circuit on the ERROR).
    assert {c["agent_id"] for c in s.control_plane.sent} == {"node-a", "node-b"}


def test_install_cert_no_agents_connected_is_error():
    s = _spoke()
    s.control_plane = _FakeCP({})
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"
    assert "no managed pxmx agents connected" in res["message"]
    assert s.control_plane.sent == []


def test_install_cert_explicit_agent_id_targets_one_node():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS",
                                                    "message": "ok"}}}})
    data = dict(_cert_data())
    data["agent_id"] = "node-a"
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "SUCCESS"
    assert [c["agent_id"] for c in s.control_plane.sent] == ["node-a"]


def test_install_cert_explicit_agent_not_connected_is_error():
    s = _spoke()
    s.control_plane = _FakeCP({"node-a": {"hostname": "a"}})
    data = dict(_cert_data())
    data["agent_id"] = "node-z"
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "ERROR"
    assert "node-z" in res["message"]
    assert s.control_plane.sent == []


def test_install_cert_send_to_agent_exception_is_caught_per_agent():
    s = _spoke()
    s.control_plane = _FakeCP({"node-a": {"hostname": "a"}},
                              exc=RuntimeError("relay exploded"))
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"
    assert res["nodes"][0]["status"] == "ERROR"
    assert "relay exploded" in res["nodes"][0]["message"]


def test_install_cert_no_control_plane_is_error():
    s = _spoke()
    s.control_plane = None
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"