"""MistClient unit tests — token auth, sites/alarms/clients/inventory parsing,
and the ``poll_site_data`` shape contract (bare ``alert_type_counts`` keys, no
``Mist:`` prefix; wired/wireless split; hw_devices from device-down alarms).

Uses a self-contained fake httpx.AsyncClient (no network). Mirrors the shape
contract ``MistPoller`` depends on (identical to ``ArubaClient.poll_site_data``).
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mist as mist_mod  # noqa: E402
from mist import MistClient  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient.

    ``routes`` maps a URL-path substring to a JSON payload. ``get`` records
    each call so a test can assert caching (one HTTP call per TTL window)."""

    def __init__(self, routes):
        self.routes = routes
        self.gets = []
        # params per get (parallel to gets) so a test can assert query params
        # like the alarms-search ``duration`` window without breaking the
        # ``gets`` substring checks other tests rely on.
        self.params = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append(url)
        self.params.append(params or {})
        for needle, payload in self.routes.items():
            if needle in url:
                return _FakeResp(payload)
        return _FakeResp({})


def _new_client(token="tok", org="org-1", host="api.mist.com"):
    return MistClient({"api_token": token, "org_id": org, "host": host})


def _patch(monkeypatch, routes):
    fake = _FakeClient(routes)
    monkeypatch.setattr(mist_mod.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


@pytest.fixture(autouse=True)
def _clear_caches():
    # Module-level caches would leak results across tests.
    for cache in (mist_mod._sites_cache, mist_mod._alarms_cache,
                 mist_mod._inventory_cache, mist_mod._site_clients_cache):
        cache.clear()
    yield
    for cache in (mist_mod._sites_cache, mist_mod._alarms_cache,
                 mist_mod._inventory_cache, mist_mod._site_clients_cache):
        cache.clear()
    # asyncio.run() closes the loop AND marks the policy _set_called, so a later
    # SYNC test that constructs asyncio.Lock() (Python 3.9, e.g. SimQuotaEngine
    # in the dongle/sim-quota tests) raises "There is no current event loop in
    # thread 'MainThread'". Leave a current open loop so get_event_loop() works.
    # Mirrors the loop-management pattern in test_hub_config.py.
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_is_configured_requires_token_and_org():
    assert not _new_client(token="", org="x").is_configured()
    assert not _new_client(token="x", org="").is_configured()
    assert _new_client().is_configured()


def test_headers_use_static_token(monkeypatch):
    c = _new_client(token="abc123")
    assert c._headers()["Authorization"] == "Token abc123"


def test_host_coercion_strips_scheme_and_path():
    assert _new_client(host="https://api.eu.mist.com/some/path").host == "api.eu.mist.com"
    assert _new_client(host="").host == "api.mist.com"  # default


def test_list_sites_parses_bare_list(monkeypatch):
    fake = _patch(monkeypatch, {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}, {"id": "s2", "name": "NYC"}],
    })
    sites = asyncio.run(_new_client().list_sites())
    names = [s["name"] for s in sites]
    assert names == ["MIA", "NYC"]
    assert sites[0]["site_id"] == "s1"


def test_poll_site_data_shape_bare_counts_and_client_split(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}],
        # 3 alarms at s1: 2 ap_offline (device-down) + 1 rogue_ap.
        "/alarms/search": {"results": [
            {"type": "ap_offline", "site_id": "s1", "severity": "critical", "hostnames": ["AP-AA"]},
            {"type": "ap_offline", "site_id": "s1", "severity": "critical", "hostnames": ["AP-CC"]},
            {"type": "rogue_ap", "site_id": "s1", "severity": "major"},
        ], "next": None},
        # 3 clients: 2 wireless (ssid/ap_mac), 1 wired (neither).
        "/sites/s1/stats/clients": [
            {"mac": "a", "ssid": "x", "ap_mac": "aa"},
            {"mac": "b", "ssid": "y", "ap_mac": "bb"},
            {"mac": "c"},
        ],
    }
    fake = _patch(monkeypatch, routes)
    data = asyncio.run(_new_client().poll_site_data("MIA", hw_check_ids={"ap_offline"}))

    # alert_type_counts keys are BARE Mist alarm types — NO "Mist:" prefix
    # (the prefix is applied only in the sim-quota catalog layer).
    assert data["alert_type_counts"] == {"ap_offline": 2, "rogue_ap": 1}
    assert all(not k.startswith("Mist:") for k in data["alert_type_counts"])

    # Wired/wireless split + total.
    assert data["wireless_clients"] == 2
    assert data["wired_clients"] == 1
    assert data["client_count"] == 3

    # hw_devices only for enrolled device-down alarm types; device name from
    # the alarm's hostnames array.
    assert data["hw_devices"] == {"ap_offline": {"AP-AA": 1, "AP-CC": 1}}

    # site_health is None for the skeleton (SLE score is a follow-on chunk).
    assert data["site_health"] is None
    # Required keys present (same shape as ArubaClient.poll_site_data).
    for k in ("site_health", "wireless_clients", "wired_clients", "client_count",
              "alert_type_counts", "insight_cat_counts", "hw_devices"):
        assert k in data


def test_poll_site_data_unconfigured_returns_empty_shape():
    data = asyncio.run(_new_client(token="").poll_site_data("MIA"))
    assert data == {"site_health": None, "wireless_clients": 0, "wired_clients": 0,
                    "client_count": 0, "alert_type_counts": {},
                    "insight_cat_counts": {}, "hw_devices": {}}


def test_global_alarm_counts_for_every_site(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}, {"id": "s2", "name": "NYC"}],
        # A global alarm (site_id unknown → resolves to "—") counts for every site.
        "/alarms/search": {"results": [
            {"type": "dns_failure", "site_id": "unknown-site", "severity": "major"},
        ], "next": None},
        "/sites/s1/stats/clients": [],
        "/sites/s2/stats/clients": [],
    }
    _patch(monkeypatch, routes)
    mia = asyncio.run(_new_client().poll_site_data("MIA"))
    nyc = asyncio.run(_new_client().poll_site_data("NYC"))
    assert mia["alert_type_counts"] == {"dns_failure": 1}
    assert nyc["alert_type_counts"] == {"dns_failure": 1}


def test_alarms_cached_within_ttl(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [],
        "/alarms/search": {"results": [{"type": "ap_offline", "site_id": "s1", "severity": "critical"}], "next": None},
    }
    fake = _patch(monkeypatch, routes)
    c = _new_client()
    asyncio.run(c._list_alarms())
    asyncio.run(c._list_alarms())
    # Only ONE alarms HTTP call across the two reads (cache hit on the second).
    alarms_gets = [u for u in fake.gets if "/alarms/search" in u]
    assert len(alarms_gets) == 1


def test_browse_passes_7d_duration_window(monkeypatch):
    """browse_all + available_checks widen the alarms window to 7d — the Mist
    /alarms/search endpoint DEFAULTS to 1d when ``duration`` is omitted, which
    hid any alarm older than 24h and left the Alerts tab empty. Pin the wider
    window so the gap (cs spoke shipped without the duration param) can't
    silently regress. The dashboard poll keeps the 1d default (separate test)."""
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}],
        "/alarms/search": {"results": [
            {"type": "ap_offline", "site_id": "s1", "severity": "critical"}], "next": None},
        "/sites/s1/stats/clients": [],
    }
    fake = _patch(monkeypatch, routes)
    asyncio.run(_new_client().browse_all())
    # The browse alarms call MUST request duration=7d (not the 1d default).
    browse_params = [p for u, p in zip(fake.gets, fake.params) if "/alarms/search" in u]
    assert browse_params, "browse_all should have fetched alarms"
    assert all(p.get("duration") == "7d" for p in browse_params), browse_params


def test_poll_keeps_1d_default_duration(monkeypatch):
    """The dashboard active-alarm poll (poll_site_data) keeps the 1d default —
    current problems only — distinct cache key from the 7d browse window."""
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}],
        "/alarms/search": {"results": [
            {"type": "ap_offline", "site_id": "s1", "severity": "critical"}], "next": None},
        "/sites/s1/stats/clients": [],
    }
    fake = _patch(monkeypatch, routes)
    asyncio.run(_new_client().poll_site_data("MIA"))
    poll_params = [p for u, p in zip(fake.gets, fake.params) if "/alarms/search" in u]
    assert poll_params, "poll_site_data should have fetched alarms"
    # 1d default (no explicit duration) — current-problems-only window.
    assert all(p.get("duration") == "1d" for p in poll_params), poll_params


def test_fetch_alarms_failure_sets_warning(monkeypatch):
    """A failed alarms fetch returns ([], warning) — NOT a silent empty list —
    so the Alerts tab can distinguish 'no alarms in window' from 'call failed'."""
    fake = _patch(monkeypatch, {"/orgs/org-1/sites": []})

    async def _boom(client, path, params=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(MistClient, "_get", _boom)
    alarms, warning = asyncio.run(_new_client()._fetch_alarms())
    assert alarms == []
    assert warning and "boom" in warning


def test_available_checks_falls_back_when_no_alarms(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [],
        "/alarms/search": {"results": [], "next": None},
    }
    _patch(monkeypatch, routes)
    out = asyncio.run(_new_client().available_checks())
    # No live alarms → the known-type fallback populates the picker.
    ids = {a["id"] for a in out["alerts"]}
    assert "ap_offline" in ids and "rogue_ap" in ids
    assert out["warning"]  # fallback warning surfaced
    # Hardware catalog only includes device-down types.
    hw_ids = {h["id"] for h in out["hardware"]}
    assert hw_ids <= {"ap_offline", "switch_down", "gateway_down"}


def test_available_checks_from_live_alarms(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}],
        "/alarms/search": {"results": [
            {"type": "ap_offline", "site_id": "s1", "severity": "critical"},
            {"type": "rogue_ap", "site_id": "s1", "severity": "major"},
        ], "next": None},
    }
    _patch(monkeypatch, routes)
    out = asyncio.run(_new_client().available_checks())
    ids = {a["id"] for a in out["alerts"]}
    assert ids == {"ap_offline", "rogue_ap"}


def test_test_connection_ok(monkeypatch):
    routes = {"/orgs/org-1": {"name": "My Org"}}
    _patch(monkeypatch, routes)
    res = asyncio.run(_new_client().test_connection())
    assert res["status"] == "SUCCESS"
    assert res["spokes"][0]["token_valid"] is True
    assert res["spokes"][0]["status"] == "Connected."


def test_test_connection_unconfigured():
    res = asyncio.run(_new_client(token="").test_connection())
    assert res["spokes"][0]["token_valid"] is False
    assert "not configured" in res["spokes"][0]["status"]


def test_browse_all_shape(monkeypatch):
    routes = {
        "/orgs/org-1/sites": [{"id": "s1", "name": "MIA"}],
        "/alarms/search": {"results": [{"type": "ap_offline", "site_id": "s1", "severity": "critical"}], "next": None},
        "/orgs/org-1/inventory": [{"serial": "SN1", "type": "ap", "site_id": "s1", "name": "AP1", "connected": True}],
        "/sites/s1/stats/clients": [{"mac": "a", "ssid": "x", "ap_mac": "aa"}],
    }
    _patch(monkeypatch, routes)
    out = asyncio.run(_new_client().browse_all())
    assert out["sites"] and out["sites"][0]["name"] == "MIA"
    assert out["alerts"] and out["alerts"][0]["name"] == "ap_offline"
    assert "MIA" in out["devices_by_site"]
    assert out["clients_by_site"]["MIA"]["wireless"] == 1
    assert out["insights"] == []  # SLE insights are a follow-on chunk