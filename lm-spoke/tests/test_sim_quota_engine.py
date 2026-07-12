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
        return self.clients

    def get(self, h):
        return self.clients.get(h)

    async def set_overrides(self, hostname, overrides):
        e = self.clients.setdefault(hostname, {"hostname": hostname})
        cur = dict(e.get("overrides") or {})
        cur.update(overrides)
        e["overrides"] = cur
        # Reflect a turned-on sim into the client's config so _has_sim_on sees
        # it via the override path (overrides win in _has_sim_on).
        return dict(e)


class _FakeLocalStore:
    def __init__(self, quotas):
        self._q = quotas

    def get_effective_sim_quotas(self):
        return list(self._q)


class _FakeSettings:
    config_dir = None  # skip sim_config in _effective_site


class _FakeSpoke:
    def __init__(self, clients, quotas, tmp_path):
        self.registry = _FakeRegistry(clients)
        self.local_store = _FakeLocalStore(quotas)
        self.settings = _FakeSettings()
        self.data_dir = str(tmp_path)


def _client(host, site, online=True, overrides=None, sim_flags=None):
    cfg = {"wsite": site}
    if sim_flags:
        cfg.update(sim_flags)
    return {
        "hostname": host, "last_seen": time.time() if online else 0,
        "config": cfg, "overrides": overrides or {},
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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