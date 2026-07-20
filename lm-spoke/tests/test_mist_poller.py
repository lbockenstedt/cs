"""MistPoller shape contract — ``spoke.mist_status`` must match the
``central_status`` shape (status/hardware_alerts/client_count_status/health/
fetched_at) so ``sim-views.js``'s Checks/Hardware/Client-Count tabs render Mist
data identically to Central. Uses a fake spoke + fake MistClient (no network)."""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mist_poller as mist_poller_mod  # noqa: E402
from mist_poller import MistPoller  # noqa: E402


class _FakeLocalStore:
    def __init__(self, path, mist_config, mist_sites_config):
        self._path = path
        self._mist_config = mist_config
        self._mist_sites_config = mist_sites_config

    def get_mist_config(self):
        return dict(self._mist_config)

    def get_mist_sites_config(self):
        return dict(self._mist_sites_config)


class _FakeSpoke:
    def __init__(self, local_store):
        self.local_store = local_store
        self.spoke_id = "spoke1"
        self.mist_status = {}


class _FakeMistClient:
    """Minimal stand-in for a configured MistClient — returns canned
    poll_site_data + an empty inventory so the per-device hw path no-ops."""

    @staticmethod
    def is_configured():
        return True

    async def poll_site_data(self, site, hw_check_ids=None):
        return {
            "site_health": None,
            "wireless_clients": 2,
            "wired_clients": 1,
            "client_count": 3,
            "alert_type_counts": {"ap_offline": 2, "rogue_ap": 1},
            "insight_cat_counts": {},
            "hw_devices": {"ap_offline": {"AP1": 1, "AP2": 1}},
        }

    async def _list_inventory(self):
        return []


def _make_poller(tmp_path, sites_cfg=None, configured=True):
    mist_config = {"api_token": "tok", "org_id": "org-1"} if configured else {}
    sites = sites_cfg or {
        "site_mappings": {"MIA": "MIA"},
        "monitored_checks": [{"id": "ap_offline"}],
        "hardware_checks": [{"id": "ap_offline", "name": "APs Offline", "device_type": "ap"}],
    }
    store = _FakeLocalStore(tmp_path / "local_store.json", mist_config, sites)
    spoke = _FakeSpoke(store)
    poller = MistPoller(spoke)
    # Force a configured fake client (bypass reload's real MistClient build).
    poller._client = _FakeMistClient() if configured else None
    return spoke, poller


@pytest.fixture(autouse=True)
def _restore_loop():
    # asyncio.run() poisons the Py3.9 event-loop policy (sets _set_called +
    # _loop=None) so a later SYNC test constructing asyncio.Lock() (e.g.
    # SimQuotaEngine in the dongle/sim-quota tests) raises "no current event
    # loop". Leave a current open loop after each test. Mirrors test_hub_config.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_poll_once_writes_central_status_shape(tmp_path):
    spoke, poller = _make_poller(tmp_path)
    asyncio.run(poller._poll_once())
    s = spoke.mist_status
    assert set(s) >= {"status", "hardware_alerts", "client_count_status", "health", "fetched_at"}
    # monitored check "ap_offline" evaluated (inverted: present -> ok).
    assert s["status"]["MIA"]["ap_offline"]["status"] == "ok"
    # The Steady Client Count check is surfaced per site.
    assert "Steady Client Count 1hr Average" in s["status"]["MIA"]
    # hardware_alerts roll up hw_devices totals.
    assert any(h["id"] == "ap_offline" and h["total"] == 2 for h in s["hardware_alerts"])
    # client_count_status carries the wired/wireless split.
    cc = s["client_count_status"]["MIA"]
    assert cc["wired"] == 1 and cc["wireless"] == 2


def test_poll_once_unconfigured_empties_status(tmp_path):
    spoke, poller = _make_poller(tmp_path, configured=False)
    asyncio.run(poller._poll_once())
    assert spoke.mist_status == {}


def test_poll_once_records_health_history(tmp_path):
    spoke, poller = _make_poller(tmp_path)
    asyncio.run(poller._poll_once())
    # summary keyed by site → check → daily buckets; ap_offline recorded.
    daily = spoke.mist_status["health"]
    assert "MIA" in daily
    assert "ap_offline" in daily["MIA"]