"""Tests for the SimQuotaEngine reconcile loop (cs spoke).

Pool-first selection, self-heal on offline runners, release on quota removal,
trim when over N, wrong-site skip, and provenance (manual pins untouched). Uses
a stub spoke + in-memory fake registry so no sim_config / filesystem state is
needed (config_dir=None → _effective_site reads c['config']['wsite']).
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

from sim_quota_engine import SimQuotaEngine  # noqa: E402


class _FakeRegistry:
    def __init__(self, clients):
        # clients: {hostname: entry(dict with last_seen, config, overrides)}
        self.clients = {h: dict(e) for h, e in clients.items()}

    def get_all(self):
        # Match the real ClientRegistry.get_all() contract: a shallow copy per
        # entry so the engine's pre-sweep snapshot isn't mutated by set_overrides
        # mid-sweep (the ledger records the PRE-assign effective site).
        return {h: dict(e) for h, e in self.clients.items()}

    def get(self, h):
        e = self.clients.get(h)
        return dict(e) if e is not None else None

    async def set_overrides(self, hostname, overrides):
        e = self.clients.setdefault(hostname, {"hostname": hostname})
        cur = dict(e.get("overrides") or {})
        cur.update(overrides)
        e["overrides"] = cur
        # Reflect a turned-on sim into the client's config so _has_sim_on sees
        # it via the override path (overrides win in _has_sim_on).
        return dict(e)


class _FakeLocalStore:
    def __init__(self, quotas, pxmx_site_map=None):
        self._q = quotas
        self._map = pxmx_site_map or {}

    def get_effective_sim_quotas(self):
        return list(self._q)

    def get_pxmx_site_map(self):
        return dict(self._map)


class _FakeDeploy:
    """Stand-in for ProxmoxDeploy — just the name→host index the engine reads."""
    def __init__(self, name_to_host):
        self._n2h = name_to_host

    def name_to_host(self):
        return dict(self._n2h)


class _FakeSettings:
    config_dir = None  # skip sim_config in _effective_site


class _FakeSpoke:
    def __init__(self, clients, quotas, tmp_path, pxmx_site_map=None,
                 name_to_host=None):
        self.registry = _FakeRegistry(clients)
        self.local_store = _FakeLocalStore(quotas, pxmx_site_map)
        self.settings = _FakeSettings()
        self.data_dir = str(tmp_path)
        self.deploy = _FakeDeploy(name_to_host or {})


def _client(host, site, online=True, overrides=None, sim_flags=None):
    cfg = {"wsite": site}
    if sim_flags:
        cfg.update(sim_flags)
    return {
        "hostname": host, "last_seen": time.time() if online else 0,
        "config": cfg, "overrides": overrides or {},
    }


def _run(coro):
    return _LOOP.run_until_complete(coro)


# A dedicated, stable loop for every _run() in this module. SimQuotaEngine
# creates an asyncio.Lock in __init__ that binds to whichever loop first runs
# it, so all _run() calls in one test MUST share a loop — and it MUST NOT be the
# global get_event_loop() (sibling test files like test_pxmx_site_map.py /
# test_hub_config.py tear the global loop down to None in their fixtures, which
# would make asyncio.get_event_loop() raise mid-suite on Python 3.9).
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


@pytest.fixture(autouse=True)
def _bind_engine_loop():
    # Rebind the global loop to _LOOP before each test so SimQuotaEngine's
    # asyncio.Lock() (eagerly bound on Python 3.9) finds it even after a sibling
    # test file tore the global loop down to None.
    asyncio.set_event_loop(_LOOP)
    yield


def _offline_recently():
    """last_seen just past the 300s online window but within the 3600s dead-TTL:
    offline (so it needs a substitute) but NOT dead (so it's kept in the ledger)."""
    return time.time() - 400


def test_reconcile_picks_from_pool_to_count(tmp_path):
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assert actions["assigned"] == 3
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert len(assigned) == 3
    for h in assigned:
        assert spoke.registry.clients[h]["overrides"]["dns_fail"] == "on"


def test_reconcile_self_heals_offline_runner(tmp_path):
    # N=3. c0,c1,c2 assigned. c0 goes offline → kept in the ledger (the sim
    # keeps running on the VM through a WS blip), producing drops to 2 → a
    # substitute c3 is picked up so producing stays at 3. The offline c0 is
    # NOT released (still in the ledger); when it returns, producing goes to 4
    # → over N → the substitute is trimmed back.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    offline = first[0]
    spoke.registry.clients[offline]["last_seen"] = _offline_recently()   # WS blip
    actions = _run(eng.reconcile())
    assert actions["assigned"] == 1 and actions["released"] == 0
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert offline in assigned              # offline runner kept, not dropped
    # 3 producing + the kept-offline one = 4 ledger entries, sim still on N=3.
    assert len(assigned) == 4
    # Original returns → producing 4 > 3 → trim one back to 3.
    spoke.registry.clients[offline]["last_seen"] = time.time()
    actions = _run(eng.reconcile())
    assert actions["released"] == 1
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 3


def test_reconcile_releases_when_quota_removed(tmp_path):
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert len(eng._ledger) == 1
    spoke.local_store._q = []                # quota removed
    actions = _run(eng.reconcile())
    assert actions["released"] == 3
    assert eng._ledger == {}
    for h in clients:
        assert spoke.registry.clients[h]["overrides"].get("dns_fail") == "off"


def test_reconcile_does_not_touch_manual_pins(tmp_path):
    # c0 is manually pinned to assoc_fail (a human override) — not free, not
    # touched. c1..c4 are free runners.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    clients["c0"]["overrides"] = {"assoc_fail": "on"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert "c0" not in assigned               # manual pin left alone
    assert len(assigned) == 3
    # c0's manual override is intact, engine never touched dns_fail on it.
    assert "dns_fail" not in clients["c0"]["overrides"]
    assert clients["c0"]["overrides"]["assoc_fail"] == "on"


def test_reconcile_trims_extras_when_count_reduced(tmp_path):
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 3
    spoke.local_store._q = [{"alert_id": "A", "sim_id": "dns_fail", "count": 1, "site": "MIA", "enabled": True}]
    actions = _run(eng.reconcile())
    assert actions["released"] == 2
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 1


def test_reconcile_skips_wrong_site(tmp_path):
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    # No MIA clients → nothing assigned, ledger entry empty.
    assert "alert:A:MIA" not in eng._ledger or not eng._ledger["alert:A:MIA"]["clients"]


def test_reconcile_blank_site_uses_any_online_client(tmp_path):
    clients = {f"c{i}": _client(f"c{i}", f"S{i}") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 2, "site": "", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assert actions["assigned"] == 2
    assert len(eng._ledger["alert:A:"]["clients"]) == 2


def test_reconcile_resolves_site_via_hosting_pxmx_server(tmp_path):
    # px1 is assigned to MIA, px2 to DFW. c0/c1 live on px1, c2/c3 on px2.
    # All clients' bucket wsite is "DFW" (so without the server map the engine
    # would see them all as DFW and none eligible for MIA). With the map, the
    # engine resolves c0/c1's site via px1 → MIA and fills the MIA quota from
    # them, ignoring px2's clients.
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(4)}
    n2h = {"c0": "px1", "c1": "px1", "c2": "px2", "c3": "px2"}
    site_map = {"px1": "MIA", "px2": "DFW"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 2, "site": "MIA", "enabled": True}],
                      tmp_path, pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert actions["assigned"] == 2
    assert set(assigned) == {"c0", "c1"}     # only px1's (MIA) clients
    # px2's clients are NOT re-homed or touched.
    assert "c2" not in assigned and "c3" not in assigned


def test_reconcile_wsite_override_wins_over_server_site(tmp_path):
    # c0 lives on px1 (MIA-assigned) but a manual wsite override pins it to DFW.
    # The override wins → c0 is NOT eligible for the MIA quota; the engine picks
    # other free runners on px1 instead.
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(4)}
    clients["c0"]["overrides"] = {"wsite": "DFW"}
    n2h = {"c0": "px1", "c1": "px1", "c2": "px1", "c3": "px2"}
    site_map = {"px1": "MIA", "px2": "DFW"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 2, "site": "MIA", "enabled": True}],
                      tmp_path, pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assigned = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert "c0" not in assigned          # override pinned it to DFW
    assert assigned == {"c1", "c2"}      # other px1 (MIA) free runners


def test_reconcile_rehome_disabled_underfills_when_site_short(tmp_path):
    # MIA quota N=3 but only c0 is in MIA (px1); c1-c4 are on px2 (DFW). Without
    # rehome the engine respects physical placement → only 1 assigned, no
    # cross-site borrowing, no wsite overrides set on DFW clients.
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(5)}
    n2h = {"c0": "px1", "c1": "px2", "c2": "px2", "c3": "px2", "c4": "px2"}
    site_map = {"px1": "MIA", "px2": "DFW"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 3, "site": "MIA", "rehome": False,
                                  "enabled": True}],
                      tmp_path, pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert actions["assigned"] == 1 and assigned == ["c0"]
    # DFW clients untouched (no wsite override).
    for h in ("c1", "c2", "c3", "c4"):
        assert "wsite" not in (spoke.registry.clients[h].get("overrides") or {})


def test_reconcile_rehome_borrows_cross_site_when_enabled(tmp_path):
    # Same layout, rehome=True → c0 in-site + c1,c2 borrowed from DFW and
    # re-homed (wsite=MIA). The ledger records their original site (DFW) so a
    # later release reverts.
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(5)}
    n2h = {"c0": "px1", "c1": "px2", "c2": "px2", "c3": "px2", "c4": "px2"}
    site_map = {"px1": "MIA", "px2": "DFW"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 3, "site": "MIA", "rehome": True,
                                  "enabled": True}],
                      tmp_path, pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assigned = eng._ledger["alert:A:MIA"]["clients"]
    assert actions["assigned"] == 3
    assert set(assigned.keys()) == {"c0", "c1", "c2"}
    # Borrowed clients re-homed to MIA; ledger from_site preserves DFW.
    assert spoke.registry.clients["c1"]["overrides"]["wsite"] == "MIA"
    assert spoke.registry.clients["c2"]["overrides"]["wsite"] == "MIA"
    assert assigned["c1"] == "DFW" and assigned["c2"] == "DFW"


def test_reconcile_rehome_release_reverts_wsite(tmp_path):
    # After a rehome assign, removing the quota releases the borrowed clients
    # and reverts wsite back to their original site (DFW).
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(5)}
    n2h = {"c0": "px1", "c1": "px2", "c2": "px2", "c3": "px2", "c4": "px2"}
    site_map = {"px1": "MIA", "px2": "DFW"}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 3, "site": "MIA", "rehome": True,
                                  "enabled": True}],
                      tmp_path, pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert spoke.registry.clients["c1"]["overrides"]["wsite"] == "MIA"
    # Remove the quota → engine releases all + reverts wsite.
    spoke.local_store._q = []
    _run(eng.reconcile())
    assert eng._ledger == {}
    # c1's wsite reverted to DFW (from_site), sim turned off.
    assert spoke.registry.clients["c1"]["overrides"].get("wsite") == "DFW"
    assert spoke.registry.clients["c1"]["overrides"].get("dns_fail") == "off"


def test_reconcile_substitute_stops_when_original_returns(tmp_path):
    # Quota N=2. c0,c1 assigned. c0 goes offline → c2 substitutes. c0 returns
    # → over N by 1 → one (the substitute c2, last in) is released.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(4)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 2, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert len(assigned) == 2
    offline = assigned[0]
    spoke.registry.clients[offline]["last_seen"] = _offline_recently()
    _run(eng.reconcile())                     # substitute picked up
    # Ledger now holds the offline-kept original + 2 producing = 3; producing=N=2.
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 3
    spoke.registry.clients[offline]["last_seen"] = time.time()  # returns
    actions = _run(eng.reconcile())           # over N → trim back to 2
    assert actions["released"] == 1
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 2


# ── multi_capable exclusivity / packing (Chunk 4) ────────────────────────────
def test_reconcile_exclusive_quotas_one_failure_sim_per_client(tmp_path):
    # Two EXCLUSIVE quotas (dns_fail N=2, assoc_fail N=2) over 4 free clients.
    # Each client gets at most one failure sim: dns_fail takes c0,c1; assoc_fail
    # must skip them (they're running an exclusive sim) and take c2,c3 instead.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(4)}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "A", "sim_id": "dns_fail", "count": 2, "site": "MIA", "enabled": True},
        {"alert_id": "B", "sim_id": "assoc_fail", "count": 2, "site": "MIA", "enabled": True},
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    dns = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    assoc = set(eng._ledger["alert:B:MIA"]["clients"].keys())
    assert dns == {"c0", "c1"} and assoc == {"c2", "c3"}   # no overlap
    assert dns.isdisjoint(assoc)                            # one failure sim/client


def test_reconcile_multi_capable_packs_two_traffic_sims_on_same_clients(tmp_path):
    # Two MULTI-CAPABLE quotas (ping_test N=2, download N=2) over just 2 clients.
    # Traffic sims stack → both quotas fill to 2 on the SAME two clients.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(2)}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "A", "sim_id": "ping_test", "count": 2, "site": "MIA",
         "multi_capable": True, "enabled": True},
        {"alert_id": "B", "sim_id": "download", "count": 2, "site": "MIA",
         "multi_capable": True, "enabled": True},
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    ping = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    dl = set(eng._ledger["alert:B:MIA"]["clients"].keys())
    assert ping == {"c0", "c1"} and dl == {"c0", "c1"}      # packed, not disjoint
    # Both sims ON on both clients.
    for h in ("c0", "c1"):
        assert spoke.registry.clients[h]["overrides"]["ping_test"] == "on"
        assert spoke.registry.clients[h]["overrides"]["download"] == "on"


def test_reconcile_multi_capable_packs_onto_exclusive_runner(tmp_path):
    # One EXCLUSIVE (dns_fail N=1) + one MULTI (ping_test N=1) over 1 client.
    # The traffic sim packs onto the client already running the failure sim.
    clients = {"c0": _client("c0", "MIA")}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "A", "sim_id": "dns_fail", "count": 1, "site": "MIA", "enabled": True},
        {"alert_id": "B", "sim_id": "ping_test", "count": 1, "site": "MIA",
         "multi_capable": True, "enabled": True},
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0"}
    assert set(eng._ledger["alert:B:MIA"]["clients"].keys()) == {"c0"}
    ov = spoke.registry.clients["c0"]["overrides"]
    assert ov["dns_fail"] == "on" and ov["ping_test"] == "on"


def test_reconcile_exclusive_skips_client_running_exclusive_via_bucket(tmp_path):
    # c0's BUCKET default already runs assoc_fail (an exclusive sim) — no manual
    # override, just the bucket profile. A dns_fail quota must NOT stack onto it
    # (one failure sim per client, regardless of source); it picks c1 instead.
    clients = {"c0": _client("c0", "MIA", sim_flags={"assoc_fail": "on"}),
               "c1": _client("c1", "MIA")}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 1, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c1"}
    # c0's bucket assoc_fail left intact, engine never touched dns_fail on it.
    assert "dns_fail" not in (clients["c0"].get("overrides") or {})