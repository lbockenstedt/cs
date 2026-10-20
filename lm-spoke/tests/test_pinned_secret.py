"""A secret frozen into the systemd unit's ExecStart (--secret=...) must not
override the rotated value the spoke persists to .env — otherwise every restart
presents a stale secret, the hub rejects it, and the spoke loops through
zero-touch. Detected at startup, surfaced on /api/health + get_status."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import client_api
import cs_spoke
from client_api import build_client_api_app
from control_plane import resolve_pinned_secret

INSTALLER = Path(__file__).resolve().parents[1] / "install_cs.sh"


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch):
    monkeypatch.setattr(cs_spoke, "PINNED_SECRET_IGNORED", [])
    monkeypatch.delenv("SPOKE_SECRET", raising=False)
    monkeypatch.delenv("HUB_SECRET", raising=False)


def test_env_wins_over_stale_cli_pin(monkeypatch):
    monkeypatch.setenv("SPOKE_SECRET", "rotated")
    assert resolve_pinned_secret("SPOKE_SECRET", "pinned-at-install") == "rotated"
    assert cs_spoke.PINNED_SECRET_IGNORED == ["SPOKE_SECRET"]


def test_env_cleared_after_rejection_falls_back_to_zero_touch(monkeypatch):
    monkeypatch.setenv("SPOKE_SECRET", "")
    assert resolve_pinned_secret("SPOKE_SECRET", "pinned-at-install") == ""
    assert cs_spoke.PINNED_SECRET_IGNORED == ["SPOKE_SECRET"]


def test_matching_values_are_not_flagged(monkeypatch):
    monkeypatch.setenv("HUB_SECRET", "same")
    assert resolve_pinned_secret("HUB_SECRET", "same") == "same"
    assert cs_spoke.PINNED_SECRET_IGNORED == []


def test_cli_used_when_env_absent():
    assert resolve_pinned_secret("SPOKE_SECRET", "explicit") == "explicit"
    assert cs_spoke.PINNED_SECRET_IGNORED == []


def test_flag_surfaces_on_health_and_status(monkeypatch):
    monkeypatch.setenv("SPOKE_SECRET", "rotated")
    resolve_pinned_secret("SPOKE_SECRET", "pinned")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    spoke = cs_spoke.CSSpoke("test-cs", {})
    monkeypatch.setattr(client_api, "_on_sim_segment", lambda host: True)
    body = TestClient(build_client_api_app(spoke)).get("/api/health").json()
    assert body["pinned_secret_ignored"] == ["SPOKE_SECRET"]
    asyncio.set_event_loop(None)
    loop.close()


def test_installer_does_not_pin_secrets_in_execstart():
    lines = [l for l in INSTALLER.read_text().splitlines()
             if l.startswith("ExecStart=") and "src.control_plane" in l]
    assert lines
    for line in lines:
        assert "--secret" not in line and "--hub-secret" not in line
