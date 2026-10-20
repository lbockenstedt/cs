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


def _heal_script(tmp_path):
    text = INSTALLER.read_text()
    body = text.split("<<'CSHEAL'\n", 1)[1].split("\nCSHEAL\n", 1)[0]
    script = tmp_path / "lm-cs-unit-heal"
    script.write_text(body + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, rc in (("systemctl", 0), ("logger", 0)):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\necho "$0 $*" >> "{tmp_path}/calls"\nexit {rc}\n')
        stub.chmod(0o755)
    return script, bindir


def _run_heal(tmp_path, unit_text):
    import os
    import subprocess
    script, bindir = _heal_script(tmp_path)
    units = tmp_path / "units"
    units.mkdir()
    (units / "lm-cs.service").write_text(unit_text)
    env = dict(os.environ, LM_CS_UNIT_DIR=str(units),
               PATH=f"{bindir}:{os.environ['PATH']}")
    # The script prepends /usr/bin etc.; put stubs first via a wrapper.
    wrapped = script.read_text().replace(
        'export PATH="/usr/sbin', f'export PATH="{bindir}:/usr/sbin', 1)
    script.write_text(wrapped)
    out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    return out, (units / "lm-cs.service").read_text(), calls


@pytest.mark.parametrize("pin", [
    "--secret=abc123", "--secret abc123", "--secret \"abc 123\"",
    "--hub-secret=xyz --secret=abc",
])
def test_heal_strips_pinned_secret_and_restarts(tmp_path, pin):
    unit = ("[Service]\nUser=svc_lm\n"
            f"ExecStart=/opt/lm/cs/venv/bin/python3 -m src.control_plane --id cs-svr-01 {pin} "
            "--hub \"${HUB_URL}\" --port 8080\n")
    out, healed, calls = _run_heal(tmp_path, unit)
    assert out.returncode == 0, out.stderr
    line = [l for l in healed.splitlines() if l.startswith("ExecStart=")][0]
    assert "secret" not in line and "abc" not in line and "xyz" not in line
    assert line.endswith('--id cs-svr-01 --hub "${HUB_URL}" --port 8080')
    assert "abc" not in out.stdout
    assert "daemon-reload" in calls and "restart lm-cs" in calls


def test_heal_is_noop_on_clean_unit(tmp_path):
    unit = "[Service]\nExecStart=/x/python3 -m src.control_plane --hub \"${HUB_URL}\"\n"
    out, healed, calls = _run_heal(tmp_path, unit)
    assert out.returncode == 0 and healed == unit
    assert "systemctl" not in calls
