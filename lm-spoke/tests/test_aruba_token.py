"""ArubaClient new_central token choice: client credentials beat a stale
pasted access token (it expires ~2h after it was saved and can't be renewed)."""
import asyncio

import httpx

from aruba import ArubaClient


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
