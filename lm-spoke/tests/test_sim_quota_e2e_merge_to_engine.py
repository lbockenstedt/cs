"""End-to-end-ish integration: hub merge output → SimQuotaEngine reconcile.

The hub's ``sim_quota.merge_effective_quotas`` (lm repo, hub-only by design —
the spoke never merges, it receives the enabled-only merged list as
``effective_sim_quotas``) produces the contract the cs spoke's SimQuotaEngine
consumes:

  * enabled-only (disabled rows excluded — a tenant disabled row suppresses
    the global default for that alert but contributes no enabled row);
  * per ``(alert_type, alert_id)``: tenant override wins (its enabled rows
    replace the global default's); alerts the tenant hasn't touched inherit
    the global default's enabled rows.

This test constructs a merge-shaped effective list (simulating that hub output)
and verifies the engine reconciles client assignments against it correctly —
the merge→engine handoff. The merge itself is unit-tested in the lm repo
(``core/tests/test_sim_quota.py``); this test pins the消费 side of the
contract so a merge-shape change is caught here too.
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


# ── minimal in-memory harness (mirrors test_sim_quota_engine.py) ────────────
class _FakeRegistry:
    def __init__(self, clients):
        self.clients = {h: dict(e) for h, e in clients.items()}

    def get_all(self):
        return {h: dict(e) for h, e in self.clients.items()}

    def get(self, h):
        e = self.clients.get(h)
        return dict(e) if e is not None else None

    async def set_overrides(self, hostname, overrides):
        e = self.clients.setdefault(hostname, {"hostname": hostname})
        cur = dict(e.get("overrides") or {})
        cur.update(overrides)
        e["overrides"] = cur
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
    def __init__(self, name_to_host):
        self._n2h = name_to_host

    def name_to_host(self):
        return dict(self._n2h)


class _FakeSettings:
    config_dir = None


class _FakeSpoke:
    def __init__(self, clients, quotas, tmp_path, pxmx_site_map=None,
                 name_to_host=None):
        self.registry = _FakeRegistry(clients)
        self.local_store = _FakeLocalStore(quotas, pxmx_site_map)
        self.settings = _FakeSettings()
        self.data_dir = str(tmp_path)
        self.deploy = _FakeDeploy(name_to_host or {})


def _client(host, site, online=True):
    return {"hostname": host, "last_seen": time.time() if online else 0,
            "config": {"wsite": site}, "overrides": {}}


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


@pytest.fixture(autouse=True)
def _bind_engine_loop():
    asyncio.set_event_loop(_LOOP)
    yield


def _run(coro):
    return _LOOP.run_until_complete(coro)


def test_engine_consumes_hub_merge_output(tmp_path):
    # Simulate the hub's merge_effective_quotas output for one tenant:
    #   * alert A — tenant override WON (count=2, dns_fail, MIA, exclusive).
    #     The global default for A (count=10) is suppressed; the engine must
    #     fill to the tenant's 2, not 10.
    #   * alert B — tenant silent → inherits the global default (ping_test,
    #     MIA, count=1, multi_capable). Present in the merged enabled-only list.
    #   * alert C — tenant added a DISABLED row → merge contributes no enabled
    #     row for C (and suppresses any global C). NOT in the merged list, so
    #     the engine must never assign a C client.
    merged = [
        {"alert_id": "A", "alert_type": "alert", "sim_id": "dns_fail",
         "count": 2, "site": "MIA", "multi_capable": False, "enabled": True},
        {"alert_id": "B", "alert_type": "insight", "sim_id": "ping_test",
         "count": 1, "site": "MIA", "multi_capable": True, "enabled": True},
    ]
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(5)}
    n2h = {f"c{i}": "px1" for i in range(5)}
    site_map = {"px1": "MIA"}
    spoke = _FakeSpoke(clients, merged, tmp_path,
                       pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    actions = _run(eng.reconcile())

    # A: 2 dns_fail clients (tenant override count, NOT the suppressed global 10).
    a = eng._ledger["alert:A:MIA"]
    assert len(a["clients"]) == 2
    for h in a["clients"]:
        assert spoke.registry.clients[h]["overrides"]["dns_fail"] == "on"
    # B: 1 ping_test client (multi-capable — may pack onto an A client or take
    # a free one; either way exactly one ping_test=on override exists).
    b = eng._ledger["insight:B:MIA"]
    assert len(b["clients"]) == 1
    pt = [h for h in clients
          if spoke.registry.clients[h]["overrides"].get("ping_test") == "on"]
    assert len(pt) == 1
    assert list(b["clients"].keys()) == pt
    # C never appears (merge excluded it — disabled rows contribute nothing).
    assert "alert:C:MIA" not in eng._ledger
    assert all("assoc_fail" not in (spoke.registry.clients[h]["overrides"] or {})
               for h in clients)
    # Total assigned == 2 (A) + 1 (B) = 3.
    assert actions["assigned"] == 3


def test_engine_releases_when_merge_drops_an_alert(tmp_path):
    # The merge re-runs on every hub push; if a tenant removes its override for
    # alert A (or disables it), A leaves the merged enabled-only list and the
    # engine must release A's clients on the next reconcile. Verifies the
    # release path driven by a merge-shape change.
    merged_a = [
        {"alert_id": "A", "alert_type": "alert", "sim_id": "dns_fail",
         "count": 2, "site": "MIA", "multi_capable": False, "enabled": True},
    ]
    clients = {f"c{i}": _client(f"c{i}", "MIA") for i in range(3)}
    n2h = {f"c{i}": "px1" for i in range(3)}
    site_map = {"px1": "MIA"}
    spoke = _FakeSpoke(clients, merged_a, tmp_path,
                       pxmx_site_map=site_map, name_to_host=n2h)
    eng = SimQuotaEngine(spoke)
    _run(eng.reconcile())
    assert len(eng._ledger["alert:A:MIA"]["clients"]) == 2
    assigned = list(eng._ledger["alert:A:MIA"]["clients"].keys())
    for h in assigned:
        assert spoke.registry.clients[h]["overrides"]["dns_fail"] == "on"

    # Hub re-merge drops A entirely (tenant disabled it / removed the override).
    spoke.local_store._q = []
    actions = _run(eng.reconcile())
    assert eng._ledger == {}
    assert actions["released"] == 2
    # The two assigned clients get dns_fail=off; the never-assigned third has no
    # dns_fail override at all (the engine never touched it).
    for h in assigned:
        assert spoke.registry.clients[h]["overrides"].get("dns_fail") == "off"
    unassigned = [h for h in clients if h not in assigned]
    assert len(unassigned) == 1
    assert "dns_fail" not in (spoke.registry.clients[unassigned[0]]["overrides"] or {})