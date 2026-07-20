"""Tests for the cs spoke client API surface (``lm-spoke/src/client_api.py``).

Drives ``build_client_api_app`` with a real ``CSSpoke`` whose runtime state
(registry / queue / settings) is isolated in a per-test tmp dir, while the
engine keeps the real repo-root ``configs/`` so ``/api/config`` serves the
canon. Uses FastAPI's ``TestClient`` for HTTP + the WebSocket flow.
"""

from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import sim_config
from client_api import build_client_api_app, resolve_script_path
from client_registry import ClientRegistry
from command_queue import CommandQueue, CSSettings
from cs_spoke import CSSpoke

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def spoke(tmp_path) -> CSSpoke:
    s = CSSpoke("test-cs", {})
    data = tmp_path / "data"
    data.mkdir()
    # Isolate runtime state (registry/queue/settings) in tmp; keep the engine on
    # the real repo-root configs so /api/config serves the canon simulation.conf.
    s.settings = CSSettings(data, CONFIGS)
    s.registry = ClientRegistry(data)
    s.queue = CommandQueue(data, s.settings)
    return s


@pytest.fixture
def client(spoke) -> TestClient:
    return TestClient(build_client_api_app(spoke))


# ── health / kill switch ─────────────────────────────────────────────────────
def test_health(client, spoke):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == spoke.get_version()
    assert body["clients"] == 0


def test_kill_switch_off_then_on(client, spoke, monkeypatch):
    monkeypatch.setattr(spoke.engine, "kill_switch_active", lambda: False)
    assert client.get("/api/kill-switch").text == "off"
    monkeypatch.setattr(spoke.engine, "kill_switch_active", lambda: True)
    assert client.get("/api/kill-switch").text == "on"


# ── status beacon + registry ─────────────────────────────────────────────────
def test_status_upserts_registry(client):
    r = client.post("/api/status", json={
        "hostname": "sim-1", "platform": "linux", "iteration": 3,
        "connected_ssid": "corp", "gateway_reachable": True,
        "ip": "10.0.0.42",
        "active_simulations": ["www_traffic"], "errors": ["boom"],
    })
    assert r.status_code == 200
    assert r.json()["client"] == "sim-1"

    clients = client.get("/api/clients").json()
    assert "sim-1" in clients
    assert clients["sim-1"]["connected_ssid"] == "corp"
    assert clients["sim-1"]["ip"] == "10.0.0.42"
    assert clients["sim-1"]["gateway_reachable"] is True
    assert clients["sim-1"]["recent_errors"] == ["boom"]
    assert clients["sim-1"]["active_simulations"] == ["www_traffic"]


def test_client_key_default_empty(client):
    r = client.get("/api/client/key")
    assert r.status_code == 200
    assert r.json() == {"client_api_key": ""}


def test_apersist_round_trips_to_disk(tmp_path):
    """_apersist is now a DEBOUNCED dirty-mark (coalesced flush, ≤1 write per
    ~5s); the actual write still runs json.dumps + write in a worker thread.
    Verify that after an explicit flush (aclose — the orderly-shutdown path)
    valid JSON lands on disk that a fresh ClientRegistry loads back — the
    debounce must not change the on-disk contract."""
    import asyncio
    # Earlier tests (test_kill_switch) call asyncio.set_event_loop(None), which
    # on Py3.9 poisons the process so the TestClient-based tests later in this
    # file can't get a loop. Save/restore like test_sim_config_merge's
    # CS_GET_CONFIG test: leave a usable loop behind no matter what.
    try:
        prev_loop = asyncio.get_event_loop()
        if prev_loop.is_closed():
            prev_loop = None
    except RuntimeError:
        prev_loop = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = tmp_path / "data"
        data.mkdir()
        reg = ClientRegistry(data)
        loop.run_until_complete(reg.apply_status("sim-1", {
            "hostname": "sim-1", "platform": "linux", "iteration": 7,
            "connected_ssid": "corp", "gateway_reachable": True,
            "active_simulations": ["www_traffic"], "errors": ["boom"]}))
        # Debounced: flush explicitly (shutdown path) instead of waiting ~5s.
        loop.run_until_complete(reg.aclose())
        # The persist file exists and round-trips through a fresh instance.
        assert (data / "clients.json").exists()
        reloaded = ClientRegistry(data)
        assert "sim-1" in reloaded.clients
        assert reloaded.clients["sim-1"]["connected_ssid"] == "corp"
        assert reloaded.clients["sim-1"]["recent_errors"] == ["boom"]
    finally:
        if prev_loop is not None:
            asyncio.set_event_loop(prev_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())
        loop.close()


# ── relay row (the spoke→hub CS_TELEMETRY choke point) ────────────────────────
def test_build_client_rows_relays_ip_and_gateway(spoke):
    """``build_client_rows`` is the choke point for what the hub sees: it must
    carry ``ip`` + ``gateway_reachable`` (previously ``gateway_reachable`` was
    persisted in the registry but dropped here, so the hub never saw it). The
    dongle-quarantine trigger relies on these reaching the hub."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(spoke.registry.apply_status("sim-1", {
            "hostname": "sim-1", "platform": "linux", "iteration": 1,
            "connected_ssid": "corp", "gateway_reachable": True, "ip": "10.0.0.42",
        }))
        from client_rows import build_client_rows
        rows, _ = build_client_rows(spoke)
        row = next(r for r in rows if r["hostname"] == "sim-1")
        assert row["ip"] == "10.0.0.42"
        assert row["gateway_reachable"] is True
        assert row["connected_ssid"] == "corp"
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_client_rows_no_ip_is_empty_not_missing(spoke):
    """A client that never got an IP reports none — the row must surface ``""``
    (falsy) so the trigger can detect 'never got an IP', not a missing key."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(spoke.registry.apply_status("noip-1", {
            "hostname": "noip-1", "platform": "linux", "iteration": 1,
            "connected_ssid": "", "gateway_reachable": False,
        }))
        from client_rows import build_client_rows
        rows, _ = build_client_rows(spoke)
        row = next(r for r in rows if r["hostname"] == "noip-1")
        assert row["ip"] == ""
        assert row["gateway_reachable"] is False
        assert row["connected_ssid"] == "—"
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── config delivery ──────────────────────────────────────────────────────────
def test_config_renders_host_bucket(client):
    hostname = "sim-host-9"
    r = client.get("/api/config", params={"hostname": hostname})
    assert r.status_code == 200
    text = r.text
    assert "[simulation]" in text
    bucket = sim_config.bucket_for(hostname)  # e.g. "s3"
    assert f"[{bucket}]" in text


def _ambient_pct(text):
    import re
    m = re.search(r"(?m)^ambient_pct\s*=\s*(\S+)", text)
    return m.group(1) if m else None


def test_exclusive_sim_suppresses_ambient(client, spoke):
    """multi_capable contract: an EXCLUSIVE sim (multi_capable=False, e.g.
    dhcp_fail) monopolizes its client — no gateway, so no other sim can run.
    The spoke must suppress ambient_pct for THAT client (serve-time, driven by
    SIM_META.multi_capable) so the client-side ambient pick does not stack a
    shareable traffic sim onto it. A SHAREABLE sim (iperf) does NOT suppress
    ambient — it may stack."""
    import re
    spoke.local_store.set_ambient_pct(50)
    spoke.local_store.set_ambient_control(False)
    h = "sim-1"
    # Baseline: no exclusive sim → ambient_pct is the served base (50).
    assert _ambient_pct(client.get("/api/config", params={"hostname": h}).text) == "50"
    # Assign an EXCLUSIVE sim via the registry override (the engine's path).
    client.post(f"/api/clients/{h}/control", json={"overrides": {"dhcp_fail": "on"}})
    assert _ambient_pct(client.get("/api/config", params={"hostname": h}).text) == "0"
    # A SHAREABLE sim does not suppress ambient — it may stack.
    client.delete(f"/api/clients/{h}/control")
    client.post(f"/api/clients/{h}/control", json={"overrides": {"iperf": "on"}})
    assert _ambient_pct(client.get("/api/config", params={"hostname": h}).text) == "50"


def test_config_overrides_and_parsed(client):
    assert client.get("/api/config/overrides").status_code == 200
    parsed = client.get("/api/config/parsed").json()
    assert "simulation" in parsed
    assert "repo_location" in parsed["simulation"]


# ── scripts ──────────────────────────────────────────────────────────────────
def test_scripts_list_and_get(client):
    r = client.get("/api/scripts/list", params={"platform": "linux"})
    assert r.status_code == 200
    assert "agent.sh" in r.json()  # bare array (linux/windows clients iterate it directly)

    r = client.get("/api/scripts/linux/agent.sh")
    assert r.status_code == 200
    assert "hostname" in r.text  # agent.sh builds the WS url from hostname


def test_resolve_script_path_traversal_guard():
    # Happy path resolves to a real file inside the platform dir.
    p = resolve_script_path("linux", "agent.sh")
    assert p.is_file() and p.name == "agent.sh"

    # Escape attempt + missing file both 404.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        resolve_script_path("linux", "../../configs/simulation.conf")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        resolve_script_path("linux", "does-not-exist.sh")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        resolve_script_path("plan9", "agent.sh")  # unknown platform
    assert exc.value.status_code == 404


# ── command queue (HTTP) ─────────────────────────────────────────────────────
def test_commands_enqueue_and_list(client):
    # Non-proxmox target, non-VM action → accepted (no safeguard).
    r = client.post("/api/commands", json={
        "target": "sim-host-1", "action": "reboot", "args": {}, "type": "client",
    })
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True

    listed = client.get("/api/commands").json()["commands"]
    assert any(c["action"] == "reboot" for c in listed)


def test_commands_safeguard_refuse_low_vmid(client):
    # proxmox VM action with vmid < SIM_VMIN (90000) → safeguard refuse → 400.
    r = client.post("/api/commands", json={
        "target": "proxmox", "action": "start_vm", "args": {"vmid": 100},
    })
    assert r.status_code == 400
    assert "90000" in r.text or "Client-Simulation range" in r.text


def test_commands_safeguard_accept_sim_vmid(client):
    r = client.post("/api/commands", json={
        "target": "proxmox", "action": "start_vm", "args": {"vmid": 91000},
    })
    assert r.status_code == 200, r.text


# ── client control (overrides) ───────────────────────────────────────────────
def test_client_control_overrides(client):
    r = client.post("/api/clients/sim-1/control", json={"overrides": {"sim_load": "0"}})
    assert r.status_code == 200
    assert client.get("/api/clients").json()["sim-1"]["overrides"] == {"sim_load": "0"}

    assert client.delete("/api/clients/sim-1/control").status_code == 200
    assert "overrides" not in client.get("/api/clients").json().get("sim-1", {})


# ── WebSocket ────────────────────────────────────────────────────────────────
def test_ws_hello_status_ping(client):
    with client.websocket_connect("/ws/client?hostname=ws-1&platform=linux") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "status", "payload": {"hostname": "ws-1", "iteration": 1}})
        assert ws.receive_json()["type"] == "status_ack"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_ws_pushes_pending_command_and_acks(client):
    # Enqueue a command targeting the host BEFORE it connects, so the
    # connect-time push delivers it immediately.
    pr = client.post("/api/commands", json={
        "target": "ws-2", "action": "reboot", "args": {}, "type": "client",
    })
    assert pr.status_code == 200 and pr.json()["created"] is True

    with client.websocket_connect("/ws/client?hostname=ws-2") as ws:
        assert ws.receive_json()["type"] == "hello"
        frame = ws.receive_json()
        assert frame["type"] == "commands"
        assert len(frame["commands"]) == 1
        cmd = frame["commands"][0]
        assert cmd["action"] == "reboot"

        # Ack it → ack_ok.
        ws.send_json({"type": "ack", "payload": {
            "id": cmd["id"], "status": "completed", "message": "ok"}})
        ack = ws.receive_json()
        assert ack["type"] == "ack_ok" and ack["ok"] is True

        # A command queued WHILE connected is live-pushed (push_pending from the
        # POST handler) without waiting for the agent's next sync.
        client.post("/api/commands", json={
            "target": "ws-2", "action": "snapshot_vm", "args": {}, "type": "client",
        })
        frame2 = ws.receive_json()
        assert frame2["type"] == "commands"
        assert len(frame2["commands"]) == 1
        assert frame2["commands"][0]["action"] == "snapshot_vm"

        # sync is fire-and-forget (no frame when nothing pending) — verify the
        # loop is still alive afterwards with a ping/pong round-trip.
        ws.send_json({"type": "sync"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_ws_rejects_bad_key(client, spoke):
    spoke.settings.update({"client_api_key": "secret"})
    with pytest.raises(Exception):  # WebSocketDisconnect / close 4403
        with client.websocket_connect("/ws/client?hostname=nope") as ws:
            ws.receive_json()


# ── shared-key gating on HTTP ────────────────────────────────────────────────
def test_http_key_gating(client, spoke):
    spoke.settings.update({"client_api_key": "secret"})

    # Public routes still work without a key.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/client/key").json()["client_api_key"] == "secret"

    # Gated route without a key → 401.
    assert client.get("/api/commands").status_code == 401

    # Gated route with the right header → 200.
    r = client.get("/api/commands", headers={"X-Client-Key": "secret"})
    assert r.status_code == 200