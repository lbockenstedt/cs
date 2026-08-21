"""Per-host T3 opt-out (``t3_exclude_hosts``), mirroring test_t1_exclude_hosts.py.

Regression parity: T1 had ``_host_t1_excluded`` enforced both at the agent's PCI
passthrough call site (``usb_provision.py``) and in the spoke's classifier
(``build_client_rows``); T3 only had the agent-side vidpid pool and no per-host
opt-out anywhere. These tests pin the classifier side of the new T3 opt-out: an
excluded host's T3-tiered client is forced to T2 (row + persisted
``tier_updates``), while non-excluded hosts, T1-classified clients, and
T2-dongle clients are all untouched by the T3 list.
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
    def __init__(self, t1_excl=(), t3_excl=()):
        self._t1 = t1_excl
        self._t3 = t3_excl

    def get_hub_config(self):
        return {"hub_config": {"t1_exclude_hosts": list(self._t1),
                                "t3_exclude_hosts": list(self._t3)}}


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


def test_excluded_host_t3_client_forced_to_t2():
    reg = _Reg({"clienta": _client("clienta")})
    deploy = _Deploy(name_to_host={"clienta": "pxmx-cs-svr-06"},
                     has_usb_map={"clienta": ("90001", False)},
                     tiers={"90001": "t3"})
    spoke = _Spoke(reg, deploy, _LS(t3_excl=["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clienta"]
    assert r["tier"] == "t2", "excluded host's T3 client must be forced to T2"
    assert tier_updates.get("clienta", {}).get("tier") == "t2", "must persist"


def test_non_excluded_host_t3_client_untouched():
    reg = _Reg({"clientb": _client("clientb")})
    deploy = _Deploy(name_to_host={"clientb": "pxmx-cs-svr-01"},
                     has_usb_map={"clientb": ("90002", False)},
                     tiers={"90002": "t3"})
    spoke = _Spoke(reg, deploy, _LS(t3_excl=["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clientb"]
    assert r["tier"] == "t3"
    # The live vm_tier_index resolution always persists (unrelated to
    # exclusion) — what matters is it's the ORIGINAL t3, not downgraded.
    assert tier_updates.get("clientb", {}).get("tier") == "t3"


def test_excluded_host_t2_dongle_client_unchanged():
    # A client WITH a USB dongle on a T3-excluded host is already T2 — the
    # opt-out must not spuriously stamp/persist it.
    reg = _Reg({"clientc": _client("clientc")})
    deploy = _Deploy(name_to_host={"clientc": "pxmx-cs-svr-06"},
                     has_usb_map={"clientc": ("90003", True)})    # real dongle
    spoke = _Spoke(reg, deploy, _LS(t3_excl=["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    r = _rows_by_host(rows)["clientc"]
    assert r["has_usb"] is True
    assert r["tier"] is None            # csClassifyClient → T2 from has_usb
    assert "clientc" not in tier_updates


def test_t3_exclude_never_touches_t1_client():
    # A T3-excluded host's would-be-T1 client (no tier stamp, no dongle) is left
    # alone by the T3 list — that's t1_exclude_hosts' job, not t3's.
    reg = _Reg({"clientd": _client("clientd")})
    deploy = _Deploy(name_to_host={"clientd": "pxmx-cs-svr-06"},
                     has_usb_map={"clientd": ("90004", False)})   # no tier stamp
    spoke = _Spoke(reg, deploy, _LS(t3_excl=["pxmx-cs-svr-06"]))
    rows, tier_updates = build_client_rows(spoke)
    assert _rows_by_host(rows)["clientd"]["tier"] is None
    assert "clientd" not in tier_updates


def test_prefix_match_excludes_a_numbered_range():
    reg = _Reg({"cliente": _client("cliente")})
    deploy = _Deploy(name_to_host={"cliente": "sim-svr-05"},
                     has_usb_map={"cliente": ("90005", False)},
                     tiers={"90005": "t3"})
    spoke = _Spoke(reg, deploy, _LS(t3_excl=["sim-svr"]))   # bare prefix
    rows, _ = build_client_rows(spoke)
    assert _rows_by_host(rows)["cliente"]["tier"] == "t2"


def test_empty_exclude_list_is_a_noop():
    reg = _Reg({"clientf": _client("clientf")})
    deploy = _Deploy(name_to_host={"clientf": "pxmx-cs-svr-06"},
                     has_usb_map={"clientf": ("90006", False)},
                     tiers={"90006": "t3"})
    spoke = _Spoke(reg, deploy, _LS())
    rows, tier_updates = build_client_rows(spoke)
    assert _rows_by_host(rows)["clientf"]["tier"] == "t3"
    # Live vm_tier_index resolution always persists; the exclusion noop just
    # means it stays t3 rather than being downgraded.
    assert tier_updates.get("clientf", {}).get("tier") == "t3"


def test_t1_and_t3_exclude_lists_are_independent():
    # A host excluded for T1 only must still downgrade its T3 client... no —
    # must NOT touch a T3 client (t1 list only governs the T1 fallback branch),
    # and a host excluded for T3 only must not touch a would-be-T1 client.
    reg = _Reg({"g1": _client("g1"), "g2": _client("g2")})
    deploy = _Deploy(
        name_to_host={"g1": "pxmx-cs-svr-06", "g2": "pxmx-cs-svr-06"},
        has_usb_map={"g1": ("90007", False), "g2": ("90008", False)},
        tiers={"90008": "t3"})
    spoke = _Spoke(reg, deploy, _LS(t1_excl=["pxmx-cs-svr-06"]))  # T1 only
    rows, _ = build_client_rows(spoke)
    by_host = _rows_by_host(rows)
    assert by_host["g1"]["tier"] == "t2"   # would-be-T1 → forced T2 by t1 list
    assert by_host["g2"]["tier"] == "t3"   # T3 client untouched (no t3 exclusion)
