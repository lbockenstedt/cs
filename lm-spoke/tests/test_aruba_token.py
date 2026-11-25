"""ArubaClient new_central token choice: client credentials beat a stale
pasted access token (it expires ~2h after it was saved and can't be renewed)."""
import asyncio

import httpx
import pytest

from aruba import ArubaClient


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise,
    so these tests never depend on real DNS."""
    _resolve_to(monkeypatch, "8.8.8.8")


def _token(cfg, handler):
    client = ArubaClient(cfg)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await client._ensure_token(http)
    return asyncio.new_event_loop().run_until_complete(go())


def test_client_credentials_beat_a_stale_static_token():
    cfg = {"api_version": "new_central", "client_id": "cid", "client_secret": "sec",
           "access_token": "EXPIRED-STATIC"}
    token = _token(cfg, lambda r: httpx.Response(200, json={"access_token": "FRESH",
                                                            "expires_in": 7200}))
    assert token == "FRESH"


def test_static_token_used_without_client_credentials():
    def handler(request):
        raise AssertionError("no token exchange expected")
    assert _token({"api_version": "new_central", "access_token": "STATIC"}, handler) == "STATIC"


def test_token_url_override_used_for_dev_cluster():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
    cfg = {"api_version": "new_central", "client_id": "cid", "client_secret": "sec",
           "token_url": "https://pavo-sso.common.cloud.hpe.com/as/token.oauth2"}
    assert _token(cfg, handler) == "T"
    assert seen["url"] == "https://pavo-sso.common.cloud.hpe.com/as/token.oauth2"


@pytest.mark.parametrize("url", [
    "https://169.254.169.254", "https://10.0.0.5", "https://localhost:8443",
    "http://eu.api.central.arubanetworks.com", "https://central.lab.local",
])
def test_internal_or_plain_http_cluster_url_is_ignored(url):
    """client_id/client_secret and the bearer token go to cluster_url, so an
    internal or plain-http value is never used (the client is not configured)."""
    client = ArubaClient({"cluster_url": url, "refresh_token": "r"})
    assert client.cluster_url == "" and not client.is_configured()


def test_public_https_cluster_url_is_kept():
    client = ArubaClient({"cluster_url": "https://eu.api.central.arubanetworks.com/"})
    assert client.cluster_url == "https://eu.api.central.arubanetworks.com"
    assert client.is_configured()


@pytest.mark.parametrize("url", ["https://169.254.169.254/token", "https://sso.lab.local/t"])
def test_internal_token_url_override_never_gets_the_secret(url):
    def handler(request):
        raise AssertionError("the client secret must not be sent")
    cfg = {"api_version": "new_central", "client_id": "cid", "client_secret": "sec",
           "token_url": url}
    with pytest.raises(ValueError, match="token_url"):
        _token(cfg, handler)


def _resolve_to(monkeypatch, addr):
    """Make every DNS lookup return addr."""
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, 0, 0, "", (addr, 0))])


def _no_dns(monkeypatch, exc):
    """Make every DNS lookup fail with exc (or return no addresses if None)."""
    import socket

    def fail(*a, **k):
        if exc is None:
            return []
        raise exc
    monkeypatch.setattr(socket, "getaddrinfo", fail)


@pytest.mark.parametrize("cfg", [
    {"cluster_url": "https://rebind.example.com", "access_token": "STATIC"},
    {"api_version": "new_central", "cluster_url": "https://eu.api.central.arubanetworks.com",
     "client_id": "cid", "client_secret": "sec", "token_url": "https://sso.rebind.example.com/t"},
])
def test_host_resolving_internal_never_gets_credentials(monkeypatch, cfg):
    """A public-looking cluster_url or token_url that resolves to an internal
    address passes the string check, but no token or secret is sent to it."""
    _resolve_to(monkeypatch, "169.254.169.254")

    def handler(request):
        raise AssertionError("no request may be sent")
    with pytest.raises(ValueError, match="internal address"):
        _token(cfg, handler)


def test_host_resolving_public_is_used(monkeypatch):
    _resolve_to(monkeypatch, "8.8.8.8")
    cfg = {"cluster_url": "https://eu.api.central.arubanetworks.com", "access_token": "STATIC"}
    assert _token(cfg, lambda r: httpx.Response(500)) == "STATIC"


@pytest.mark.parametrize("addr", [
    "100.64.0.1", "198.18.0.1", "192.0.0.8", "fec0::1",
    "::ffff:10.0.0.5", "2002:a00:5::1", "64:ff9b::a00:5",
])
def test_non_global_addresses_never_get_credentials(monkeypatch, addr):
    """CGNAT, benchmarking, IETF, IPv6 site-local, and IPv6 forms that embed a
    private IPv4 address are all non-public. core's is_internal_ip misses
    some of them, so the spoke refuses anything that is not global when DNS
    answers with it, and a literal one in the URL never gets a request."""
    _resolve_to(monkeypatch, addr)
    host = f"[{addr}]" if ":" in addr else addr

    def handler(request):
        raise AssertionError("no request may be sent")
    with pytest.raises(ValueError, match="internal address"):
        _token({"cluster_url": "https://rebind.example.com", "access_token": "STATIC"}, handler)
    # A literal is refused either when the client is built or before sending.
    literal = {"cluster_url": f"https://{host}", "access_token": "STATIC"}
    if ArubaClient(literal).is_configured():
        with pytest.raises(ValueError, match="internal address"):
            _token(literal, handler)


@pytest.mark.parametrize("exc", [None, OSError("NXDOMAIN"), __import__("socket").gaierror(-2, "x")])
@pytest.mark.parametrize("cfg", [
    {"cluster_url": "https://eu.api.central.arubanetworks.com", "access_token": "STATIC"},
    {"api_version": "new_central", "cluster_url": "https://eu.api.central.arubanetworks.com",
     "client_id": "cid", "client_secret": "sec"},
])
def test_unresolvable_host_fails_closed(monkeypatch, cfg, exc):
    """A host that does not resolve is refused as its own state, not treated
    as public: no token or secret is sent and nothing is returned."""
    _no_dns(monkeypatch, exc)

    def handler(request):
        raise AssertionError("no request may be sent")
    with pytest.raises(ValueError, match="could not be resolved"):
        _token(cfg, handler)


def test_every_call_resolves_again(monkeypatch):
    """A host that was public on one call and internal on the next is refused
    on the second call: an earlier answer is never reused as proof."""
    cfg = {"cluster_url": "https://eu.api.central.arubanetworks.com", "access_token": "STATIC"}
    client = ArubaClient(cfg)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            first = await client._ensure_token(http)
            _resolve_to(monkeypatch, "10.0.0.5")
            with pytest.raises(ValueError, match="internal address"):
                await client._ensure_token(http)
            return first
    assert asyncio.new_event_loop().run_until_complete(go()) == "STATIC"
