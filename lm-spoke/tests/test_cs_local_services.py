"""Tests for CSSpoke's PROCESS-scoped local-service startup hooks
(``start_background_loops`` / ``start_client_api_server`` /
``stop_client_api_server``).

These hooks are what let a generic-agent ROLE-HOSTED simulation spoke run the
same always-on services the standalone ``CSControlPlane.run`` starts — most
critically the 8080 client check-in API. Without them a role-hosted cs spoke's
sim VMs get DHCP but have nowhere to POST /api/status, so every one shows "never
checked in". The server bind must honour ``CS_API_HOST``/``CS_API_PORT`` (the
same env the installer uses) and be idempotent.
"""
import asyncio

from cs_spoke import CSSpoke


def test_hooks_exist():
    for name in ("start_background_loops", "start_client_api_server",
                 "stop_client_api_server"):
        assert callable(getattr(CSSpoke, name, None)), f"missing hook {name}"


def test_start_client_api_server_binds_env_host_port(monkeypatch):
    monkeypatch.setenv("CS_API_HOST", "169.253.1.1")
    monkeypatch.setenv("CS_API_PORT", "8080")

    async def _run():
        spoke = CSSpoke("test-cs", {})
        server = spoke.start_client_api_server()
        try:
            assert server.config.host == "169.253.1.1"
            assert server.config.port == 8080
            assert spoke.start_client_api_server() is server
        finally:
            spoke.stop_client_api_server()
            assert server.should_exit is True

    asyncio.run(_run())


def test_start_client_api_server_defaults(monkeypatch):
    monkeypatch.delenv("CS_API_HOST", raising=False)
    monkeypatch.delenv("CS_API_PORT", raising=False)

    async def _run():
        spoke = CSSpoke("test-cs", {})
        server = spoke.start_client_api_server()
        try:
            assert server.config.host == "0.0.0.0"
            assert server.config.port == 8080
        finally:
            spoke.stop_client_api_server()

    asyncio.run(_run())


def test_start_client_api_server_bumps_legacy_8000(monkeypatch):
    monkeypatch.setenv("CS_API_PORT", "8000")

    async def _run():
        spoke = CSSpoke("test-cs", {})
        server = spoke.start_client_api_server()
        try:
            assert server.config.port == 8080
        finally:
            spoke.stop_client_api_server()

    asyncio.run(_run())
