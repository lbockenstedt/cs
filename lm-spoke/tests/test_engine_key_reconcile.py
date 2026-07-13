"""Engine-owned override hygiene — the two SimQuotaEngine reconcile tail passes:

1. ``_reconcile_engine_keys`` — removes engine-set ``sim_id`` overrides the
   ledger no longer claims (the missed-_release leak: a transient registry
   failure dropped the ledger entry but left ``sim_id=on``, so the client
   kept running a sim no quota was paying for). Provenance via
   ``engine_keys`` means a human manual pin is never touched.

2. ``_reconcile_prune_defaults`` — re-prunes every client's on/off overrides
   against the CURRENT bucket default, dropping flags that became no-ops
   when the bucket config changed later.

Both are pure hygiene: served config is unchanged (only no-op overrides are
removed) and only keys the ENGINE set are ever removed. Uses the REAL
``ClientRegistry`` (with a fake bucket_resolver) so provenance + prune run
for real, plus a minimal spoke stub to host a ``SimQuotaEngine``.
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

from client_registry import ClientRegistry  # noqa: E402
from sim_quota_engine import SimQuotaEngine  # noqa: E402


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def _bind_loop():
    asyncio.set_event_loop(_LOOP)
    yield


# ── registry-level: set_engine_overrides / remove_engine_keys / prune ──────

def _reg(tmp_path, resolver=None):
    return ClientRegistry(tmp_path / "data", bucket_resolver=resolver)


def test_set_engine_overrides_records_engine_keys(tmp_path):
    reg = _reg(tmp_path)  # no resolver → no prune
    entry = _run(reg.set_engine_overrides("host-a", {"dns_fail": "on", "wsite": "MIA"}))
    assert entry["overrides"] == {"dns_fail": "on", "wsite": "MIA"}
    assert set(entry["engine_keys"]) == {"dns_fail", "wsite"}


def test_set_engine_overrides_prunes_and_drops_key(tmp_path):
    # Bucket default dns_fail=on → the engine-set "on" matches the bucket →
    # pruned from overrides AND from engine_keys (nothing to revert later).
    reg = _reg(tmp_path, resolver=lambda hn: {"dns_fail": "on"})
    entry = _run(reg.set_engine_overrides("host-a", {"dns_fail": "on"}))
    assert entry["overrides"] == {}
    assert entry["engine_keys"] == []


def test_set_engine_overrides_merges_existing_engine_keys(tmp_path):
    reg = _reg(tmp_path)
    _run(reg.set_engine_overrides("host-a", {"dns_fail": "on"}))
    entry = _run(reg.set_engine_overrides("host-a", {"ping_test": "on"}))
    assert set(entry["overrides"]) == {"dns_fail", "ping_test"}
    assert set(entry["engine_keys"]) == {"dns_fail", "ping_test"}


def test_set_overrides_does_not_record_engine_keys(tmp_path):
    # A HUMAN pin via the Control Panel path must NOT be marked engine-owned
    # — provenance: the reconcile tail never removes a human pin.
    reg = _reg(tmp_path)
    entry = _run(reg.set_overrides("host-a", {"dns_fail": "on"}))
    assert entry["overrides"] == {"dns_fail": "on"}
    assert entry.get("engine_keys") is None


def test_remove_engine_keys_removes_from_overrides_and_engine_keys(tmp_path):
    reg = _reg(tmp_path)
    _run(reg.set_engine_overrides("host-a", {"dns_fail": "on", "wsite": "MIA"}))
    entry = _run(reg.remove_engine_keys("host-a", ["dns_fail"]))
    assert entry["overrides"] == {"wsite": "MIA"}
    assert entry["engine_keys"] == ["wsite"]


def test_remove_engine_keys_noop_for_absent_key(tmp_path):
    reg = _reg(tmp_path)
    _run(reg.set_engine_overrides("host-a", {"dns_fail": "on"}))
    entry = _run(reg.remove_engine_keys("host-a", ["kill_switch"]))  # not set
    assert entry["overrides"] == {"dns_fail": "on"}
    assert entry["engine_keys"] == ["dns_fail"]


def test_prune_against_bucket_drops_now_matching(tmp_path):
    # The flag was a real deviation when the bucket was on; now the bucket
    # flipped off, so "off" matches the bucket → re-pruned (no-op removal).
    reg = _reg(tmp_path, resolver=lambda hn: {"dns_fail": ""})  # bucket off
    _run(reg.set_overrides("host-a", {"dns_fail": "off"}))  # no resolver prune path? set with resolver
    # set_overrides prunes at write time against the SAME resolver, so this
    # would already be pruned. To exercise the "bucket changed later" path,
    # write the override with NO resolver, then add the resolver and re-prune.
    reg2 = _reg(tmp_path, resolver=None)
    _run(reg2.set_overrides("host-a", {"dns_fail": "off"}))
    reg2._bucket_resolver = lambda hn: {"dns_fail": ""}  # bucket now off
    entry = _run(reg2.prune_against_bucket("host-a"))
    assert entry["overrides"] == {}


def test_prune_against_bucket_keeps_deviation(tmp_path):
    reg = _reg(tmp_path, resolver=None)
    _run(reg.set_overrides("host-a", {"dns_fail": "on"}))
    reg._bucket_resolver = lambda hn: {"dns_fail": ""}  # bucket off; "on" deviates
    entry = _run(reg.prune_against_bucket("host-a"))
    assert entry["overrides"] == {"dns_fail": "on"}


def test_prune_against_bucket_skips_nontoggle(tmp_path):
    # wsite is free-form, never a prune candidate.
    reg = _reg(tmp_path, resolver=lambda hn: {"wsite": ""})
    _run(reg.set_overrides("host-a", {"wsite": "MIA"}))
    entry = _run(reg.prune_against_bucket("host-a"))
    assert entry["overrides"] == {"wsite": "MIA"}


# ── engine tail: _reconcile_engine_keys + _reconcile_prune_defaults ────────

class _FakeLocalStore:
    def __init__(self, quotas=None, pxmx_site_map=None):
        self._q = quotas or []
        self._map = pxmx_site_map or {}

    def get_effective_sim_quotas(self):
        return list(self._q)

    def get_pxmx_site_map(self):
        return dict(self._map)

    def get_sim_shareable(self):
        return {}

    def get_ssid_placement(self):
        return {}

    def get_ssid_matrix(self):
        return []


class _FakeDeploy:
    def __init__(self, name_to_host=None):
        self._n2h = name_to_host or {}

    def name_to_host(self):
        return dict(self._n2h)


class _FakeSettings:
    config_dir = None  # skip sim_config in _effective_site


class _Spoke:
    """Minimal spoke hosting a real ClientRegistry + SimQuotaEngine."""
    def __init__(self, tmp_path, resolver=None, name_to_host=None,
                 pxmx_site_map=None):
        self.registry = ClientRegistry(tmp_path / "data", bucket_resolver=resolver)
        self.local_store = _FakeLocalStore([], pxmx_site_map)
        self.settings = _FakeSettings()
        self.data_dir = str(tmp_path)
        self.deploy = _FakeDeploy(name_to_host)


def _seed(reg, hostname, overrides, engine_keys=None, last_seen=None, cfg=None):
    """Write a registry entry directly (bypasses prune) to stage a scenario."""
    reg.clients[hostname] = {
        "hostname": hostname,
        "last_seen": last_seen if last_seen is not None else time.time(),
        "config": cfg or {"wsite": "MIA"},
        "overrides": dict(overrides),
        "engine_keys": list(engine_keys or []),
    }


def test_reconcile_engine_keys_removes_orphan_on(tmp_path):
    # dns_fail=on was engine-set but the ledger no longer claims it → orphan.
    spoke = _Spoke(tmp_path)
    _seed(spoke.registry, "host-a", {"dns_fail": "on"}, engine_keys=["dns_fail"])
    eng = SimQuotaEngine(spoke)
    # ledger empty → no claims
    _run(eng._reconcile_engine_keys(spoke.registry.get_all()))
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {}
    assert entry["engine_keys"] == []


def test_reconcile_engine_keys_leaves_claimed(tmp_path):
    # Ledger still claims dns_fail for host-a → NOT an orphan → kept.
    spoke = _Spoke(tmp_path)
    _seed(spoke.registry, "host-a", {"dns_fail": "on"}, engine_keys=["dns_fail"])
    eng = SimQuotaEngine(spoke)
    eng._ledger = {"alert:A:MIA": {"sim_id": "dns_fail", "site": "MIA",
                                   "clients": {"host-a": "MIA"}}}
    _run(eng._reconcile_engine_keys(spoke.registry.get_all()))
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {"dns_fail": "on"}
    assert entry["engine_keys"] == ["dns_fail"]


def test_reconcile_engine_keys_leaves_human_off(tmp_path):
    # A human turned dns_fail OFF (engine dropped the ledger entry at
    # `not _has_sim_on`). The tail must NOT remove the human's "off" — only
    # engine-set "on" orphans are removed.
    spoke = _Spoke(tmp_path)
    _seed(spoke.registry, "host-a", {"dns_fail": "off"}, engine_keys=["dns_fail"])
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_engine_keys(spoke.registry.get_all()))
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {"dns_fail": "off"}


def test_reconcile_engine_keys_skips_wsite(tmp_path):
    # wsite is reverted by _release's from_site path, not the tail.
    spoke = _Spoke(tmp_path)
    _seed(spoke.registry, "host-a", {"wsite": "MIA"}, engine_keys=["wsite"])
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_engine_keys(spoke.registry.get_all()))
    entry = spoke.registry.get("host-a")
    assert entry["overrides"] == {"wsite": "MIA"}
    assert entry["engine_keys"] == ["wsite"]


def test_reconcile_engine_keys_never_touches_human_pin(tmp_path):
    # A pure human pin (no engine_keys record) is invisible to the tail.
    spoke = _Spoke(tmp_path)
    _seed(spoke.registry, "host-a", {"dns_fail": "on"}, engine_keys=[])
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_engine_keys(spoke.registry.get_all()))
    assert spoke.registry.get("host-a")["overrides"] == {"dns_fail": "on"}


def test_reconcile_prune_defaults_drops_now_matching(tmp_path):
    # Bucket default for dns_fail is OFF; a lingering "off" override is a
    # no-op → re-pruned across every client in one sweep.
    spoke = _Spoke(tmp_path, resolver=lambda hn: {"dns_fail": ""})
    _seed(spoke.registry, "host-a", {"dns_fail": "off"})
    _seed(spoke.registry, "host-b", {"dns_fail": "off"})
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_prune_defaults(spoke.registry.get_all()))
    assert spoke.registry.get("host-a")["overrides"] == {}
    assert spoke.registry.get("host-b")["overrides"] == {}


def test_reconcile_prune_defaults_keeps_deviation(tmp_path):
    spoke = _Spoke(tmp_path, resolver=lambda hn: {"dns_fail": ""})  # bucket off
    _seed(spoke.registry, "host-a", {"dns_fail": "on"})  # real deviation
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_prune_defaults(spoke.registry.get_all()))
    assert spoke.registry.get("host-a")["overrides"] == {"dns_fail": "on"}


def test_reconcile_prune_defaults_skips_clients_without_toggle_flags(tmp_path):
    # wsite-only clients have no on/off values → skipped (no resolver call).
    spoke = _Spoke(tmp_path, resolver=lambda hn: {"wsite": ""})
    _seed(spoke.registry, "host-a", {"wsite": "MIA"})
    eng = SimQuotaEngine(spoke)
    _run(eng._reconcile_prune_defaults(spoke.registry.get_all()))
    assert spoke.registry.get("host-a")["overrides"] == {"wsite": "MIA"}


def test_full_reconcile_runs_both_tail_passes(tmp_path):
    # End-to-end: an empty effective-quota set + a client carrying an orphan
    # engine sim + a now-redundant "off" → both are cleaned in one reconcile().
    spoke = _Spoke(tmp_path, resolver=lambda hn: {"dns_fail": ""})
    _seed(spoke.registry, "host-a",
          {"dns_fail": "on", "kill_switch": "off"}, engine_keys=["dns_fail"])
    eng = SimQuotaEngine(spoke)
    # No effective quotas → ledger walk is empty, then the tail passes run.
    _run(eng.reconcile())
    entry = spoke.registry.get("host-a")
    # dns_fail: engine-set "on" orphan → removed by _reconcile_engine_keys.
    # kill_switch: human "off", bucket off → matches → pruned by re-prune.
    assert entry["overrides"] == {}
    assert entry["engine_keys"] == []