"""JIT 802.1X on the cs spoke: the SimQuotaEngine ``_reconcile_dot1x`` pass
(PROVISION / RELEASE / SYNC toward the hub broker), the hub's
``CS_DOT1X_CREDENTIAL`` reply (engine-owned ``dot1x_password``) and the
masking of that password everywhere overrides leave the spoke."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sim_config  # noqa: E402
import sim_quota_engine as sqe  # noqa: E402
from client_registry import ClientRegistry  # noqa: E402
from command_handlers.handlers_clients import ClientCommandsMixin  # noqa: E402
from sim_quota_engine import SimQuotaEngine  # noqa: E402

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(autouse=True)
def _bind_loop():
    asyncio.set_event_loop(_LOOP)
    yield


CELLS = {
    "MIA-1X": {"name": "MIA-1X", "site": "MIA", "ssid": "1X", "auth": "1x", "dot1x_account": "ldap"},
    "MIA-PSK": {"name": "MIA-PSK", "site": "MIA", "ssid": "PSK", "auth": "psk", "dot1x_account": "ldap"},
    "DFW-1X": {"name": "DFW-1X", "site": "DFW", "ssid": "1X", "auth": "1x"},  # manual
}


class _CP:
    def __init__(self):
        self.sent = []
        self.up = True

    async def send_to_hub(self, t, d):
        if not self.up:
            return False
        self.sent.append((t, json.loads(json.dumps(d))))
        return True


class _Store:
    def get_pxmx_site_map(self):
        return {}


class _Settings:
    config_dir = None


class _Spoke(ClientCommandsMixin):
    def __init__(self, tmp_path):
        self.registry = ClientRegistry(tmp_path / "data")
        self.local_store = _Store()
        self.settings = _Settings()
        self.data_dir = str(tmp_path)
        self.deploy = None
        self.control_plane = _CP()
        self.refreshed = []
        self.sim_quota_engine = SimQuotaEngine(self)
        self.sim_quota_engine._cells_by_name = dict(CELLS)

    async def _on_client_override_changed(self, hostname):
        self.refreshed.append(hostname)


def _client(spoke, h, site="MIA", ssid="1X", last_seen=None):
    spoke.registry.clients[h] = {"hostname": h, "last_seen": last_seen or time.time(),
                                 "config": {"wsite": site, "ssid": ssid}, "overrides": {}}


def _types(spoke):
    return [t for t, _ in spoke.control_plane.sent]


def test_cell_backend_detection():
    b = SimQuotaEngine._dot1x_cell_backend
    assert b(CELLS["MIA-1X"]) == "ldap"
    assert b(CELLS["MIA-PSK"]) == ""          # not a 1X cell
    assert b(CELLS["DFW-1X"]) == ""           # manual
    assert b({"ssid": "1x", "dot1x_account": "entra"}) == "entra"   # legacy no-auth cell
    assert b({"auth": "1x", "dot1x_account": "radius"}) == ""


def test_provision_credential_and_release(tmp_path):
    spoke = _Spoke(tmp_path)
    eng = spoke.sim_quota_engine
    _client(spoke, "jsmith-1")
    _client(spoke, "amy-1", ssid="PSK")
    _client(spoke, "bob-1", site="DFW")
    now = time.time()
    _run(eng._reconcile_dot1x(now))
    prov = [d for t, d in spoke.control_plane.sent if t == "CS_DOT1X_PROVISION"]
    assert prov == [{"hostname": "jsmith-1", "username": "jsmith", "backend": "ldap", "cell": "MIA-1X"}]
    assert "CS_DOT1X_SYNC" in _types(spoke)
    # not re-requested inside the resend window
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + 10))
    assert spoke.control_plane.sent == []

    # hub delivers the credential
    res = _run(spoke._dispatch_clients("CS_DOT1X_CREDENTIAL", {
        "username": "jsmith", "backend": "ldap", "hostnames": ["jsmith-1", "other-1"],
        "password": "S3cret!pw", "version": 123.0}))
    assert res["status"] == "SUCCESS" and res["applied"] == ["jsmith-1"]
    assert res["skipped"] == ["other-1"] and "S3cret" not in json.dumps(res)
    entry = spoke.registry.get("jsmith-1")
    assert entry["overrides"]["dot1x_password"] == "S3cret!pw"
    assert "dot1x_password" in entry["engine_keys"]
    assert spoke.refreshed == ["jsmith-1"]
    st = json.loads((tmp_path / "sim_quota_dot1x.json").read_text())
    assert st["jsmith-1"]["delivered"] is True and st["jsmith-1"]["version"] == 123.0

    # delivered → not re-requested even after the resend window
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + sqe.DOT1X_RESEND_S + 1))
    assert "CS_DOT1X_PROVISION" not in _types(spoke)

    # engine moves the client off the 1X cell → RELEASE + password removed
    _run(spoke.registry.set_engine_overrides("jsmith-1", {"ssid": "PSK"}))
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + 20))
    assert spoke.control_plane.sent[0] == ("CS_DOT1X_RELEASE",
                                           {"hostname": "jsmith-1", "username": "jsmith", "backend": "ldap"})
    assert "dot1x_password" not in spoke.registry.get("jsmith-1")["overrides"]
    assert eng._dot1x == {}


def test_torn_down_client_is_released(tmp_path):
    spoke = _Spoke(tmp_path)
    eng = spoke.sim_quota_engine
    _client(spoke, "jsmith-1")
    now = time.time()
    _run(eng._reconcile_dot1x(now))
    spoke.registry.clients["jsmith-1"]["last_seen"] = now - sqe.DOT1X_STALE_S - 5
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + 1))
    assert _types(spoke)[0] == "CS_DOT1X_RELEASE"
    # removed from the registry entirely (purge) → also released
    _client(spoke, "amy-1")
    _run(eng._reconcile_dot1x(now + 2))
    spoke.registry.clients.pop("amy-1")
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + 3))
    assert spoke.control_plane.sent[0][0] == "CS_DOT1X_RELEASE"


def test_failed_send_is_retried(tmp_path):
    spoke = _Spoke(tmp_path)
    eng = spoke.sim_quota_engine
    _client(spoke, "jsmith-1")
    spoke.control_plane.up = False
    _run(eng._reconcile_dot1x(time.time()))
    assert eng._dot1x == {}
    spoke.control_plane.up = True
    _run(eng._reconcile_dot1x(time.time()))
    assert "CS_DOT1X_PROVISION" in _types(spoke)


def test_revoke_and_state_survives_restart(tmp_path):
    spoke = _Spoke(tmp_path)
    eng = spoke.sim_quota_engine
    _client(spoke, "jsmith-1")
    _run(eng._reconcile_dot1x(time.time()))
    _run(eng.dot1x_credential({"username": "jsmith", "backend": "ldap",
                               "hostnames": ["jsmith-1"], "password": "pw1", "version": 5}))
    eng2 = SimQuotaEngine(spoke)
    assert eng2._dot1x["jsmith-1"]["delivered"] is True
    res = _run(eng.dot1x_credential({"username": "jsmith", "backend": "ldap",
                                     "hostnames": ["jsmith-1"], "revoke": True}))
    assert res["applied"] == ["jsmith-1"] and res["revoked"] is True
    assert "dot1x_password" not in spoke.registry.get("jsmith-1")["overrides"]
    assert eng._dot1x == {}
    # missing password (non-revoke) is rejected
    assert _run(eng.dot1x_credential({"username": "jsmith", "backend": "ldap",
                                      "hostnames": ["jsmith-1"]}))["status"] == "ERROR"


def test_overrides_masked_and_mask_not_written_back(tmp_path):
    spoke = _Spoke(tmp_path)
    _run(spoke.registry.set_engine_overrides("jsmith-1", {"dot1x_password": "real", "ssid": "1X"}))
    got = _run(spoke._dispatch_clients("CS_GET_CLIENT_OVERRIDES", {"hostname": "jsmith-1"}))
    assert got["overrides"]["dot1x_password"] == sim_config.SECRET_MASK
    # Control Panel "Apply" round-trips the masked value
    res = _run(spoke._dispatch_clients("CS_SET_CLIENT_OVERRIDES", {
        "hostname": "jsmith-1", "overrides": dict(got["overrides"], dns_fail="on")}))
    assert res["overrides"]["dot1x_password"] == sim_config.SECRET_MASK
    assert spoke.registry.get("jsmith-1")["overrides"]["dot1x_password"] == "real"


def test_clear_overrides_triggers_redelivery(tmp_path):
    spoke = _Spoke(tmp_path)
    eng = spoke.sim_quota_engine
    _client(spoke, "jsmith-1")
    now = time.time()
    _run(eng._reconcile_dot1x(now))
    _run(eng.dot1x_credential({"username": "jsmith", "backend": "ldap",
                               "hostnames": ["jsmith-1"], "password": "pw1", "version": 5}))
    _run(spoke._dispatch_clients("CS_CLEAR_CLIENT_OVERRIDES", {"hostname": "jsmith-1"}))
    spoke.control_plane.sent.clear()
    _run(eng._reconcile_dot1x(now + 1))
    assert "CS_DOT1X_PROVISION" in _types(spoke)


def test_client_rows_mask_password():
    assert sim_config.mask_secret_overrides({"dot1x_password": "x", "a": 1}) == {
        "dot1x_password": sim_config.SECRET_MASK, "a": 1}
    assert sim_config.mask_secret_overrides({"dot1x_password": ""}) == {"dot1x_password": ""}
    assert sim_config.strip_masked_secrets({"dot1x_password": sim_config.SECRET_MASK}) == {}
