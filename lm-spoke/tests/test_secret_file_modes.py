"""Files holding secrets are 0600 from creation, and older ones are tightened.

The Proxmox API tokens (proxmox_tokens.json, root@pam tokens), the client API
key (cs_settings.json), the Central/Mist credentials (local_store.json), the
per-client JIT dot1x_password overrides (clients.json) and the local-webui TLS
key (privkey.pem) must never be readable by another local user, not even between the write and the rename.
Each test records the file's mode at the moment it is renamed into place.
"""
import json
import os
import stat

import pytest

import asyncio

import command_queue
from client_registry import ClientRegistry
from local_store import LocalStore
from cs_settings import CSSettings
from token_store import TokenStore


@pytest.fixture(autouse=True)
def _umask_022():
    """Use the common default umask, so a missing mode shows up as 0644."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def renames(monkeypatch):
    """Record (destination name, source mode) for every os.replace."""
    seen = []
    real = os.replace

    def _replace(src, dst, *a, **kw):
        seen.append((os.path.basename(str(dst)), stat.S_IMODE(os.stat(src).st_mode)))
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(os, "replace", _replace)
    return seen


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_token_store_writes_0600_atomically(tmp_path, renames):
    ts = TokenStore(tmp_path)
    ts.save("pve1", "root@pam!cs-hub=secret")
    path = tmp_path / "proxmox_tokens.json"
    assert json.loads(path.read_text()) == {"pve1": "root@pam!cs-hub=secret"}
    assert ("proxmox_tokens.json", 0o600) in renames
    assert _mode(path) == 0o600
    assert TokenStore(tmp_path).get("pve1") == "root@pam!cs-hub=secret"


def test_token_store_tightens_an_old_world_readable_file(tmp_path):
    path = tmp_path / "proxmox_tokens.json"
    path.write_text(json.dumps({"pve1": "tok"}))
    os.chmod(path, 0o644)
    assert TokenStore(tmp_path).get("pve1") == "tok"
    assert _mode(path) == 0o600


def test_settings_with_client_api_key_are_0600(tmp_path, renames):
    s = CSSettings(tmp_path, tmp_path)
    assert s.get("client_api_key")
    assert ("cs_settings.json", 0o600) in renames
    assert _mode(tmp_path / "cs_settings.json") == 0o600


def test_settings_tighten_an_old_world_readable_file(tmp_path):
    path = tmp_path / "cs_settings.json"
    path.write_text(json.dumps({"client_api_key": "k"}))
    os.chmod(path, 0o644)
    assert CSSettings(tmp_path, tmp_path).get("client_api_key") == "k"
    assert _mode(path) == 0o600


def test_local_store_with_central_secret_is_0600(tmp_path, renames):
    LocalStore(tmp_path).set_central_config({"client_secret": "cs-secret"})
    path = tmp_path / "local_store.json"
    assert ("local_store.json", 0o600) in renames
    assert _mode(path) == 0o600
    assert LocalStore(tmp_path).get_central_config()["client_secret"] == "cs-secret"


def test_local_store_tightens_an_old_world_readable_file(tmp_path):
    path = tmp_path / "local_store.json"
    path.write_text(json.dumps({"mist_config": {"api_token": "t"}}))
    os.chmod(path, 0o644)
    LocalStore(tmp_path)
    assert _mode(path) == 0o600


def test_client_registry_with_dot1x_password_is_0600(tmp_path, renames):
    async def _run():
        reg = ClientRegistry(tmp_path)
        await reg.set_overrides("sim-1", {"dot1x_password": "p"})
        await reg._flush_now()
    asyncio.run(_run())
    path = tmp_path / "clients.json"
    assert ("clients.json", 0o600) in renames
    assert _mode(path) == 0o600
    assert json.loads(path.read_text())["sim-1"]["overrides"]["dot1x_password"] == "p"
    renames.clear()
    reg = ClientRegistry(tmp_path)
    reg._persist()
    assert ("clients.json", 0o600) in renames


def test_client_registry_tightens_an_old_world_readable_file(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"sim-1": {"overrides": {"dot1x_password": "p"}}}))
    os.chmod(path, 0o644)
    assert ClientRegistry(tmp_path).get("sim-1")["overrides"]["dot1x_password"] == "p"
    assert _mode(path) == 0o600


def test_a_stale_temp_file_does_not_keep_its_mode(tmp_path):
    path = tmp_path / "x.json"
    tmp = tmp_path / "x.json.tmp"
    tmp.write_text("old")
    os.chmod(tmp, 0o666)
    command_queue._write_atomic(path, "new", mode=0o600)
    assert path.read_text() == "new" and _mode(path) == 0o600
    assert not tmp.exists()


def test_local_tls_key_is_0600_before_the_rename(monkeypatch, tmp_path, renames):
    from test_install_cert import _ensure_loop, _real_cert_pair, _run
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("LM_CS_ALLOW_LOCAL_CERT", "1")
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)

    async def _noop(*a, **kw):
        return None
    cp._rebind_api_server = _noop  # type: ignore[assignment]
    cp._rebind_agent_server = _noop  # type: ignore[assignment]
    fullchain, privkey = _real_cert_pair()
    assert _run(cp._apply_local_cert(fullchain, privkey))["status"] == "SUCCESS"
    key = tmp_path / "tls" / "privkey.pem"
    assert key.read_text() == privkey
    assert ("privkey.pem", 0o600) in renames
    assert ("fullchain.pem", 0o644) in renames
    # An old key left readable to others is tightened when it is next used.
    os.chmod(key, 0o644)
    assert cp._local_tls_paths()[1] == str(key)
    assert _mode(key) == 0o600


@pytest.mark.parametrize("load,name,legacy", [
    (lambda d: TokenStore(d), "proxmox_tokens.json", None),
    (lambda d: CSSettings(d, d), "cs_settings.json", None),
    (lambda d: LocalStore(d), "local_store.json", "local_store.tmp"),
    (lambda d: ClientRegistry(d), "clients.json", None),
])
def test_loading_removes_a_crash_left_temp_copy(tmp_path, load, name, legacy):
    """A temp copy of the secrets that a crash left at 0644 (under this
    version's name or an older one) is deleted when the store loads."""
    (tmp_path / name).write_text("{}")
    leftovers = [tmp_path / (name + ".tmp")] + ([tmp_path / legacy] if legacy else [])
    for t in leftovers:
        t.write_text('{"secret": "s"}')
        os.chmod(t, 0o644)
    load(tmp_path)
    assert not any(t.exists() for t in leftovers)


def test_local_tls_key_temp_copy_is_removed(monkeypatch, tmp_path):
    """A privkey.pem.tmp left by a crash is deleted when the key is used."""
    from control_plane import CSControlPlane
    tls = tmp_path / "tls"
    tls.mkdir()
    for f in ("fullchain.pem", "privkey.pem", "privkey.pem.tmp"):
        (tls / f).write_text("x")
    os.chmod(tls / "privkey.pem.tmp", 0o644)
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tls))
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    assert cp._local_tls_paths()[1] == str(tls / "privkey.pem")
    assert not (tls / "privkey.pem.tmp").exists()


@pytest.mark.parametrize("use", ["_local_tls_paths", "_purge_local_cert"])
def test_tls_key_temp_copy_without_a_final_key_is_removed(monkeypatch, tmp_path, use):
    """A crash between writing fullchain.pem and renaming privkey.pem.tmp
    leaves no final key; the temp copy is still deleted on both startup
    paths (TLS allowed, and the default plaintext purge)."""
    from control_plane import CSControlPlane
    tls = tmp_path / "tls"
    tls.mkdir()
    for f in ("fullchain.pem", "privkey.pem.tmp"):
        (tls / f).write_text("x")
    os.chmod(tls / "privkey.pem.tmp", 0o644)
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tls))
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    getattr(cp, use)()
    assert not (tls / "privkey.pem.tmp").exists()
