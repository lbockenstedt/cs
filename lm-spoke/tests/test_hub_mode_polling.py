"""Hub (centralized) mode: the HUB polls Central / Central On-Prem / Mist, so
the spoke's pollers must not. The hub pushes ``processing_modes`` via
CS_CONFIG_UPDATE; only an explicit "centralized" idles a spoke poller, so a
standalone spoke or an older hub (no modes pushed) keeps polling."""
import asyncio

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from central_poller import CentralPoller
from cs_spoke import CSSpoke
from local_store import LocalStore
from mist_poller import MistPoller


class _CountingClient:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def is_configured():
        return True

    async def poll_site_data(self, site, hw_check_ids=None):
        self.calls += 1
        return {"client_count": 3, "wired_clients": 1, "wireless_clients": 2,
                "alert_type_counts": {}, "insight_cat_counts": {}}

    async def _nc_devices(self):
        return []

    async def _list_inventory(self):
        return []


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "configs"
    cfg.mkdir()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data, cfg)
    s.registry = ClientRegistry(data)
    s.queue = CommandQueue(data, s.settings)
    s.local_store = LocalStore(data)
    s.local_store.set_central_sites_config({"site_mappings": {"BARCELONA": "BARCELONA"}})
    s.local_store.set_central_on_prem_sites_config({"site_mappings": {"BARCELONA": "BARCELONA"}})
    s.local_store.set_mist_sites_config({"site_mappings": {"MIA": "MIA"}})
    s.central_poller = CentralPoller(s)
    s.central_on_prem_poller = CentralPoller(s, instance="central_on_prem")
    s.mist_poller = MistPoller(s)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _arm(spoke):
    clients = {}
    for name in ("central_poller", "central_on_prem_poller", "mist_poller"):
        c = _CountingClient()
        getattr(spoke, name)._client = c
        clients[name] = c
    return clients


def _poll_all(spoke, loop):
    for name in ("central_poller", "central_on_prem_poller", "mist_poller"):
        loop.run_until_complete(getattr(spoke, name)._poll_once())


def test_no_modes_pushed_spoke_keeps_polling(spoke_loop):
    spoke, loop = spoke_loop
    clients = _arm(spoke)
    _poll_all(spoke, loop)
    assert all(c.calls == 1 for c in clients.values())
    assert "BARCELONA" in spoke.central_status["status"]


def test_centralized_mode_idles_spoke_pollers(spoke_loop):
    spoke, loop = spoke_loop
    clients = _arm(spoke)
    spoke.central_status = {"status": {"BARCELONA": {}}}
    spoke.mist_status = {"status": {"MIA": {}}}
    applied = spoke._apply_hub_config({"processing_modes": {
        "central_api": "centralized", "central_on_prem_api": "centralized",
        "mist_api": "centralized"}})
    assert "processing_modes" in applied
    # Relayed status cleared immediately, not on the next poll tick.
    assert spoke.central_status == {} and spoke.mist_status == {}
    _poll_all(spoke, loop)
    assert all(c.calls == 0 for c in clients.values())
    assert spoke.central_status == {}
    assert spoke.central_on_prem_status == {}
    assert spoke.mist_status == {}
    assert spoke.local_store.get_processing_modes()["central_api"] == "centralized"


def test_mode_is_per_feature(spoke_loop):
    spoke, loop = spoke_loop
    clients = _arm(spoke)
    spoke._apply_hub_config({"processing_modes": {
        "central_api": "centralized", "central_on_prem_api": "distributed",
        "mist_api": "distributed"}})
    _poll_all(spoke, loop)
    assert clients["central_poller"].calls == 0
    assert clients["central_on_prem_poller"].calls == 1
    assert clients["mist_poller"].calls == 1


def test_switch_back_to_distributed_resumes_polling(spoke_loop):
    spoke, loop = spoke_loop
    clients = _arm(spoke)
    spoke._apply_hub_config({"processing_modes": {"central_api": "centralized"}})
    loop.run_until_complete(spoke.central_poller._poll_once())
    assert clients["central_poller"].calls == 0
    spoke._apply_hub_config({"processing_modes": {"central_api": "distributed"}})
    loop.run_until_complete(spoke.central_poller._poll_once())
    assert clients["central_poller"].calls == 1
    assert "BARCELONA" in spoke.central_status["status"]
