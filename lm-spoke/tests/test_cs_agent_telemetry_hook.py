"""Regression test for ``CSControlPlane._on_agent_telemetry`` (``control_plane.py``).

Before this hook existed, CS fell through to the shared
``AgentHostingControlPlane`` no-op default, so ``connected_agents[agent_id]``
never picked up the agent's resolved ``cluster_name``/``nodes``/``vms`` from
an ``AGENT_TELEMETRY`` frame — it stayed at the connect-time default
(``agent_id``, the agent's own hostname) forever. For a Proxmox host hosted
directly by a cs spoke (the split-topology case), that meant the hub's shared
GET_NODE_STATS/PXMX_LIST_VMS aggregator always read the agent's own hostname
as its "cluster" and never saw its telemetry-cached nodes/vms, regardless of
the agent correctly resolving and sending its real Proxmox cluster name on
every tick. This pins the hook now populates the same fields pxmx's
``PxmxControlPlane._on_agent_telemetry`` does.
"""
import asyncio

from control_plane import CSControlPlane


def test_on_agent_telemetry_populates_cluster_name_nodes_vms_and_ts():
    cp = CSControlPlane.__new__(CSControlPlane)
    rec = {"cluster_name": "agent-1", "nodes": [], "vms": [], "agent_metrics": {}}
    data = {
        "cluster_name": "cs-cluster",
        "nodes": {"nodes": [{"node": "pxmx-cs-svr-01", "status": "online"}]},
        "vms": {"vms": [{"vmid": 101, "node": "pxmx-cs-svr-01"}]},
        "metrics": {"cpu_usage": 5.0},
    }
    asyncio.run(cp._on_agent_telemetry("agent-1", rec, data))

    assert rec["cluster_name"] == "cs-cluster"
    assert rec["nodes"] == [{"node": "pxmx-cs-svr-01", "status": "online"}]
    assert rec["vms"] == [{"vmid": 101, "node": "pxmx-cs-svr-01"}]
    assert rec["agent_metrics"] == {"cpu_usage": 5.0}
    assert rec["telemetry_ts"] > 0


def test_on_agent_telemetry_defaults_cluster_name_to_agent_id_when_absent():
    """An agent that hasn't resolved its cluster name yet (still falling back
    to its own hostname) is honored as-is — this hook must not invent a
    cluster name the agent didn't send."""
    cp = CSControlPlane.__new__(CSControlPlane)
    rec = {"cluster_name": "agent-1", "nodes": [], "vms": [], "agent_metrics": {}}
    asyncio.run(cp._on_agent_telemetry("agent-1", rec, {}))

    assert rec["cluster_name"] == "agent-1"
    assert rec["nodes"] == []
    assert rec["vms"] == []
    assert rec["telemetry_ts"] > 0


def test_on_agent_telemetry_none_rec_is_a_noop():
    """A disconnected-before-processing race (rec already evicted) must not
    raise — mirrors the pxmx implementation's ``if rec is not None`` guard."""
    cp = CSControlPlane.__new__(CSControlPlane)
    asyncio.run(cp._on_agent_telemetry("agent-1", None, {"cluster_name": "cs-cluster"}))
