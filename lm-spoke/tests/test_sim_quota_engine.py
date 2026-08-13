"""Tests for the SimQuotaEngine reconcile loop (cs spoke).

Pool-first selection, self-heal on offline runners, release on quota removal,
trim when over N, wrong-site skip, and provenance (manual pins untouched). Uses
a stub spoke + in-memory fake registry so no sim_config / filesystem state is
needed (config_dir=None → _effective_site reads c['config']['wsite']).
"""
import asyncio
import random
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sim_quota_engine import (  # noqa: E402
    SimQuotaEngine, OFFLINE_TTL_S, TIER_UPGRADE_PER_SWEEP, GATEWAY_EXEMPT_SIMS)


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


class _ProvRegistry(_FakeRegistry):
    """Fake registry WITH engine-key provenance + deletion revert, so drone
    preemption (a displaced-off recorded in engine_keys, reverted by DELETION on
    release) can be exercised. The plain _FakeRegistry lacks these, so _engine_set
    falls back to set_overrides and _engine_remove sets "off" — fine for the
    other tests, but it can't show a displaced default reverting to its bucket."""
    async def set_engine_overrides(self, hostname, overrides):
        e = self.clients.setdefault(hostname, {"hostname": hostname})
        cur = dict(e.get("overrides") or {})
        cur.update(overrides)
        eng = list(e.get("engine_keys") or [])
        for k in overrides:
            if k not in eng:
                eng.append(k)
        e["overrides"] = cur
        e["engine_keys"] = eng
        return dict(e)

    async def remove_engine_keys(self, hostname, keys):
        e = self.clients.get(hostname)
        if not e:
            return {}
        cur = dict(e.get("overrides") or {})
        eng = list(e.get("engine_keys") or [])
        for k in keys:
            cur.pop(k, None)
            if k in eng:
                eng.remove(k)
        e["overrides"] = cur
        e["engine_keys"] = eng
        return dict(e)


class _FakeLocalStore:
    def __init__(self, quotas, pxmx_site_map=None):
        self._q = quotas
        self._map = pxmx_site_map or {}
        self._csc = {}          # central_sites_config (tier_priority lives here)
        self._matrix = []          # ssid_matrix (cell defs)
        self._weights = []         # ssid_weights (per-cell spread rules)
        self._site_w = {}          # ambient_site_weights (cross-site spread)
        # Sim-stacking config (empty defaults → no stacking, so the harvest tests
        # above are unaffected; the stacking tests below set these).
        self._randomizable = []
        self._sim_weights = {}
        self._sim_shareable = {}
        self._stack_cap = 3
        self._stack_rotation_s = 600
        self._harvest_cooldown_s = 0     # off by default → existing tests unaffected

    def get_randomizable_sims(self):
        return list(self._randomizable)

    def get_sim_weights(self):
        return dict(self._sim_weights)

    def get_sim_shareable(self):
        return dict(self._sim_shareable)

    def get_stack_cap(self):
        return self._stack_cap

    def get_stack_rotation_s(self):
        return self._stack_rotation_s

    def get_harvest_cooldown_s(self):
        return self._harvest_cooldown_s

    def get_effective_sim_quotas(self):
        return list(self._q)

    def get_pxmx_site_map(self):
        return dict(self._map)

    def get_ssid_matrix(self):
        return list(self._matrix)

    def get_ssid_weights(self):
        return list(self._weights)

    def get_ambient_site_weights(self):
        return dict(self._site_w)

    def get_central_sites_config(self):
        return dict(self._csc)


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
                 name_to_host=None, pin_sites=False):
        # pin_sites: PHYSICALLY pin each client to its config.wsite (RF-chamber
        # model) by deriving the name_to_host + pxmx_site_map the engine now reads.
        # The engine no longer treats a bare config.wsite as a physical pin — a
        # client with no pxmx_site_map entry is a tenant-POOL client (assignable
        # to any site via re-home). Tests that assert physical site-binding /
        # re-home must opt in; default off keeps the tenant-pool tests unchanged.
        if pin_sites and pxmx_site_map is None and name_to_host is None:
            def _ws(c):
                return str((c.get("config") or {}).get("wsite") or "").strip()
            name_to_host = {h: f"__host_{_ws(c)}" for h, c in clients.items()}
            pxmx_site_map = {f"__host_{_ws(c)}": _ws(c)
                             for c in clients.values() if _ws(c)}
        self.registry = _FakeRegistry(clients)
        self.local_store = _FakeLocalStore(quotas, pxmx_site_map)
        self.settings = _FakeSettings()
        self.data_dir = str(tmp_path)
        self.deploy = _FakeDeploy(name_to_host or {})


def _client(host, site, online=True, overrides=None, sim_flags=None, adapters=None):
    cfg = {"wsite": site}
    if sim_flags:
        cfg.update(sim_flags)
    # Default to reporting BOTH media types so the generic fixture is neutral
    # to the wired/wireless media gate (_media_ok) — tests that care about it
    # pass an explicit adapters= list (see test_media_* below).
    if adapters is None:
        adapters = [
            {"name": "eth0", "mac": "", "media_type": "wired", "is_default_route": False},
            {"name": "wlan0", "mac": "", "media_type": "wireless", "is_default_route": True},
        ]
    return {
        "hostname": host, "last_seen": time.time() if online else 0,
        "config": cfg, "overrides": overrides or {}, "adapters": adapters,
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
    """last_seen past the 30-min HARVEST_WINDOW_S (so the runner no longer counts
    as PRODUCING → a substitute is picked to hold N) but within the 60-min
    OFFLINE_TTL_S (so it's NOT dead → kept in the ledger). NOTE: the producing
    window is HARVEST_WINDOW_S (1800s), NOT the 5-min online window — a client
    seen in the last 30 min still counts (real agents flap offline for up to
    ~15 min, and the TTL is what defines 'dead'). 40 min sits in the 30–60 min
    no-longer-producing-but-not-dead band."""
    return time.time() - 2400


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
    # N=3. c0,c1,c2 assigned. c0 goes offline but is NOT dead (within OFFLINE_TTL):
    # the sim still runs on its VM, so it stays COUNTED as producing — NO substitute
    # (the fleet's ~20% availability buffer covers a brief agent-offline; real
    # agents flap offline for up to ~15 min). Only once it's gone past the TTL
    # (dead) is it released and REPLACED — the single point we churn.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    offline = first[0]
    # Offline but within the TTL → kept + counted, no substitute, ledger stays N.
    spoke.registry.clients[offline]["last_seen"] = _offline_recently()
    actions = _run(eng.reconcile())
    assert actions["assigned"] == 0 and actions["released"] == 0
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert offline in assigned and len(assigned) == 3
    # Gone past the TTL → dead → released + a substitute replaces it (still N=3).
    spoke.registry.clients[offline]["last_seen"] = time.time() - (OFFLINE_TTL_S + 60)
    actions = _run(eng.reconcile())
    assert actions["released"] == 1 and actions["assigned"] == 1
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert offline not in assigned and len(assigned) == 3


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
    # Physically pin every client to DFW (RF chamber) so a MIA quota can't harvest
    # them — the engine only pins via pxmx_site_map now, not a bare config.wsite.
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path, pin_sites=True)
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
    # TENANT-LEVEL environment (all SSIDs visible → clients are TENANT-POOL,
    # assignable to any site). Re-home only applies here — in RF isolation a
    # PXMX-pinned client can't leave its chamber. c0 is logically MIA (in-site),
    # c1-c4 logically DFW; a MIA quota re-homes the DFW ones to MIA (wsite=MIA),
    # and the ledger records their original site (DFW) so a later release reverts.
    clients = {"c0": _client("c0", "MIA")}
    for i in range(1, 5):
        clients[f"c{i}"] = _client(f"c{i}", "DFW")
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 3, "site": "MIA", "rehome": True,
                                  "enabled": True}], tmp_path)
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
    # Tenant-level (tenant-pool) env: after a rehome assign, removing the quota
    # releases the borrowed clients and reverts wsite to their original site (DFW).
    clients = {"c0": _client("c0", "MIA")}
    for i in range(1, 5):
        clients[f"c{i}"] = _client(f"c{i}", "DFW")
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 3, "site": "MIA", "rehome": True,
                                  "enabled": True}], tmp_path)
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


def test_reconcile_rehome_packed_multi_release_keeps_wsite(tmp_path):
    # re-home reference counting with TWO STACKABLE (multi) sims — an exclusive sim
    # would monopolize and B could never pack (confirmed: exclusive = one sim only).
    # Tenant-pool env, single logically-DFW client so the pack is deterministic:
    # quota A (ping_test) re-homes c0 DFW→MIA; quota B (download) PACKS onto c0.
    # Both record the NATURAL site (DFW). Releasing B must NOT revert wsite (A still
    # needs c0 at MIA); only releasing A (the last re-homer) reverts to DFW.
    clients = {"c0": _client("c0", "DFW")}
    quotas = [
        {"alert_id": "A", "sim_id": "ping_test", "count": 1, "site": "MIA",
         "rehome": True, "multi_capable": True, "enabled": True},
        {"alert_id": "B", "sim_id": "download", "count": 1, "site": "MIA",
         "rehome": True, "multi_capable": True, "enabled": True},
    ]
    spoke = _FakeSpoke(clients, quotas, tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    # A re-homed c0 to MIA; B packed onto c0. Both ledger entries recorded the
    # natural site DFW → both are recognized as re-homers.
    assert "c0" in eng._ledger["alert:A:MIA"]["clients"]
    assert "c0" in eng._ledger["alert:B:MIA"]["clients"]
    assert eng._ledger["alert:A:MIA"]["clients"]["c0"] == "DFW"
    assert eng._ledger["alert:B:MIA"]["clients"]["c0"] == "DFW"
    assert spoke.registry.clients["c0"]["overrides"]["wsite"] == "MIA"
    assert spoke.registry.clients["c0"]["overrides"]["ping_test"] == "on"
    assert spoke.registry.clients["c0"]["overrides"]["download"] == "on"
    # Drop ONLY B → A still re-homes c0, so wsite stays at MIA (not reverted to
    # DFW) and ping_test stays on; download turns off.
    spoke.local_store._q = [quotas[0]]
    _run(eng.reconcile())
    assert "alert:B:MIA" not in eng._ledger
    assert spoke.registry.clients["c0"]["overrides"]["wsite"] == "MIA"
    assert spoke.registry.clients["c0"]["overrides"]["ping_test"] == "on"
    assert spoke.registry.clients["c0"]["overrides"].get("download") == "off"
    # Drop A too → no re-homer remains → wsite reverts to DFW, ping_test off.
    spoke.local_store._q = []
    _run(eng.reconcile())
    assert eng._ledger == {}
    assert spoke.registry.clients["c0"]["overrides"].get("wsite") == "DFW"
    assert spoke.registry.clients["c0"]["overrides"].get("ping_test") == "off"


def test_reconcile_offline_runner_replaced_only_when_dead(tmp_path):
    # Quota N=2. c0,c1 assigned. c0 offline-but-alive (within OFFLINE_TTL) stays
    # counted → NO substitute, ledger stays 2 (ample buffer, no thrashing). When
    # c0 RETURNS before dying → nothing changed. Only once c0 is gone past the TTL
    # (dead) is it released and a substitute replaces it — ledger stays exactly N.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(4)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 2, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert len(assigned) == 2
    offline = assigned[0]
    # Offline but alive → kept, no substitute.
    spoke.registry.clients[offline]["last_seen"] = _offline_recently()
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == set(assigned)  # unchanged
    # Returns before dying → still the same 2, no churn.
    spoke.registry.clients[offline]["last_seen"] = time.time()
    actions = _run(eng.reconcile())
    assert actions["released"] == 0
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == set(assigned)
    # Now goes offline AND dies (past TTL) → released + replaced, ledger stays 2.
    spoke.registry.clients[offline]["last_seen"] = time.time() - (OFFLINE_TTL_S + 60)
    actions = _run(eng.reconcile())
    assert actions["released"] == 1 and actions["assigned"] == 1
    led = eng._ledger["alert:A:MIA"]["clients"]
    assert offline not in led and len(led) == 2


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


def test_reconcile_multi_does_not_pack_onto_exclusive_runner(tmp_path):
    # One EXCLUSIVE (dns_fail N=1) + one MULTI (ping_test N=1). An exclusive sim
    # MONOPOLIZES its client — a traffic sim never stacks onto it (confirmed model:
    # exclusive = ONE sim only, everything else may stack). With c0 taken by
    # dns_fail, ping_test takes the OTHER free client, not packs onto c0.
    clients = {"c0": _client("c0", "MIA"), "c1": _client("c1", "MIA")}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "A", "sim_id": "dns_fail", "count": 1, "site": "MIA", "enabled": True},
        {"alert_id": "B", "sim_id": "ping_test", "count": 1, "site": "MIA",
         "multi_capable": True, "enabled": True},
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    dns = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    ping = set(eng._ledger["alert:B:MIA"]["clients"].keys())
    assert dns == {"c0"}
    assert ping == {"c1"}                       # did NOT pack onto the exclusive c0
    assert dns.isdisjoint(ping)
    assert spoke.registry.clients["c0"]["overrides"]["dns_fail"] == "on"
    assert spoke.registry.clients["c0"]["overrides"].get("ping_test") in (None, "off")
    assert spoke.registry.clients["c1"]["overrides"]["ping_test"] == "on"


def test_reconcile_exclusive_preempts_drone_running_exclusive_bucket_default(tmp_path):
    # c0 is a DRONE: its bucket default runs assoc_fail (an exclusive sim), no
    # manual override. A dns_fail quota now PREEMPTS the drone — harvest (the
    # controlled few) wins over drones (the randomized many): it may harvest c0,
    # turning assoc_fail OFF (recorded in _displaced) so its own exclusive sim
    # runs cleanly. c0 sorts first, so it's the pick.
    clients = {"c0": _client("c0", "MIA", sim_flags={"assoc_fail": "on"}),
               "c1": _client("c1", "MIA")}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 1, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assigned = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert len(assigned) == 1
    h = next(iter(assigned))
    ov = spoke.registry.clients[h].get("overrides") or {}
    assert ov.get("dns_fail") == "on"
    if h == "c0":                                   # preempted the drone
        assert ov.get("assoc_fail") == "off", ov
        assert "assoc_fail" in eng._displaced.get("c0", set())


def test_reconcile_preempt_reverts_displaced_on_release(tmp_path):
    # dns_fail preempts drone c0 (bucket assoc_fail on) → assoc_fail turned OFF
    # (recorded in _displaced). Remove the quota → c0 released: dns_fail reverts
    # AND the displaced assoc_fail is restored by DELETION (back to its bucket
    # default), _displaced cleared. _ProvRegistry exercises the deletion revert.
    clients = {"c0": _client("c0", "MIA", sim_flags={"assoc_fail": "on"})}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail",
                                  "count": 1, "site": "MIA", "enabled": True}], tmp_path)
    spoke.registry = _ProvRegistry(clients)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    ov = spoke.registry.clients["c0"]["overrides"]
    assert ov.get("dns_fail") == "on" and ov.get("assoc_fail") == "off", ov
    assert "assoc_fail" in eng._displaced.get("c0", set())
    spoke.local_store._q = []                       # quota removed
    _run(eng.reconcile())
    ov = spoke.registry.clients["c0"]["overrides"]
    assert "dns_fail" not in ov, ov                 # released → reverted by deletion
    assert "assoc_fail" not in ov, ov               # displaced restored to bucket default
    assert "c0" not in eng._displaced


def test_preempt_displaced_off_is_not_a_human_pin(tmp_path):
    # A human "off" on a non-engine key IS a pin; an engine-displaced "off"
    # (recorded in _displaced) is NOT — otherwise a preempted drone would be
    # wrongly skipped by every other quota.
    clients = {"c0": _client("c0", "MIA",
                             overrides={"assoc_fail": "off", "dns_fail": "on"})}
    spoke = _FakeSpoke(clients, [], tmp_path)
    eng = SimQuotaEngine(spoke)
    c = spoke.registry.get("c0")
    c["engine_keys"] = ["assoc_fail", "dns_fail"]   # engine set both
    # No displaced record → the "off" reads as a human flip → pin.
    assert eng._has_manual_sim_pin("c0", c) is True
    # Record assoc_fail as displaced → the "off" is the engine's → NOT a pin
    # (dns_fail is an engine "on", also not a pin).
    eng._displaced["c0"] = {"assoc_fail"}
    assert eng._has_manual_sim_pin("c0", c) is False

# ── real-registry regression: wsite override must survive set_overrides prune ─
# The fake registry used above doesn't prune, so it can't catch the bug where
# the engine's re-home wsite=MIA is deleted by ClientRegistry.set_overrides'
# prune loop (which used to treat wsite like an on/off flag). This wires the
# engine to a REAL ClientRegistry with a bucket_resolver whose bucket default
# wsite is "DFW" — the exact shape of the user's MIA quota borrowing DFW
# runners — and asserts wsite=MIA + dns_fail=on both survive into the registry
# (and therefore reach the client via /api/config's bake-into-[sX]).
def test_engine_rehome_wsite_survives_real_registry_prune(tmp_path):
    from client_registry import ClientRegistry

    class _RealRegSpoke:
        def __init__(self, reg, quotas, pxmx_site_map, name_to_host, tmp_path):
            self.registry = reg
            self.local_store = _FakeLocalStore(quotas, pxmx_site_map)
            self.settings = _FakeSettings()
            self.data_dir = str(tmp_path)
            self.deploy = _FakeDeploy(name_to_host)

    # Bucket default wsite=DFW, dns_fail off — the DFW runner's natural bucket.
    def _bucket(hn):
        return {"wsite": "DFW", "dns_fail": "off"}

    reg = ClientRegistry(tmp_path / "data", bucket_resolver=_bucket)
    # Tenant-level env: two TENANT-POOL clients (no pxmx_site_map) whose NATURAL
    # (bucket) site is DFW while the quota wants MIA → the engine re-homes them
    # (set wsite=MIA). Seeding via the status-beacon path (no register() method).
    _run(reg.apply_status("c0", {"config": {"wsite": "DFW"}}))
    _run(reg.apply_status("c1", {"config": {"wsite": "DFW"}}))

    quotas = [{"alert_id": "A", "alert_type": "alert", "sim_id": "dns_fail",
               "count": 2, "site": "MIA", "rehome": True,
               "multi_capable": False, "enabled": True}]
    spoke = _RealRegSpoke(reg, quotas, {}, {}, tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())

    ledger = eng._ledger["alert:A:MIA"]
    assert len(ledger["clients"]) == 2
    for h in ledger["clients"]:
        ov = reg.get(h)["overrides"]
        # wsite=MIA MUST survive the prune (the bug deleted it) AND dns_fail=on
        # is a real deviation from the bucket (off) so it stays too.
        assert ov.get("wsite") == "MIA", f"{h}: wsite pruned! overrides={ov}"
        assert ov.get("dns_fail") == "on", f"{h}: dns_fail missing! overrides={ov}"
        # from_site recorded as the natural DFW so a later release reverts.
        assert ledger["clients"][h] == "DFW"


# ── presence quotas (Clients Associated — sim_id empty) ────────────────────
def test_reconcile_presence_homes_n_clients_no_sim_flag(tmp_path):
    # A presence quota (sim_id="") on MIA homes N online free runners and sets
    # NO sim flag — only wsite (and only when re-homing). The clients remain
    # free runners other sims may stack onto.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "", "alert_type": "alert",
                                  "sim_id": "", "count": 3, "site": "MIA",
                                  "multi_capable": True, "rehome": False,
                                  "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    ledger = eng._ledger["presence::MIA"]
    assert len(ledger["clients"]) == 3
    for h in ledger["clients"]:
        ov = spoke.registry.clients[h]["overrides"]
        # No sim flag set — presence homes only. In-site clients (already MIA)
        # aren't re-homed, so no wsite override either.
        assert "dns_fail" not in ov and "ping_test" not in ov
        assert "wsite" not in ov  # already MIA → no re-home


def test_reconcile_presence_rehomes_cross_site_sets_wsite(tmp_path):
    # Presence MIA with rehome borrows DFW runners and re-homes them (wsite=MIA),
    # still setting NO sim flag. Ledger records from_site=DFW so release reverts.
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(3)}
    spoke = _FakeSpoke(clients, [{"alert_id": "", "sim_id": "", "count": 2,
                                  "site": "MIA", "multi_capable": True,
                                  "rehome": True, "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    ledger = eng._ledger["presence::MIA"]
    assert len(ledger["clients"]) == 2
    for h in ledger["clients"]:
        ov = spoke.registry.clients[h]["overrides"]
        assert ov.get("wsite") == "MIA"          # re-homed, no sim flag
        assert not any(k for k in ov if k != "wsite")
        assert ledger["clients"][h] == "DFW"      # from_site recorded


def test_reconcile_presence_packs_with_sim_quota_on_same_client(tmp_path):
    # A presence MIA quota + a dns_fail MIA quota over 3 MIA clients: presence
    # homes 3 (no sim flag), dns_fail stacks dns_fail=on onto 2 of them (the
    # user's "they can run other simulations that are stackable"). The presence
    # clients remain available for the sim quota to pack onto.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "", "sim_id": "", "count": 3, "site": "MIA",
         "multi_capable": True, "rehome": False, "enabled": True},
        {"alert_id": "A", "sim_id": "dns_fail", "count": 2, "site": "MIA",
         "enabled": True},
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    pres = eng._ledger["presence::MIA"]["clients"]
    sim = eng._ledger["alert:A:MIA"]["clients"]
    assert len(pres) == 3
    assert len(sim) == 2
    # The 2 dns_fail clients are a subset of the 3 presence-homed clients —
    # presence doesn't consume the client for sim purposes.
    assert set(sim).issubset(set(pres))
    for h in pres:
        # presence clients have no sim flag from the presence quota; the two
        # that dns_fail packed onto DO have dns_fail=on (from the sim quota).
        ov = spoke.registry.clients[h]["overrides"]
        if h in sim:
            assert ov.get("dns_fail") == "on"
        else:
            assert "dns_fail" not in ov
    # No presence client got a sim flag from the presence quota itself.
    assert all("ping_test" not in (spoke.registry.clients[h]["overrides"] or {})
               for h in pres)


def test_reconcile_presence_releases_revert_wsite_no_sim_flag(tmp_path):
    # After a presence re-home, removing the quota releases the borrowed
    # clients: wsite reverts to DFW, NO sim flag is toggled (there was none).
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(2)}
    spoke = _FakeSpoke(clients, [{"alert_id": "", "sim_id": "", "count": 2,
                                  "site": "MIA", "multi_capable": True,
                                  "rehome": True, "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert spoke.registry.clients["c0"]["overrides"]["wsite"] == "MIA"
    spoke.local_store._q = []                    # quota removed
    _run(eng.reconcile())
    assert eng._ledger == {}
    for h in clients:
        ov = spoke.registry.clients[h]["overrides"]
        assert ov.get("wsite") == "DFW"           # reverted
        # No sim flag was ever set, so none toggled off on release.
        assert not any(k != "wsite" for k in ov)


def test_reconcile_presence_substitute_on_offline(tmp_path):
    # Presence keeps N homed: an assigned client going dead (>OFFLINE_TTL) is
    # released and a substitute fills so the homed count stays at N.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "", "sim_id": "", "count": 3,
                                  "site": "MIA", "multi_capable": True,
                                  "rehome": False, "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = set(eng._ledger["presence::MIA"]["clients"])
    dead = next(iter(first))
    spoke.registry.clients[dead]["last_seen"] = 0  # past OFFLINE_TTL → dead
    _run(eng.reconcile())
    after = set(eng._ledger["presence::MIA"]["clients"])
    assert dead not in after                       # dead client released
    assert len(after) == 3                         # substitute filled → still N


# ── Source-aware claim_key (Phase 4: Central/Mist separate clients) ─────────

def _q(alert_id, sim_id, count, site="MIA"):
    return {"alert_id": alert_id, "alert_type": "alert", "sim_id": sim_id,
            "count": count, "site": site, "enabled": True}


def test_cross_source_same_site_keeps_separate_clients(tmp_path):
    # A Central dns_fail@MIA and a Mist dns_fail@MIA each want 3 clients. They
    # must NOT share — each row runs its OWN clients (the prefix is the only
    # seam). 6 clients in the pool → 3 for Central, 3 for Mist, no overlap.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(6)}
    spoke = _FakeSpoke(clients, [
        _q("Central:dns_fail", "dns_fail", 3),
        _q("Mist:dns_fail", "dns_fail", 3),
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    cen = set(eng._ledger["alert:Central:dns_fail:MIA"]["clients"])
    mist = set(eng._ledger["alert:Mist:dns_fail:MIA"]["clients"])
    assert len(cen) == 3 and len(mist) == 3
    assert not (cen & mist), "cross-source rows must keep separate clients"
    # Distinct ledger keys → independent counts/learning.
    assert "alert:Central:dns_fail:MIA" in eng._ledger
    assert "alert:Mist:dns_fail:MIA" in eng._ledger


def test_same_source_same_site_still_stacks(tmp_path):
    # Same-source stacking is unchanged: a Central PRESENCE quota (Clients
    # Associated) homes 3 clients at MIA, and a Central dns_fail at MIA stacks
    # onto them (same claim "central:MIA" → reuses the homed clients). Source-
    # scoping must not regress this. Total distinct clients = 3, shared.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(6)}
    spoke = _FakeSpoke(clients, [
        {"alert_id": "", "sim_id": "", "count": 3, "site": "MIA",
         "multi_capable": True, "rehome": False, "enabled": True},
        _q("Central:dns_fail", "dns_fail", 3),
    ], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    pres = set(eng._ledger["presence::MIA"]["clients"])
    dns = set(eng._ledger["alert:Central:dns_fail:MIA"]["clients"])
    assert len(pres) == 3 and len(dns) == 3
    assert pres == dns, "same-source presence+sim must stack onto the same clients"


def test_reconcile_weighted_spreads_tenant_pool_across_sites(tmp_path):
    # 4 tenant-pool (unmapped → assignable-anywhere) clients, NO harvest quota →
    # all spare. Two weighted SSID cells at sites A and B (even weights). The
    # spare must SPREAD across both sites, not pile onto whichever sorts first
    # (the cross-site randomization fix). Physically-bound clients are untouched.
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(4)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A-cell", "site": "A", "ssid": "ssidA", "ssidpw": "pwA", "enabled": True},
        {"name": "B-cell", "site": "B", "ssid": "ssidB", "ssidpw": "pwB", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A-cell", "site": "A", "weight": 1},
        {"ssid": "B-cell", "site": "B", "weight": 1},
    ]
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = sorted(spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients)
    assert wsites.count("A") == 2, wsites   # even split, not 4/0
    assert wsites.count("B") == 2, wsites


def test_reconcile_weighted_site_weights_bias_the_spread(tmp_path):
    # ambient_site_weights {A:3, B:1} → 6 spare split 4-to-A / 2-to-B (weighted).
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(6)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A-cell", "site": "A", "ssid": "ssidA", "ssidpw": "pwA", "enabled": True},
        {"name": "B-cell", "site": "B", "ssid": "ssidB", "ssidpw": "pwB", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A-cell", "site": "A", "weight": 1},
        {"ssid": "B-cell", "site": "B", "weight": 1},
    ]
    spoke.local_store._site_w = {"A": 3, "B": 1}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = [spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients]
    assert wsites.count("A") == 4, wsites
    assert wsites.count("B") == 2, wsites


def test_reconcile_weighted_cell_weights_default_the_cross_site_split(tmp_path):
    # No ambient_site_weights set → the cross-site split defaults to the SUM of
    # each site's weighted cell rules, so a cell weighted 3 at A draws 3× the
    # tenant-pool clients of a cell weighted 1 at B (the operator's cell-weight
    # intent). 8 spare, A-cell weight 3 / B-cell weight 1 → 6 to A / 2 to B.
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(8)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A-cell", "site": "A", "ssid": "ssidA", "ssidpw": "pwA", "enabled": True},
        {"name": "B-cell", "site": "B", "ssid": "ssidB", "ssidpw": "pwB", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A-cell", "site": "A", "weight": 3},
        {"ssid": "B-cell", "site": "B", "weight": 1},
    ]
    # _site_w left empty → default to cell-weight sums.
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = [spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients]
    assert wsites.count("A") == 6, wsites
    assert wsites.count("B") == 2, wsites


def test_reconcile_weighted_all_only_site_keeps_even_share(tmp_path):
    # A site whose only cell is an `all` (weight 0) cell must still receive an
    # even share for its `all` cell to soak — the cell-sum default floors at 1,
    # it must NOT drop to 0 and starve the `all` cell. 4 spare across two sites,
    # A has a weighted cell (weight 1), B has only an `all` cell → 2 to A / 2 to
    # B (B's `all` cell soaks its 2).
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(4)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A-cell", "site": "A", "ssid": "ssidA", "ssidpw": "pwA", "enabled": True},
        {"name": "B-all", "site": "B", "ssid": "ssidB", "ssidpw": "pwB", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A-cell", "site": "A", "weight": 1},
        {"ssid": "B-all", "site": "B", "weight": 0, "all": True},
    ]
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = [spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients]
    assert wsites.count("A") == 2, wsites
    assert wsites.count("B") == 2, wsites


def test_reconcile_weighted_multi_cell_site_gets_larger_share(tmp_path):
    # The cells-per-site dilution fix: a site with MORE cells must draw a
    # proportionally larger share of the tenant-pool so its per-cell count isn't
    # starved. 12 spare, site A has two weight-1 cells (sum 2), site B has one
    # weight-1 cell (sum 1), no ambient_site_weights → cross-site 2:1 → A gets
    # 8 / B gets 4, then A splits 4/4. Per-cell: A1=4, A2=4, B1=4 (all equal),
    # instead of the old 3/3/6 where B1 got double.
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(12)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A1", "site": "A", "ssid": "ssidA1", "ssidpw": "pw", "enabled": True},
        {"name": "A2", "site": "A", "ssid": "ssidA2", "ssidpw": "pw", "enabled": True},
        {"name": "B1", "site": "B", "ssid": "ssidB1", "ssidpw": "pw", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A1", "site": "A", "weight": 1},
        {"ssid": "A2", "site": "A", "weight": 1},
        {"ssid": "B1", "site": "B", "weight": 1},
    ]
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = [spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients]
    assert wsites.count("A") == 8, wsites          # 2-cell site gets 2× the 1-cell site
    assert wsites.count("B") == 4, wsites
    ssids = [spoke.registry.clients[h].get("overrides", {}).get("ssid") for h in clients]
    assert ssids.count("ssidA1") == 4, ssids
    assert ssids.count("ssidA2") == 4, ssids
    assert ssids.count("ssidB1") == 4, ssids


def test_reconcile_weighted_site_weight_multiplies_cell_sum(tmp_path):
    # The per-site weight is a MULTIPLIER on the cell-sum, not an absolute share.
    # site_w {A:2}, A-cell weight 2 (sum 2), B-cell weight 1 (sum 1, no site_w →
    # multiplier 1) → cross-site weights A=2×2=4 vs B=1×1=1 → 10 spare splits
    # 8 to A / 2 to B.
    clients = {f"c{i}": _client(f"c{i}", "") for i in range(10)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._matrix = [
        {"name": "A-cell", "site": "A", "ssid": "ssidA", "ssidpw": "pw", "enabled": True},
        {"name": "B-cell", "site": "B", "ssid": "ssidB", "ssidpw": "pw", "enabled": True},
    ]
    spoke.local_store._weights = [
        {"ssid": "A-cell", "site": "A", "weight": 2},
        {"ssid": "B-cell", "site": "B", "weight": 1},
    ]
    spoke.local_store._site_w = {"A": 2}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    wsites = [spoke.registry.clients[h].get("overrides", {}).get("wsite") for h in clients]
    assert wsites.count("A") == 8, wsites
    assert wsites.count("B") == 2, wsites


# ── weighted multi-sim stacking (_reconcile_stacked) ─────────────────────────
def _stack_on(spoke, host, sim):
    return spoke.registry.clients[host].get("overrides", {}).get(sim) == "on"


def test_stacking_off_when_no_randomizable(tmp_path):
    """Opt-in: with randomizable_sims empty (default) the engine assigns no
    ambient sims — preserves the pre-stacking behavior."""
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(5)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    _run(SimQuotaEngine(spoke).reconcile())
    for h in clients:
        assert not spoke.registry.clients[h].get("overrides")


def test_stacking_fills_spare_clients_weighted_and_capped(tmp_path):
    """randomizable {ping_test:w1, download:w3}, cap 2 → download reaches more
    clients than ping_test (breadth ∝ weight), no client exceeds the cap, and
    some clients carry BOTH (real stacking)."""
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(30)}
    spoke = _FakeSpoke(clients, [], tmp_path)
    spoke.local_store._randomizable = ["ping_test", "download"]
    spoke.local_store._sim_weights = {"ping_test": 1, "download": 3}
    spoke.local_store._stack_cap = 2
    random.seed(1234)
    _run(SimQuotaEngine(spoke).reconcile())
    sims = ("ping_test", "download")
    pt = sum(1 for h in clients if _stack_on(spoke, h, "ping_test"))
    dl = sum(1 for h in clients if _stack_on(spoke, h, "download"))
    assert dl > pt, (pt, dl)                       # weight 3 → broader than weight 1
    for h in clients:                              # cap respected
        assert sum(1 for s in sims if _stack_on(spoke, h, s)) <= 2
    stacked = sum(1 for h in clients
                  if sum(1 for s in sims if _stack_on(spoke, h, s)) >= 2)
    assert stacked > 0                             # some clients actually stacked


def test_stacking_skips_exclusive_client_and_is_harvestable(tmp_path):
    """A client running an EXCLUSIVE sim is never stacked; and a harvest quota
    can still take a stacked client (harvest wins → stack cleared)."""
    import random as _r
    clients = {f"c{i}": _client(f"c{i}", "DFW") for i in range(8)}
    # c0 runs an exclusive sim (ssidpw_fail) as a bucket default.
    clients["c0"]["config"]["ssidpw_fail"] = "on"
    # An exclusive dns_fail quota for 2 clients coexists with stacking.
    spoke = _FakeSpoke(
        clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 2,
                   "site": "", "enabled": True}], tmp_path)
    spoke.local_store._randomizable = ["ping_test", "download"]
    spoke.local_store._stack_cap = 2
    _r.seed(7)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    # c0 (exclusive bucket default) is never given a stacked sim.
    assert not _stack_on(spoke, "c0", "ping_test")
    assert not _stack_on(spoke, "c0", "download")
    # The dns_fail quota harvested 2 clients; a harvested client carries dns_fail
    # and is NOT left holding a stacked sim (harvest cleared the stack).
    harvested = [h for h in clients
                 if spoke.registry.clients[h]["overrides"].get("dns_fail") == "on"]
    assert len(harvested) == 2
    for h in harvested:
        assert not _stack_on(spoke, h, "ping_test")
        assert not _stack_on(spoke, h, "download")


# ── harvest cooldown (anti-flap) ─────────────────────────────────────────────
def test_harvest_cooldown_blocks_reharvest_until_expired(tmp_path):
    """A client that just finished a harvest can't be re-harvested until its
    cooldown expires — a fresh quota picks a DIFFERENT client meanwhile; a client
    CURRENTLY serving is exempt. cooldown 0 (default) would re-pick immediately."""
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(2)}
    q = {"alert_id": "A", "sim_id": "dns_fail", "count": 1, "site": "MIA", "enabled": True}
    spoke = _FakeSpoke(clients, [dict(q)], tmp_path)
    spoke.registry = _ProvRegistry(clients)           # provenance: released sim reverts cleanly
    spoke.local_store._harvest_cooldown_s = 3600      # 1h
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = list(eng._ledger["alert:A:MIA"]["clients"].keys())[0]
    # Release everything (remove the quota), then re-add it in the same window.
    spoke.local_store._q = []
    _run(eng.reconcile())
    assert eng._ledger == {}
    spoke.local_store._q = [dict(q)]
    _run(eng.reconcile())
    second = list(eng._ledger["alert:A:MIA"]["clients"].keys())[0]
    assert second != first                            # cooling client NOT re-picked
    # Expire `first`'s cooldown and grow the quota to 2 → it's allowed back
    # (the now-serving `second` is exempt and kept).
    eng._last_harvest[first] = time.time() - 4000     # past the 3600s window
    spoke.local_store._q = [{"alert_id": "A", "sim_id": "dns_fail", "count": 2,
                             "site": "MIA", "enabled": True}]
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0", "c1"}


# ── per-quota client-tier policy (T1 dedicated PCI / T2 dongle / Best) ────────
def _tiered(host, site, tier):
    c = _client(host, site)
    c["tier"] = tier
    return c


def test_quota_tier_t1_only_underfills(tmp_path):
    # 2 T1 + 2 T2. A tier='t1' quota (count 3) admits ONLY T1 → underfills to 2
    # rather than degrade a high-reliability sim onto a dongle.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t1"),
               "c2": _tiered("c2", "MIA", "t2"), "c3": _tiered("c3", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3,
                                  "site": "MIA", "tier": "t1", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0", "c1"}


def test_quota_tier_t2_only(tmp_path):
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t1"),
               "c2": _tiered("c2", "MIA", "t2"), "c3": _tiered("c3", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3,
                                  "site": "MIA", "tier": "t2", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c2", "c3"}


def test_quota_tier_best_prefers_t1_then_falls_back(tmp_path):
    # 'best' uses all T1 first, then falls back to T2.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t1"),
               "c2": _tiered("c2", "MIA", "t2"), "c3": _tiered("c3", "MIA", "t2")}
    # count 2 → both T1.
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 2,
                                  "site": "MIA", "tier": "best", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0", "c1"}
    # grow to 3 → T1 pair + one T2 (fallback).
    spoke.local_store._q = [{"alert_id": "A", "sim_id": "dns_fail", "count": 3,
                             "site": "MIA", "tier": "best", "enabled": True}]
    _run(eng.reconcile())
    got = set(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert {"c0", "c1"} <= got and len(got) == 3 and (got & {"c2", "c3"})


# ── tenant tier priority + tier upgrade ───────────────────────────────────────
# tier_priority sets the DEFAULT tier for quotas that don't pin one, and reserves
# the other tier out of the ambient spread. The upgrade pass then swaps a held T2
# for a T1 the moment one frees up, so a specific simulation doesn't stay stuck on
# a dongle just because it filled while every radio was busy.
def _tq(count, tier=None):
    q = {"alert_id": "A", "sim_id": "dns_fail", "count": count,
         "site": "MIA", "enabled": True}
    if tier:
        q["tier"] = tier
    return q


def test_tier_priority_defaults_to_t1_first(tmp_path):
    # Unconfigured tenant == historical 'best' behaviour: T1 first.
    clients = {"c0": _tiered("c0", "MIA", "t2"), "c1": _tiered("c1", "MIA", "t1")}
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}


def test_tier_priority_t2_first_inverts_the_preference(tmp_path):
    clients = {"c0": _tiered("c0", "MIA", "t2"), "c1": _tiered("c1", "MIA", "t1")}
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    spoke.local_store._csc = {"tier_priority": "t2_first"}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}


def test_tier_priority_t1_only_gates_an_unpinned_quota(tmp_path):
    # t1_only makes an unpinned ('best') quota behave like tier='t1': underfill
    # rather than take a dongle.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t2"),
               "c2": _tiered("c2", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [_tq(3)], tmp_path)
    spoke.local_store._csc = {"tier_priority": "t1_only"}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}


def test_quota_pinned_tier_beats_the_tenant_default(tmp_path):
    # An explicit per-quota tier is an operator decision — the tenant-wide
    # default must not override it.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [_tq(1, tier="t2")], tmp_path)
    spoke.local_store._csc = {"tier_priority": "t1_only"}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}


def test_unknown_tier_priority_falls_back_to_default(tmp_path):
    clients = {"c0": _tiered("c0", "MIA", "t2"), "c1": _tiered("c1", "MIA", "t1")}
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    spoke.local_store._csc = {"tier_priority": "nonsense"}
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}


def test_tier_upgrade_swaps_a_held_t2_for_a_freed_t1(tmp_path):
    # THE regression this pass exists for: the quota fills with a T2 because no
    # T1 is online, then a T1 appears. Before the upgrade pass the quota was full
    # (need == 0), never re-picked, and held the dongle indefinitely.
    clients = {"c0": _tiered("c0", "MIA", "t2"),
               "c1": _tiered("c1", "MIA", "t1")}
    clients["c1"]["last_seen"] = 0     # T1 present but OFFLINE
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}, "fills with the T2"
    # T1 comes online.
    spoke.registry.clients["c1"]["last_seen"] = time.time()
    actions = _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}, "upgraded onto the T1"
    assert actions.get("upgraded") == 1


def test_tier_upgrade_is_capped_per_sweep(tmp_path):
    # 3 T2s holding, 3 T1s free → at most TIER_UPGRADE_PER_SWEEP swap per sweep,
    # so a large quota migrates gradually instead of tearing down all at once.
    clients = {f"t2_{i}": _tiered(f"t2_{i}", "MIA", "t2") for i in range(3)}
    spoke = _FakeSpoke(clients, [_tq(3)], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 3
    for i in range(3):
        spoke.registry.clients[f"t1_{i}"] = _tiered(f"t1_{i}", "MIA", "t1")
    actions = _run(eng.reconcile())
    assert actions.get("upgraded") == TIER_UPGRADE_PER_SWEEP
    held = eng._ledger["alert:A:MIA"]["clients"]
    assert len(held) == 3, "ledger never exceeds N during a swap"
    assert sum(1 for h in held if h.startswith("t1_")) == TIER_UPGRADE_PER_SWEEP
    # Subsequent sweeps finish the migration, then stop (no lateral churn).
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"t1_0", "t1_1", "t1_2"}
    assert _run(eng.reconcile()).get("upgraded", 0) == 0, "converged — no churn"


def test_tier_upgrade_skipped_for_a_hard_pinned_quota(tmp_path):
    # tier='t2' admits only T2 — every holder ranks the same, so a free T1 must
    # NOT trigger a swap.
    clients = {"c0": _tiered("c0", "MIA", "t2"), "c1": _tiered("c1", "MIA", "t1")}
    spoke = _FakeSpoke(clients, [_tq(1, tier="t2")], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    actions = _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}
    assert actions.get("upgraded", 0) == 0


def test_tier_upgrade_harvests_a_t1_busy_with_background_work(tmp_path):
    # The operating model: T1s are the reliable clients and should be running the
    # issue-generating quota sims; T2s are the less-reliable background noise. A
    # T1 that no quota needs still does background work (nothing is idle) — but
    # the moment a quota needs it, it must be harvested OUT of that background
    # work and INTO the quota, displacing a T2 to make room.
    #
    # Background work is representable as a bucket-default sim: stacked/ambient
    # sims live OUTSIDE the ledger, so _engine_sims_for is empty for a background
    # client and _pool_eligible treats it as a preemptible drone.
    clients = {"c0": _tiered("c0", "MIA", "t2"),
               "c1": _tiered("c1", "MIA", "t1")}
    clients["c1"]["config"]["ping_test"] = "on"       # T1 busy with background work
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    spoke.registry = _ProvRegistry(clients)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}, \
        "a T1 doing background work is harvestable, not blocked"


def test_t1_in_background_is_harvested_over_a_held_t2(tmp_path):
    # Same rule from the UPGRADE side: the quota is already full of T2 (the T1
    # was absent when it filled). The T1 returns and goes to background work.
    # It must still be pulled into the quota, disrupting the T2.
    clients = {"c0": _tiered("c0", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    spoke.registry = _ProvRegistry(clients)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}, "filled with the T2"
    # T1 appears, already loaded with background work.
    t1 = _tiered("c1", "MIA", "t1")
    t1["config"]["ping_test"] = "on"
    spoke.registry.clients["c1"] = t1
    actions = _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c1"}, \
        "T1 harvested out of background work, T2 disrupted"
    assert actions.get("upgraded") == 1
    # And the displaced T2 is free to go back to background noise.
    assert "c0" not in eng._ledger["alert:A:MIA"]["clients"]


def eng_reset(spoke):
    return SimQuotaEngine(spoke)


def test_ambient_spread_does_not_reserve_t1_out_of_background_work(tmp_path):
    # "All clients should be doing something": the weighted ambient spread must
    # place EVERY spare client, T1 included. A T1 held back would sit with no
    # SSID/site placement, which is worse than loading it — harvest preempts
    # ambient anyway, so nothing is lost by keeping it busy.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t2")}
    spoke = _FakeSpoke(clients, [], tmp_path)          # no quotas → all spare
    spoke.local_store._matrix = [{"name": "MIA-PSK", "site": "MIA",
                                  "ssid": "PSK", "ssidpw": "pw"}]
    spoke.local_store._weights = [{"site": "MIA", "ssid": "MIA-PSK",
                                   "weight": 1, "all": True}]
    for pri in ("t1_first", "t2_first"):
        spoke.local_store._csc = {"tier_priority": pri}
        _run(eng_reset(spoke).reconcile())
        placed = {h for h, c in spoke.registry.clients.items()
                  if (c.get("overrides") or {}).get("ssid")}
        assert placed == {"c0", "c1"}, f"{pri}: every spare client placed, got {placed}"


# ── T1 exemption from the harvest cooldown ────────────────────────────────────
# The cooldown is an anti-flap rule for the DRONE pool: rest a released client so
# Central data stays realistic. T1s are exempt — a T1 released by a disappearing
# quota and needed again straight away must be re-harvestable, or it rests for
# hours while a less-reliable T2 takes the slot, inverting the tier model.
# NOTE: a count-0 quota does NOT release its clients (the ledger is left alone);
# the real release path is the quota leaving the effective set entirely.
def _cooldown_spoke(clients, tmp_path):
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    spoke.registry = _ProvRegistry(clients)      # release reverts by DELETION, not "off"
    spoke.local_store._harvest_cooldown_s = 3600  # 1h — the fake defaults it OFF
    return spoke


def test_t1_is_exempt_from_harvest_cooldown(tmp_path):
    clients = {"c0": _tiered("c0", "MIA", "t1")}
    spoke = _cooldown_spoke(clients, tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}
    spoke.local_store._q = []                     # quota removed → released
    _run(eng.reconcile())
    assert "alert:A:MIA" not in eng._ledger
    assert eng._last_harvest.get("c0"), \
        "still STAMPED — the exemption is policy, not a missing record"
    spoke.local_store._q = [_tq(1)]               # and comes straight back
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}, \
        "T1 re-harvested inside its cooldown window"


def test_t2_still_rests_in_harvest_cooldown(tmp_path):
    # Same sequence, T2: the exemption must be tier-scoped, so the drone rests.
    clients = {"c0": _tiered("c0", "MIA", "t2")}
    spoke = _cooldown_spoke(clients, tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}
    spoke.local_store._q = []
    _run(eng.reconcile())
    spoke.local_store._q = [_tq(1)]
    _run(eng.reconcile())
    assert not eng._ledger["alert:A:MIA"]["clients"], "a released T2 rests out its cooldown"


def test_t1_preferred_over_a_resting_t2(tmp_path):
    # The point of the exemption, end to end: both were released, the quota comes
    # back needing one client. The T1 is available immediately; the T2 is resting.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t2")}
    spoke = _cooldown_spoke(clients, tmp_path)
    spoke.local_store._q = [_tq(2)]
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0", "c1"}
    spoke.local_store._q = []
    _run(eng.reconcile())
    spoke.local_store._q = [_tq(2)]               # wants 2, only the T1 is free
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == {"c0"}


def test_t1_cooldown_exemption_surfaces_in_diag(tmp_path):
    # _diag_reason mirrors _pool_eligible; if it lagged behind, Engine State would
    # report harvest_cooldown for a T1 the engine is happily harvesting.
    clients = {"c0": _tiered("c0", "MIA", "t1"), "c1": _tiered("c1", "MIA", "t2")}
    spoke = _cooldown_spoke(clients, tmp_path)
    spoke.local_store._q = [_tq(2)]
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    spoke.local_store._q = []
    _run(eng.reconcile())
    spoke.local_store._q = [_tq(2)]
    _run(eng.reconcile())
    blocked = eng._quota_diag["alert:A:MIA"]["blocked"]
    assert blocked.get("harvest_cooldown", 0) == 1, blocked   # the T2, never the T1


# ── gateway-confirmed-down: not-working clients don't inflate the count ─────
# A client can heartbeat perfectly (fresh last_seen) while being functionally
# dead — e.g. a detached T2 dongle means no gateway_reachable even though the
# WS/API connection never drops. client_registry.apply_status latches
# gateway_confirmed_down only once gateway_reachable has held False for a full
# 60 continuous minutes (never a single bad reading), and clears it only after
# an equally continuous 60-min recovery. These tests drive that latch directly
# rather than the 60-min real-time wait.

def test_reconcile_releases_gateway_confirmed_down_immediately(tmp_path):
    # Fresh last_seen (would pass the old harvestable check outright) but a
    # confirmed gateway drop must be released NOW, not ridden on the
    # last_seen-based OFFLINE_TTL_S grace meant for WS blips.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    down = first[0]
    spoke.registry.clients[down]["gateway_confirmed_down"] = True
    actions = _run(eng.reconcile())
    assert actions["released"] == 1 and actions["assigned"] == 1
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    assert down not in assigned and len(assigned) == 3


def test_reconcile_gateway_flicker_not_latched_stays_counted(tmp_path):
    # gateway_state=False but gateway_confirmed_down never latched (< 60 min
    # streak) — this is the normal per-loop-iteration flicker; must NOT be
    # treated as a drop.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    clients["c0"]["gateway_state"] = False
    clients["c0"]["gateway_state_since"] = time.time() - 30
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assert actions["released"] == 0
    assert set(eng._ledger["alert:A:MIA"]["clients"]) == set(clients)


def test_reconcile_gateway_down_exempt_while_running_connectivity_breaking_sim(tmp_path):
    # A client running e.g. ssidpw_fail is EXPECTED to have no gateway — a
    # confirmed drop while running one of GATEWAY_EXEMPT_SIMS must not release it.
    exempt_sim = sorted(GATEWAY_EXEMPT_SIMS)[0]
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    quotas = [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}]
    spoke = _FakeSpoke(clients, quotas, tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    first = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    down = first[0]
    spoke.registry.clients[down]["gateway_confirmed_down"] = True
    spoke.registry.clients[down]["active_simulations"] = {exempt_sim: {}}
    actions = _run(eng.reconcile())
    assert actions["released"] == 0
    assert down in eng._ledger["alert:A:MIA"]["clients"]


def test_pool_eligible_excludes_gateway_confirmed_down_client(tmp_path):
    # A free (unassigned) client that's gateway-confirmed-down must never be
    # freshly picked into a quota, same as it must not be kept once assigned.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    clients["c0"]["gateway_confirmed_down"] = True
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())
    assigned = set(eng._ledger["alert:A:MIA"]["clients"])
    assert "c0" not in assigned
    assert actions["assigned"] == 2   # only c1, c2 — underfilled, no substitute for c0


def test_gateway_down_confirmed_readmits_after_latch_clears(tmp_path):
    # Once client_registry clears the latch (60 continuous min back up), the
    # client is eligible again — this drives the latch directly to prove the
    # engine reads gateway_confirmed_down fresh each sweep, not a cached verdict.
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    clients["c0"]["gateway_confirmed_down"] = True
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "dns_fail", "count": 3, "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert "c0" not in eng._ledger["alert:A:MIA"]["clients"]
    spoke.registry.clients["c0"]["gateway_confirmed_down"] = False
    actions = _run(eng.reconcile())
    assert actions["assigned"] == 1
    assert "c0" in eng._ledger["alert:A:MIA"]["clients"]
# ── media gate (_media_ok): wired/wireless-tagged sims vs reported adapters ──
# SIM_META tags assoc_fail/ssidpw_fail/auth_fail/mac_auth_fail "wireless" and
# port_flap "wired"; everything else is "any". A client only qualifies for a
# media-tagged sim if its self-reported adapters (client["adapters"]) include
# that media — fail-closed when the list is empty/missing (no heartbeat yet).

_WIRED_ONLY = [{"name": "eth0", "media_type": "wired", "is_default_route": True}]
_WIRELESS_ONLY = [{"name": "wlan0", "media_type": "wireless", "is_default_route": True}]


def test_media_gate_keeps_wireless_sim_off_a_wired_only_client(tmp_path):
    clients = {"c0": _client("c0", "MIA", adapters=_WIRED_ONLY),
               "c1": _client("c1", "MIA", adapters=_WIRELESS_ONLY)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "assoc_fail", "count": 2,
                                  "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c1"}, \
        "the wired-only client must never be picked for a wireless sim; underfill instead"


def test_media_gate_keeps_wired_sim_off_a_wireless_only_client(tmp_path):
    clients = {"c0": _client("c0", "MIA", adapters=_WIRED_ONLY),
               "c1": _client("c1", "MIA", adapters=_WIRELESS_ONLY)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "port_flap", "count": 2,
                                  "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0"}


def test_media_gate_fails_closed_with_no_reported_adapters(tmp_path):
    # A client that hasn't sent adapter data yet (fresh boot, or an old
    # client-sim build predating this field) is NOT eligible for a
    # media-tagged sim — the exact rollout gap that could reproduce the
    # original bug if left fail-open.
    clients = {"c0": _client("c0", "MIA", adapters=[])}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "assoc_fail", "count": 1,
                                  "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert not eng._ledger["alert:A:MIA"]["clients"]


def test_media_gate_does_not_apply_to_any_media_sims(tmp_path):
    # dns_fail is media="any" — unaffected by adapter reporting, even with none.
    clients = {"c0": _client("c0", "MIA", adapters=[])}
    spoke = _FakeSpoke(clients, [_tq(1)], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert set(eng._ledger["alert:A:MIA"]["clients"].keys()) == {"c0"}


def test_media_mismatch_surfaces_in_diag(tmp_path):
    clients = {"c0": _client("c0", "MIA", adapters=_WIRED_ONLY)}
    spoke = _FakeSpoke(clients, [{"alert_id": "A", "sim_id": "assoc_fail", "count": 1,
                                  "site": "MIA", "enabled": True}], tmp_path)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    blocked = eng._quota_diag["alert:A:MIA"]["blocked"]
    assert blocked.get("media_mismatch", 0) == 1, blocked
