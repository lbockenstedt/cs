"""Dongle-quarantine detection sweep (``SimQuotaEngine._quarantine_sweep``).

A T2 (USB-dongle) client that never connected (no SSID / no IP) past the 1h
grace window, and isn't running an exclusion sim, is shed: the engine dispatches
``quarantine_dongle_and_destroy`` to the client's pxmx agent. Storm guard:
>20% per host failed -> bulk alarm, no mass shed. ``ever_connected`` latched True
on a prior connect, the grace window, the exclusion set, and the T2-only scope
all suppress the shed.

Self-contained fakes (no real deploy / control_plane / sim_config).
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sim_quota_engine import (SimQuotaEngine,  # noqa: E402
                              QT_EXCLUDE_SIMS_DEFAULT, QT_GRACE_S_DEFAULT)


class _Reg:
    def __init__(self, clients):
        self.clients = {h: dict(e) for h, e in clients.items()}

    def get_all(self):
        return {h: dict(e) for h, e in self.clients.items()}


class _Store:
    def __init__(self, csc=None):
        self._csc = csc or {}

    def get_central_sites_config(self):
        return dict(self._csc)

    def get_pxmx_site_map(self):
        return {}


class _Deploy:
    """Richer fake: name→vmid + per-host usb_state (vmid→bus) + name→host."""
    def __init__(self, name_to_vmid, host_usb_state, name_to_host):
        self._n2v = name_to_vmid
        self._host_usb = host_usb_state  # {host: [{vmid,bus_path}, ...]}
        self._n2h = name_to_host
        self.proxmox_states = {
            host: {"usb_state": entries} for host, entries in host_usb_state.items()
        }

    def usb_vmid_index(self):
        usb_vmids = set()
        name_to_vmid = {}
        for host, entries in self._host_usb.items():
            for u in entries:
                if u.get("vmid") not in (None, ""):
                    usb_vmids.add(str(u["vmid"]))
        for nm, vid in self._n2v.items():
            name_to_vmid[nm.lower()] = str(vid)
        return usb_vmids, name_to_vmid

    def name_to_host(self):
        return dict(self._n2h)


class _CP:
    def __init__(self):
        self.sent = []

    async def send_to_agent(self, cmd_type, data, agent_id=None, timeout=15.0):
        self.sent.append({"type": cmd_type, "data": data, "agent_id": agent_id})
        return {"status": "SUCCESS"}


class _Settings:
    config_dir = None


class _Spoke:
    def __init__(self, clients, deploy, csc=None):
        self.registry = _Reg(clients)
        self.local_store = Store = _Store(csc)
        self.settings = _Settings()
        self.deploy = deploy
        self.control_plane = _CP()
        self.data_dir = None


def _t2_client(host, *, ip="", ssid="", ever_connected=False, age=4000.0,
               active_sims=None, vmid=100, bus="3-1"):
    return {
        "hostname": host, "tier": "t2", "has_usb": True,
        "ip": ip, "connected_ssid": ssid, "ever_connected": ever_connected,
        "first_seen": time.time() - age, "last_seen": time.time(),
        "active_simulations": active_sims or [],
    }


def _eng(spoke):
    e = SimQuotaEngine(spoke)
    e._refresh_host_index()  # populate _name_to_host from deploy.name_to_host
    return e


def _sweep(eng):
    """Run the (synchronous) sweep inside a live event loop so its
    ``asyncio.create_task(self._qt_shed(...))`` dispatches, then yield twice to
    let the fired task run ``send_to_agent``."""
    now = time.time()

    async def _go():
        eng._quarantine_sweep(now)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.new_event_loop().run_until_complete(_go())


def test_never_connected_t2_is_shed(tmp_path):
    name_to_vmid = {"kbell-01": 100}
    deploy = _Deploy(name_to_vmid,
                     {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=4000)},
                   deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert len(spoke.control_plane.sent) == 1
    s = spoke.control_plane.sent[0]
    assert s["type"] == "CS_COMMAND"
    assert s["data"]["action"] == "quarantine_dongle_and_destroy"
    assert s["data"]["vmid"] == 100
    assert s["data"]["bus_path"] == "3-1"
    assert s["agent_id"] == "hostA"


def test_within_grace_window_not_shed(tmp_path):
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=60)},  # < 1h grace
                   deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert spoke.control_plane.sent == []


def test_ever_connected_not_shed(tmp_path):
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=4000, ever_connected=True)},
                   deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert spoke.control_plane.sent == []  # mid-run drop is out of scope


def test_exclusion_sim_not_shed(tmp_path):
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=4000,
                                           active_sims=["dhcp_fail"])},
                   deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert spoke.control_plane.sent == []  # no-IP is the point of dhcp_fail


def test_non_exclusion_sim_is_shed(tmp_path):
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=4000,
                                           active_sims=["www_traffic"])},
                   deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert len(spoke.control_plane.sent) == 1  # traffic sim expects an IP


def test_t1_client_not_shed(tmp_path):
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    c = _t2_client("kbell-01", age=4000)
    c["tier"] = "t1"  # wired / PCI passthrough — no swappable dongle
    spoke = _Spoke({"kbell-01": c}, deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert spoke.control_plane.sent == []


def test_bulk_failure_suppresses_shed(tmp_path):
    # 3 of 4 T2 clients on hostA never connected → 75% > 20% → bulk, no shed.
    clients = {f"c{i}": _t2_client(f"c{i}", age=4000, vmid=100 + i, bus=f"3-{i}")
               for i in range(4)}
    # c0 got an IP (connected) so it's not failed; c1,c2,c3 failed = 75%
    clients["c0"]["ip"] = "10.0.0.5"
    clients["c0"]["ever_connected"] = True
    name_to_vmid = {f"c{i}": 100 + i for i in range(4)}
    deploy = _Deploy(name_to_vmid,
                     {"hostA": [{"vmid": 100 + i, "bus_path": f"3-{i}"}
                                for i in range(4)]},
                     {f"c{i}": "hostA" for i in range(4)})
    spoke = _Spoke(clients, deploy, csc={})
    eng = _eng(spoke)
    _sweep(eng)
    assert spoke.control_plane.sent == []  # bulk → infra, not dongles
    assert "hostA" in eng._qt_telemetry["bulk_hosts"]
    assert eng._qt_telemetry["per_host"]["hostA"]["failed"] == 3
    assert eng._qt_telemetry["per_host"]["hostA"]["total"] == 4


def test_admin_exclude_sims_override(tmp_path):
    # Admin narrows the exclusion set to ONLY dhcp_fail → assoc_fail is now a
    # shed candidate (no-IP on assoc_fail is expected, but admin opted it out of
    # the exclusion set → treat as a real failure).
    deploy = _Deploy({"kbell-01": 100}, {"hostA": [{"vmid": 100, "bus_path": "3-1"}]},
                     {"kbell-01": "hostA"})
    spoke = _Spoke({"kbell-01": _t2_client("kbell-01", age=4000,
                                           active_sims=["assoc_fail"])},
                   deploy, csc={"qt_exclude_sims": ["dhcp_fail"]})
    eng = _eng(spoke)
    _sweep(eng)
    assert len(spoke.control_plane.sent) == 1


def test_no_deploy_no_op(tmp_path):
    # A spoke with no deploy (pre-agent / test mode) sweeps without raising.
    spoke = _Spoke({}, _Deploy({}, {}, {}), csc={})
    spoke.deploy = None
    eng = _eng(spoke)
    eng._quarantine_sweep(time.time())  # must not raise
    assert eng._qt_telemetry == {}


def test_defaults_are_the_locked_decisions():
    assert QT_GRACE_S_DEFAULT == 3600.0
    assert set(QT_EXCLUDE_SIMS_DEFAULT) == {"dhcp_fail", "assoc_fail",
                                            "ssidpw_fail", "auth_fail",
                                            "port_flap"}