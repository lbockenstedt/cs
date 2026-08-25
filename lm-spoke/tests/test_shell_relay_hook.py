"""SHELL_* relay handlers on CSSpoke (host-shell / xterm terminal).

In the all-cs-hosted topology the pxmx node-agents dial the cs spoke, so — like
the ported VNC_START — the cs spoke must relay the host shell (VM Server →
Terminal) to them. These handlers were missing, so SHELL_START fell through to a
generic error and the hub surfaced "agent refused SHELL_START"; the terminal
never opened. This locks in the SHELL_START/IN/RESIZE/DISCONNECT routing.
"""
import asyncio

from cs_spoke import CSSpoke


class _FakeCP:
    def __init__(self, agents):
        self.connected_agents = agents
        self.sent = []          # (cmd, data, agent_id) via send_to_agent
        self.raw = []           # (agent_id, cmd, data) via send_raw_to_agent
        self.relays = []        # register_console_relay calls

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=None):
        self.sent.append((cmd, data, agent_id))
        return {"status": "OK", "agent_id": agent_id}

    async def send_raw_to_agent(self, agent_id, cmd, data):
        self.raw.append((agent_id, cmd, data))

    def register_console_relay(self, session_id, token, agent_id, kind):
        self.relays.append((session_id, token, agent_id, kind))

    def unregister_console_relay(self, session_id):
        self.relays = [r for r in self.relays if r[0] != session_id]


def test_shell_start_resolves_agent_and_relays():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP({"pxmx-cs-svr-05": {"hostname": "svr-05"}})
        res = await spoke._dispatch_agents("SHELL_START", {
            "session_id": "s1", "agent_id": "pxmx-cs-svr-05",
            "relay_token": "tok",
        })
        assert res.get("status") == "OK"
        assert spoke.control_plane.sent[0][0] == "SHELL_START"
        assert spoke.control_plane.sent[0][2] == "pxmx-cs-svr-05"
        # session→agent recorded + relay token registered
        assert spoke.shell_sessions["s1"] == "pxmx-cs-svr-05"
        assert spoke.control_plane.relays == [("s1", "tok", "pxmx-cs-svr-05", "shell")]

    asyncio.run(_run())


def test_shell_start_single_agent_fallback():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP({"only-agent": {}})
        # No agent_id in the request → the sole connected agent is used.
        res = await spoke._dispatch_agents("SHELL_START", {"session_id": "s2"})
        assert res.get("status") == "OK"
        assert spoke.shell_sessions["s2"] == "only-agent"

    asyncio.run(_run())


def test_shell_start_no_agent_errors():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP({})  # nothing connected
        res = await spoke._dispatch_agents("SHELL_START", {"session_id": "s3"})
        assert res.get("status") == "ERROR"
        assert "SHELL_START" in res.get("message", "")

    asyncio.run(_run())


def test_shell_in_resize_disconnect_route_to_agent():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        cp = _FakeCP({"a1": {}})
        spoke.control_plane = cp
        await spoke._dispatch_agents("SHELL_START", {"session_id": "s1", "agent_id": "a1"})
        await spoke._dispatch_agents("SHELL_IN", {"session_id": "s1", "data": "bA=="})
        await spoke._dispatch_agents("SHELL_RESIZE", {"session_id": "s1", "rows": 40, "cols": 120})
        assert [(a, c) for (a, c, _d) in cp.raw] == [("a1", "SHELL_IN"), ("a1", "SHELL_RESIZE")]
        r = await spoke._dispatch_agents("SHELL_DISCONNECT", {"session_id": "s1"})
        assert r.get("status") == "OK"
        assert "s1" not in spoke.shell_sessions
        assert cp.raw[-1][1] == "SHELL_DISCONNECT"

    asyncio.run(_run())
