"""sim-tag map: liveness window + the hostname join used to dispatch.

Both regressions were found the same way — Proxmox showed tags that disagreed
with the Engine State ledger, and nothing logged why.

1. The map declared a client offline after 300s (borrowed from QT_ONLINE_S, the
   DONGLE QUARANTINE window) and handed back an EMPTY tag set. But the quota
   engine keeps an offline-but-alive runner ASSIGNED and PRODUCING for a full
   hour, so a client quiet for 5-60 minutes had its sim- tags STRIPPED off a VM
   that was still running the simulation.

2. The dispatcher joined telemetry hostnames to connected-agent hostnames by
   exact string match. An FQDN/case difference after a host rebuild made every
   sweep skip that host silently, so its VMs were never tagged (or froze).
"""
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from token_store import (  # noqa: E402
    _ONLINE_WINDOW_S, _client_sim_map, compute_sim_tag_map, norm_hostname)


class _Reg:
    def __init__(self, clients):
        self._c = clients

    def get_all(self):
        return self._c


def _client(host, age_s, sims):
    return {"hostname": host, "last_seen": time.time() - age_s,
            "active_simulations": sims}


def test_window_tracks_the_engine_offline_ttl_not_the_quarantine_window():
    # The engine's OFFLINE_TTL_S is 3600. If this ever drops back to the 300s
    # quarantine window, still-assigned runners lose their tags again.
    assert _ONLINE_WINDOW_S == 3600.0


def test_briefly_quiet_runner_keeps_its_tags():
    reg = _Reg({"a": _client("a", 600, ["dns_latency"])})   # 10 min quiet
    assert _client_sim_map(reg) == {"a": ["sim-dns-latency"]}


def test_fresh_client_tagged_and_long_dead_client_cleared():
    reg = _Reg({"fresh": _client("fresh", 30, ["ssidpw_fail"]),
                "gone": _client("gone", 7200, ["dns_fail"])})
    m = _client_sim_map(reg)
    assert m["fresh"] == ["sim-ssidpw-fail"]
    assert m["gone"] == [], "past the engine TTL the sim- tags are cleared"


def test_client_with_no_last_seen_is_not_tagged():
    reg = _Reg({"x": {"hostname": "x", "active_simulations": ["dns_fail"]}})
    assert _client_sim_map(reg) == {"x": []}


# ── the hostname join ────────────────────────────────────────────────────────
def test_norm_hostname_joins_fqdn_and_case_variants():
    assert norm_hostname("PXMX-CS-SVR-03.lrbtechnologies.com") == "pxmx-cs-svr-03"
    assert norm_hostname("  pxmx-cs-svr-03  ") == "pxmx-cs-svr-03"
    assert norm_hostname(None) == ""


def test_norm_hostname_never_merges_different_servers():
    # The fallback must not collapse distinct hosts onto one agent — that would
    # dispatch one server's tag map to another server's agent.
    assert norm_hostname("pxmx-cs-svr-01") != norm_hostname("pxmx-cs-svr-02")


# ── the VM -> client join the map performs ───────────────────────────────────
class _Deploy:
    def __init__(self, states):
        self.proxmox_states = states


def test_mis_stamped_clone_is_omitted_not_mis_tagged():
    # A clone whose in-guest hostname stayed `sim-rpi-0000` beacons under THAT
    # name, so the VM's own name matches no client. It must be OMITTED entirely
    # (agent never touches its tags) rather than inheriting another client's.
    reg = _Reg({"sim-rpi-0000": _client("sim-rpi-0000", 30, ["dns_fail"])})
    deploy = _Deploy({"svr-04": {"vms": [
        {"vmid": 90083, "name": "wmiller"},          # mis-stamped clone
        {"vmid": 90084, "name": "sim-rpi-0000"},     # the identity it reports as
    ]}})
    out = compute_sim_tag_map(deploy, reg)
    assert "90083" not in out.get("svr-04", {}), "no client match → leave tags alone"
    assert out["svr-04"]["90084"] == ["sim-dns-fail"]


def test_templates_and_lxc_are_never_tagged():
    reg = _Reg({"c1": _client("c1", 30, ["dns_fail"])})
    deploy = _Deploy({"h": {"vms": [
        {"vmid": 1, "name": "c1", "is_template": True},
        {"vmid": 2, "name": "c1", "type": "lxc"},
        {"vmid": 3, "name": "c1"},
    ]}})
    assert compute_sim_tag_map(deploy, reg) == {"h": {"3": ["sim-dns-fail"]}}
