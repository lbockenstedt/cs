"""Per-host T1 opt-out (``t1_exclude_hosts``).

Regression: a pxmx server the operator listed in ``t1_exclude_hosts`` (Setup →
hub config) does NOT PCI-pass its T1 card — its clients run as USB/T2 and must
never be classified/deployed as T1. The config was stored + emitted into the
agent payload but had NO consumer anywhere, so excluded hosts kept deploying T1.

``build_client_rows`` is the authoritative spoke-side classifier (the agent
never stamps a per-VM tier; a client with no USB dongle falls through to T1).
These tests pin the exclusion there: an excluded host's would-be-T1 client is
forced to T2 (row + persisted ``tier_updates``), while non-excluded hosts and
already-T2/T3 clients are untouched.
"""
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from client_rows import build_client_rows  # noqa: E402


class _Reg:
    def __init__(self, clients):
        self._c = clients

    def get_all(self):
        return self._c


class _Deploy:
    """Minimal stand-in exposing exactly the methods build_client_rows calls."""

    def __init__(self, name_to_host, has_usb_map, tiers=None):
        self._n2h = name_to_host
        self._has_usb = has_usb_map          # {hostname: (vmid, has_usb)}
        self._tiers = tiers or {}            # {str(vmid): 't1'|'t2'|'t3'}
        self.proxmox_states = {}

    def usb_vmid_index(self):
        return set(), {}

    def vm_tier_index(self):
        return dict(self._tiers)

    def vm_health_index(self):
        return {}

    def name_to_host(self):
        return dict(self._n2h)

    def client_has_usb(self, hostname, c, usb_vmids, name_to_vmid):
        return self._has_usb.get(hostname, (None, False))


class _LS:
    def __init__(self, excl):
        self._excl = excl

    def get_hub_config(self):
        return {"hub_config": {"t1_exclude_hosts": list(self._excl)}}


class _Spoke:
    def __init__(self, reg, deploy, ls):
        self.registry = reg
        self.deploy = deploy
        self.local_store = ls


def _client(host):
    return {"hostname": host, "last_seen": time.time() - 30,
            "simulation_id": "s0", "config": {}}


def _rows_by_host(rows):
    return {r["hostname"]: r for r in rows}


def test_excluded_host_would_be_t1_client_forced_to_t2():
    reg = _Reg({"clienta": _client("clienta")})
    deploy = _Deploy(name_to_host={"clienta": "pxmx-cs-svr-06"},
                     has_usb_map={"clienta": ("90001", False)})   # no dongle → T1
    spoke = _Spoke(reg, deploy, _LS(["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clienta"]
    assert r["tier"] == "t2", "excluded host's would-be-T1 client must become T2"
    assert tier_updates.get("clienta", {}).get("tier") == "t2", "must persist"


def test_non_excluded_host_t1_client_untouched():
    reg = _Reg({"clientb": _client("clientb")})
    deploy = _Deploy(name_to_host={"clientb": "pxmx-cs-svr-01"},
                     has_usb_map={"clientb": ("90002", False)})
    spoke = _Spoke(reg, deploy, _LS(["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clientb"]
    # Left as-is: no agent tier stamp → None (csClassifyClient renders T1 via
    # has_usb=False). The exclusion must NOT touch non-listed hosts.
    assert r["tier"] is None
    assert "clientb" not in tier_updates


def test_excluded_host_real_t2_dongle_client_unchanged():
    # A client WITH a USB dongle on an excluded host is already T2 — the opt-out
    # must not spuriously stamp/persist it.
    reg = _Reg({"clientc": _client("clientc")})
    deploy = _Deploy(name_to_host={"clientc": "pxmx-cs-svr-06"},
                     has_usb_map={"clientc": ("90003", True)})    # real dongle
    spoke = _Spoke(reg, deploy, _LS(["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clientc"]
    assert r["has_usb"] is True
    assert r["tier"] is None            # csClassifyClient → T2 from has_usb
    assert "clientc" not in tier_updates


def test_excluded_host_t3_client_not_downgraded():
    # A T3 (PCI IoT) client on an excluded host keeps T3 — the opt-out is T1→T2
    # only, never T3→T2.
    reg = _Reg({"clientd": _client("clientd")})
    deploy = _Deploy(name_to_host={"clientd": "pxmx-cs-svr-06"},
                     has_usb_map={"clientd": ("90004", False)},
                     tiers={"90004": "t3"})
    spoke = _Spoke(reg, deploy, _LS(["pxmx-cs-svr-06"]))
    rows, _ = build_client_rows(spoke)
    assert _rows_by_host(rows)["clientd"]["tier"] == "t3"


def test_prefix_match_excludes_a_numbered_range():
    reg = _Reg({"cliente": _client("cliente")})
    deploy = _Deploy(name_to_host={"cliente": "sim-svr-05"},
                     has_usb_map={"cliente": ("90005", False)})
    spoke = _Spoke(reg, deploy, _LS(["sim-svr"]))   # bare prefix
    rows, _ = build_client_rows(spoke)
    assert _rows_by_host(rows)["cliente"]["tier"] == "t2"


def test_empty_exclude_list_is_a_noop():
    reg = _Reg({"clientf": _client("clientf")})
    deploy = _Deploy(name_to_host={"clientf": "pxmx-cs-svr-06"},
                     has_usb_map={"clientf": ("90006", False)})
    spoke = _Spoke(reg, deploy, _LS([]))
    rows, tier_updates = build_client_rows(spoke)
    assert _rows_by_host(rows)["clientf"]["tier"] is None
    assert tier_updates == {}
