"""CSSpoke INSTALL_CERT handler — relays a hub-delivered TLS cert to each
managed pxmx agent (→ ``pvenode cert set`` on that node's pveproxy).

In the split topology the cs/lm-spoke owns the pxmx agents (they dial
``wss://<cs>:443/ws/agent``), so the hub routes the ``simulation`` cert target
here. This mirrors ProxmoxSpoke's INSTALL_CERT branch
(``proxmox_spoke.py:203-213``); the agent's ``install_cert`` (pvenode +
fingerprint verify) is covered by the pxmx agent test suite — here we assert
the cs spoke's RELAY + aggregation only. (Local-webui HTTPS apply is added in
a later phase and covered separately.)
"""
import asyncio

from cs_spoke import CSSpoke

_PEM = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n"
_KEY = "-----BEGIN PRIVATE KEY-----\nY\n-----END PRIVATE KEY-----\n"


class _FakeCP:
    """Stand-in for CSControlPlane: connected_agents + send_to_agent +
    _apply_local_cert (the local-webui HTTPS apply). ``webui`` controls the
    apply result (default SUCCESS); ``webui_exc`` makes it raise."""
    def __init__(self, agents, responses=None, exc=None,
                 webui=None, webui_exc=None):
        self.connected_agents = agents
        self._responses = responses or {}
        self._exc = exc
        self._webui = webui if webui is not None else {
            "status": "SUCCESS", "message": "local webui HTTPS applied"}
        self._webui_exc = webui_exc
        self.sent = []
        self.applied_certs = []

    async def send_to_agent(self, command, data, agent_id, timeout=None):
        self.sent.append({"agent_id": agent_id, "cmd": command,
                          "data": data, "timeout": timeout})
        if self._exc:
            raise self._exc
        return self._responses.get(
            agent_id,
            {"payload": {"data": {"status": "ERROR", "message": "no stub"}}})

    async def _apply_local_cert(self, fullchain, privkey):
        self.applied_certs.append({"fullchain": fullchain, "privkey": privkey})
        if self._webui_exc:
            raise self._webui_exc
        return dict(self._webui)


def _ensure_loop():
    """CSSpoke/CSControlPlane construct asyncio.Lock() at __init__, which on
    py3.9 binds to the current event loop — a prior suite test may have closed
    / removed it. Make sure a live loop is current before construction + runs."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _spoke() -> CSSpoke:
    _ensure_loop()
    return CSSpoke("test-cs", {})


def _run(coro):
    _ensure_loop()
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


def _cert_data():
    return {"domain": "a.example.com", "fullchain": _PEM, "privkey": _KEY,
            "chain": "", "identifier": "", "module_type": "simulation"}


def test_install_cert_broadcasts_to_all_connected_agents():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={
            "node-a": {"payload": {"data": {"status": "SUCCESS", "message": "ok-a"}}},
            "node-b": {"payload": {"data": {"status": "SUCCESS", "message": "ok-b"}}},
        })
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "SUCCESS"
    assert res["message"].startswith("deployed to 2/2")
    # Every connected agent was contacted.
    sids = {c["agent_id"] for c in s.control_plane.sent}
    assert sids == {"node-a", "node-b"}
    # The cert material + 620s relay window are passed through to each agent.
    for c in s.control_plane.sent:
        assert c["cmd"] == "INSTALL_CERT"
        assert c["data"]["fullchain"] == _PEM
        assert c["data"]["module_type"] == "simulation"
        assert c["timeout"] == 620.0  # > agent 600s pvenode wait; < hub 640s
    nodes = {n["agent_id"]: n for n in res["nodes"]}
    assert nodes["node-a"]["status"] == "SUCCESS"
    assert nodes["node-b"]["status"] == "SUCCESS"


def test_install_cert_one_agent_error_does_not_abort_others():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={
            "node-a": {"payload": {"data": {"status": "SUCCESS"}}},
            "node-b": {"payload": {"data": {"status": "ERROR",
                                            "message": "pvenode failed"}}},
        })
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"  # not all succeeded
    assert "1/2" in res["message"]
    nodes = {n["agent_id"]: n for n in res["nodes"]}
    assert nodes["node-a"]["status"] == "SUCCESS"
    assert nodes["node-b"]["status"] == "ERROR"
    # Both agents were still contacted (no short-circuit on the ERROR).
    assert {c["agent_id"] for c in s.control_plane.sent} == {"node-a", "node-b"}


def test_install_cert_no_agents_connected_is_deferred():
    """No connected agents at all → DEFERRED (not ERROR): the hub retries the
    target every distribution sweep, so this self-heals on agent reconnect —
    surfacing it as a hard FAILED would just alarm the operator about a
    transient condition (see cs_spoke._install_cert_relay)."""
    s = _spoke()
    s.control_plane = _FakeCP({})
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "DEFERRED"
    assert "no managed pxmx agents connected" in res["message"]
    assert s.control_plane.sent == []


def test_install_cert_explicit_agent_id_targets_one_node():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS",
                                                    "message": "ok"}}}})
    data = dict(_cert_data())
    data["agent_id"] = "node-a"
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "SUCCESS"
    assert [c["agent_id"] for c in s.control_plane.sent] == ["node-a"]


def test_install_cert_identifier_targets_one_node():
    """The hub's INSTALL_CERT payload carries the target ``identifier`` (not
    ``agent_id``); for a per-node ``simulation`` target the identifier IS the
    pxmx agent_id. The relay must fall back to ``identifier`` (parity with the
    pxmx spoke's ``_agent_for_node(data.get("identifier"))``) — else a per-node
    target silently broadcasts to every node instead of the one clicked."""
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}, "node-b": {"hostname": "b"}},
        responses={"node-b": {"payload": {"data": {"status": "SUCCESS",
                                                   "message": "ok"}}}})
    data = dict(_cert_data())
    data["identifier"] = "node-b"  # no agent_id — the hub sends identifier
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "SUCCESS"
    assert [c["agent_id"] for c in s.control_plane.sent] == ["node-b"]


def test_install_cert_explicit_agent_not_connected_broadcasts_to_fleet():
    """An explicit target id that is NOT a connected agent (e.g. the hub's
    wildcard fan-out sends a spoke/group id, never an agent id) falls through
    to a fleet-wide broadcast rather than hard-failing — see the fix in
    cs_spoke._install_cert_relay: 'a specific-but-offline node falls through
    to the fleet too' is the documented, intentional behavior."""
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS",
                                                    "message": "ok"}}}})
    data = dict(_cert_data())
    data["agent_id"] = "node-z"
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "SUCCESS"
    # Broadcast to every connected agent, not the missing "node-z".
    assert [c["agent_id"] for c in s.control_plane.sent] == ["node-a"]


def test_install_cert_send_to_agent_exception_is_caught_per_agent():
    s = _spoke()
    s.control_plane = _FakeCP({"node-a": {"hostname": "a"}},
                              exc=RuntimeError("relay exploded"))
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"
    assert res["nodes"][0]["status"] == "ERROR"
    assert "relay exploded" in res["nodes"][0]["message"]


def test_install_cert_no_control_plane_is_error():
    s = _spoke()
    s.control_plane = None
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "ERROR"


def test_install_cert_applies_cert_to_local_webui():
    """INSTALL_CERT also applies the cert to the cs spoke's own 8080 dashboard
    (control_plane._apply_local_cert) — the user wants the cert on the pxmx
    servers AND the cs spoke's local webui. Both succeed → overall SUCCESS."""
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS"}}}})
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "SUCCESS"
    # The local webui apply was handed the delivered cert material.
    assert len(s.control_plane.applied_certs) == 1
    assert s.control_plane.applied_certs[0]["fullchain"] == _PEM
    assert s.control_plane.applied_certs[0]["privkey"] == _KEY
    assert "webui success" in res["message"]


def test_install_cert_webui_failure_does_not_flip_successful_relay():
    """A local-webui apply failure is surfaced in the message but must NOT flip
    a successful node relay to ERROR — the cert IS deployed to the nodes, so the
    target badge must be green. (Previously the overall status went ERROR
    whenever the webui apply failed, so a deployed cert showed failed on the
    UI — the operator's complaint.) The webui error is still reported, not
    dropped."""
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS"}}}},
        webui={"status": "ERROR", "message": "write failed: permission denied"})
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    assert res["status"] == "SUCCESS"  # node deploy succeeded
    assert "webui error" in res["message"]
    assert "permission denied" in res["message"]
    # The node relay still ran + is reported.
    assert res["nodes"][0]["status"] == "SUCCESS"


def test_install_cert_webui_exception_does_not_raise_or_flip_relay():
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS"}}}},
        webui_exc=RuntimeError("rebind exploded"))
    res = _run(s.handle_command("INSTALL_CERT", _cert_data()))
    # Caught → relay SUCCESS preserved, the exception surfaced in the message.
    assert res["status"] == "SUCCESS"
    assert "rebind exploded" in res["message"]


def test_install_cert_hypervisor_target_relays_only_no_webui_apply():
    """A 'hypervisor' target routed to the cs spoke (split topology: cs owns the
    pxmx agents) is destined for a pxmx node's pveproxy, NOT this spoke's
    dashboard. It must relay only and NOT call _apply_local_cert — otherwise a
    webui failure would flip a successful node deploy red, and rebinding the
    cs dashboard is the wrong side effect for a hypervisor-scoped target."""
    s = _spoke()
    s.control_plane = _FakeCP(
        {"node-a": {"hostname": "a"}},
        responses={"node-a": {"payload": {"data": {"status": "SUCCESS"}}}},
        webui={"status": "ERROR", "message": "must NOT be called"})
    data = _cert_data()
    data["module_type"] = "hypervisor"
    res = _run(s.handle_command("INSTALL_CERT", data))
    assert res["status"] == "SUCCESS"
    # The relay ran.
    assert len(s.control_plane.sent) == 1
    # The local-webui apply was NOT invoked (hypervisor target = relay only).
    assert s.control_plane.applied_certs == []
    # No webui note in the message (the dashboard wasn't touched).
    assert "webui" not in res["message"]


# ── _apply_local_cert (the control plane's local-webui HTTPS apply) ──────────

def _real_cert_pair():
    """Generate a real self-signed cert+key PEM (so ssl.load_cert_chain
    validates). Skips if cryptography isn't installed."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime
    except Exception:
        import pytest
        pytest.skip("cryptography not installed")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lm-test")])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    fullchain = cert.public_bytes(serialization.Encoding.PEM).decode()
    privkey = key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    return fullchain, privkey


def test_apply_local_cert_writes_files_and_rebinds(monkeypatch, tmp_path):
    """_apply_local_cert refuses by default (cs client API is plaintext-only on
    the isolated sim segment — see control_plane._local_cert_allowed); an
    operator who explicitly wants TLS on the local webui sets
    LM_CS_ALLOW_LOCAL_CERT=1, which is what this test exercises."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("LM_CS_ALLOW_LOCAL_CERT", "1")
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    cp._api_app = object()  # dummy; rebind is mocked so it never serves
    rebound = []
    agent_rebound = []

    async def _fake_rebind(ssl_certfile=None, ssl_keyfile=None):
        rebound.append((ssl_certfile, ssl_keyfile))
    cp._rebind_api_server = _fake_rebind  # type: ignore[assignment]

    async def _fake_agent_rebind():
        agent_rebound.append(True)
    cp._rebind_agent_server = _fake_agent_rebind  # type: ignore[assignment]

    fullchain, privkey = _real_cert_pair()
    res = _run(cp._apply_local_cert(fullchain, privkey))
    assert res["status"] == "SUCCESS"
    cert = tmp_path / "tls" / "fullchain.pem"
    key = tmp_path / "tls" / "privkey.pem"
    assert cert.read_text() == fullchain
    assert key.read_bytes() == privkey.encode()
    # Re-bind was called with the written paths (HTTPS on the 8080 server).
    assert rebound == [(str(cert), str(key))]
    # The /ws/agent listener (443) is also re-bound so it serves the new cert.
    assert agent_rebound == [True]
    # _local_tls_paths now sees the persisted cert (run() would bind HTTPS).
    assert cp._local_tls_paths() == (str(cert), str(key))


def test_apply_local_cert_agent_rebind_failure_does_not_mask_success(monkeypatch, tmp_path):
    """The 443 agent-listener rebind is best-effort: if it fails, a successful
    8080 webui apply still reports SUCCESS (the webui cert is the primary target;
    the agent leg retries on next renew/re-distribute)."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("LM_CS_ALLOW_LOCAL_CERT", "1")
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    cp._api_app = object()

    async def _fake_rebind(ssl_certfile=None, ssl_keyfile=None):
        pass
    cp._rebind_api_server = _fake_rebind  # type: ignore[assignment]

    async def _boom():
        raise RuntimeError("agent listener port busy")
    cp._rebind_agent_server = _boom  # type: ignore[assignment]

    fullchain, privkey = _real_cert_pair()
    res = _run(cp._apply_local_cert(fullchain, privkey))
    assert res["status"] == "SUCCESS"
    # Files still written even though the agent rebind raised.
    assert (tmp_path / "tls" / "fullchain.pem").read_text() == fullchain


# ── _agent_listener_tls_paths (the 443 listener serves the applied LE cert) ──

def test_agent_listener_tls_paths_prefers_local_le_cert(monkeypatch, tmp_path):
    """After _apply_local_cert persists the LE cert, the /ws/agent listener must
    serve THAT cert (not the installer-provisioned LM_TLS_* env) so the
    agent→spoke leg verifies against LE with LM_HUB_TLS_VERIFY=1."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("LM_TLS_CERT", "/etc/lm/legacy/fullchain.pem")
    monkeypatch.setenv("LM_TLS_KEY", "/etc/lm/legacy/privkey.pem")
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    # No persisted LE cert yet → falls back to env.
    assert cp._agent_listener_tls_paths() == \
        ("/etc/lm/legacy/fullchain.pem", "/etc/lm/legacy/privkey.pem")
    # Persist an LE cert (the apply writes these files).
    d = tmp_path / "tls"; d.mkdir(parents=True, exist_ok=True)
    (d / "fullchain.pem").write_text("LE-CHAIN")
    (d / "privkey.pem").write_bytes(b"LE-KEY")
    cert, key = cp._agent_listener_tls_paths()
    assert cert == str(d / "fullchain.pem")
    assert key == str(d / "privkey.pem")


def test_agent_listener_tls_paths_falls_back_to_env_when_no_local_cert(monkeypatch, tmp_path):
    """A cert-less / pre-INSTALL_CERT cs spoke falls back to the installer env
    (may be empty → run_agent_server serves plaintext, the legacy/cert-less mode)."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))  # dir absent
    monkeypatch.setenv("LM_TLS_CERT", "/etc/lm/env/fullchain.pem")
    monkeypatch.setenv("LM_TLS_KEY", "/etc/lm/env/privkey.pem")
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    assert cp._agent_listener_tls_paths() == \
        ("/etc/lm/env/fullchain.pem", "/etc/lm/env/privkey.pem")


def test_agent_listener_tls_paths_empty_when_neither_local_nor_env(monkeypatch, tmp_path):
    """No persisted LE cert AND no LM_TLS_* env → ('', '') → run_agent_server
    serves plaintext (the cert-less standalone fallback)."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))  # dir absent
    monkeypatch.delenv("LM_TLS_CERT", raising=False)
    monkeypatch.delenv("LM_TLS_KEY", raising=False)
    cp = CSControlPlane("test-cs", "secret", api_host="127.0.0.1", api_port=0)
    assert cp._agent_listener_tls_paths() == ("", "")


def test_apply_local_cert_rejects_invalid_material(monkeypatch, tmp_path):
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.setenv("LM_CS_ALLOW_LOCAL_CERT", "1")
    cp = CSControlPlane("test-cs", "secret")
    wrote = []
    cp._atomic_write_text = lambda p, t: wrote.append(("text", p))  # type: ignore
    cp._atomic_write_bytes = lambda p, d: wrote.append(("bytes", p))  # type: ignore
    res = _run(cp._apply_local_cert("not a cert", "not a key"))
    assert res["status"] == "ERROR"
    assert "invalid cert material" in res["message"]
    # Nothing was written (validation fails fast, no brick).
    assert wrote == []


def test_apply_local_cert_missing_material_is_error(monkeypatch):
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_ALLOW_LOCAL_CERT", "1")
    cp = CSControlPlane("test-cs", "secret")
    res = _run(cp._apply_local_cert("", "key"))
    assert res["status"] == "ERROR"
    assert "missing cert material" in res["message"]


def test_apply_local_cert_refused_when_not_allowed(monkeypatch, tmp_path):
    """Default posture: the cs client API is plaintext-only on the isolated
    sim segment (agents hard-code http://169.253.1.1:8080), so an INSTALL_CERT
    delivered here is a no-op SUCCESS unless LM_CS_ALLOW_LOCAL_CERT=1 — see
    control_plane._apply_local_cert / _local_cert_allowed."""
    from control_plane import CSControlPlane
    _ensure_loop()
    monkeypatch.setenv("LM_CS_TLS_DIR", str(tmp_path / "tls"))
    monkeypatch.delenv("LM_CS_ALLOW_LOCAL_CERT", raising=False)
    cp = CSControlPlane("test-cs", "secret")
    res = _run(cp._apply_local_cert(_PEM, _KEY))
    assert res["status"] == "SUCCESS"
    assert "plaintext-only" in res["message"]
    # Nothing was written — the cert was refused, not silently applied.
    assert not (tmp_path / "tls" / "fullchain.pem").exists()