"""PXMX_DRIVE_HEALTH / PXMX_INSTALL_SSACLI relay handlers on CSSpoke.

In the all-cs-hosted topology the pxmx host-agents dial the cs spoke (not a
dedicated pxmx spoke), and the hub's drive-health route already treats
"simulation" spokes as valid hypervisor targets (see
hub_spoke_registry.get_hypervisor_spokes_for_tenant), so it sends the raw
PXMX_DRIVE_HEALTH/PXMX_INSTALL_SSACLI command directly here. These handlers
were missing, so the command fell through to the generic
"Unknown command: PXMX_DRIVE_HEALTH" error and the CS-hosted nodes' Drive
Health panel showed "Agent vunknown" / 0 drives even though the same pxmx
agent (with drive-health support) is running on those boxes. This locks in
the relay + node-resolution + response-shape behavior, mirroring
ProxmoxSpoke's PXMX_DRIVE_HEALTH/PXMX_INSTALL_SSACLI branches
(proxmox_spoke.py:340-374).
"""
import asyncio

from cs_spoke import CSSpoke


class _FakeCP:
    def __init__(self, agents, response=None, exc=None):
        self.connected_agents = agents
        self.sent = []
        self._response = response
        self._exc = exc

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=None):
        self.sent.append((cmd, data, agent_id, timeout))
        if self._exc:
            raise self._exc
        return self._response


def test_drive_health_resolves_agent_by_node_and_relays():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP(
            {"pxmx-cs-svr-05": {"hostname": "pxmx-cs-svr-05", "cluster_name": "cs-05"}},
            response={"payload": {"data": {"status": "SUCCESS", "drives": [
                {"device": "/dev/sda", "wear": 12}]}}},
        )
        res = await spoke._dispatch_agents(
            "PXMX_DRIVE_HEALTH", {"node": "pxmx-cs-svr-05"})
        assert res["status"] == "SUCCESS"
        assert res["drives"] == [{"device": "/dev/sda", "wear": 12}]
        assert res["cluster"] == "cs-05"
        cmd, data, agent_id, timeout = spoke.control_plane.sent[0]
        assert cmd == "PXMX_DRIVE_HEALTH"
        assert data == {}
        assert agent_id == "pxmx-cs-svr-05"
        assert timeout == 30.0

    asyncio.run(_run())


def test_drive_health_explicit_agent_id_wins():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP(
            {"a1": {}, "a2": {}},
            response={"status": "SUCCESS", "drives": []},
        )
        res = await spoke._dispatch_agents(
            "PXMX_DRIVE_HEALTH", {"agent_id": "a2", "node": "unrelated"})
        assert res["status"] == "SUCCESS"
        assert spoke.control_plane.sent[0][2] == "a2"

    asyncio.run(_run())


def test_drive_health_single_agent_fallback():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP(
            {"only-agent": {}}, response={"status": "SUCCESS", "drives": []})
        res = await spoke._dispatch_agents("PXMX_DRIVE_HEALTH", {})
        assert res["status"] == "SUCCESS"
        assert spoke.control_plane.sent[0][2] == "only-agent"

    asyncio.run(_run())


def test_drive_health_no_agent_resolved_errors():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP({})  # nothing connected
        res = await spoke._dispatch_agents(
            "PXMX_DRIVE_HEALTH", {"node": "missing-node"})
        assert res["status"] == "ERROR"
        assert "No agent resolved" in res["message"]

    asyncio.run(_run())


def test_drive_health_no_control_plane_errors():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = None
        res = await spoke._dispatch_agents("PXMX_DRIVE_HEALTH", {"node": "n"})
        assert res["status"] == "ERROR"
        assert "control plane" in res["message"]

    asyncio.run(_run())


def test_drive_health_agent_exception_falls_back_with_empty_drives():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP(
            {"pxmx-cs-svr-01": {"hostname": "pxmx-cs-svr-01"}},
            exc=RuntimeError("agent timeout"))
        res = await spoke._dispatch_agents(
            "PXMX_DRIVE_HEALTH", {"node": "pxmx-cs-svr-01"})
        assert res["status"] == "ERROR"
        assert res["drives"] == []
        assert "agent timeout" in res["message"]
        assert res["cluster"] == "pxmx-cs-svr-01"

    asyncio.run(_run())


def test_install_ssacli_relays_with_120s_timeout_and_installed_fallback():
    async def _run():
        spoke = CSSpoke("test-cs", {})
        spoke.control_plane = _FakeCP(
            {"pxmx-cs-svr-02": {"hostname": "pxmx-cs-svr-02"}},
            response={"status": "SUCCESS", "installed": True},
        )
        res = await spoke._dispatch_agents(
            "PXMX_INSTALL_SSACLI", {"node": "pxmx-cs-svr-02"})
        assert res["status"] == "SUCCESS"
        assert res["installed"] is True
        cmd, data, agent_id, timeout = spoke.control_plane.sent[0]
        assert cmd == "PXMX_INSTALL_SSACLI"
        assert timeout == 120.0

        spoke.control_plane = _FakeCP(
            {"pxmx-cs-svr-02": {"hostname": "pxmx-cs-svr-02"}},
            exc=RuntimeError("boom"))
        res = await spoke._dispatch_agents(
            "PXMX_INSTALL_SSACLI", {"node": "pxmx-cs-svr-02"})
        assert res["status"] == "ERROR"
        assert res["installed"] is False

    asyncio.run(_run())
