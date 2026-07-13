"""ingest_telemetry threads the pxmx agent's ``reclone_state`` (Fleet Reclone
batch progress) into the per-host entry, and relay_payload emits it per-host
(``proxmox_hosts[].reclone_state``) plus top-level from the freshest host — so
the hub's Fleet Reclone progress bar (modeled on the first-version
``renderRecloneStatus``) advances live. Empty/missing → ``{}`` (idle), never
KeyError.
"""
import proxmox_deploy


def _body(reclone_state=None, **over):
    body = {
        "node": {"hostname": "h1", "status": "online"},
        "vms": [],
        "present_usb": [],
        "unknown_usb": [],
        "usb_state": [],
        "provision": {},
    }
    if reclone_state is not None:
        body["reclone_state"] = reclone_state
    body.update(over)
    return body


def test_ingest_telemetry_stores_reclone_state():
    rs = {"status": "running", "current_vm": 90014, "phase": "cloning",
          "total": 9, "completed": 3, "failed": 0, "started_at": 1.0,
          "type": "manual", "log": []}
    d = proxmox_deploy.ProxmoxDeploy()
    entry = d.ingest_telemetry("h1", _body(rs))
    assert entry["reclone_state"]["status"] == "running"
    assert entry["reclone_state"]["total"] == 9


def test_ingest_telemetry_defaults_missing_reclone_state_to_empty():
    # Older agent build omits reclone_state → {} (idle), never KeyError.
    d = proxmox_deploy.ProxmoxDeploy()
    entry = d.ingest_telemetry("h1", _body())
    assert entry["reclone_state"] == {}


def test_relay_payload_emits_per_host_reclone_state():
    rs = {"status": "running", "current_vm": 90014, "total": 9,
          "completed": 3, "failed": 0, "log": []}
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body(rs))
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    hosts = payload["proxmox_hosts"]
    assert hosts and hosts[0]["reclone_state"]["status"] == "running"
    assert hosts[0]["reclone_state"]["total"] == 9


def test_relay_payload_top_level_from_freshest_host():
    # Two hosts: h2 is freshest (later last_seen) → top-level reclone_state = h2's.
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body({"status": "running", "total": 5}))
    # h2 ingested second → freshest.
    d.ingest_telemetry("h2", _body({"status": "completed", "total": 9}))
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    assert payload["reclone_state"]["status"] == "completed"
    assert payload["reclone_state"]["total"] == 9
    # Per-host still carries each host's own state.
    by_hn = {h["hostname"]: h["reclone_state"] for h in payload["proxmox_hosts"]}
    assert by_hn["h1"]["total"] == 5
    assert by_hn["h2"]["total"] == 9


def test_relay_payload_idle_when_no_state():
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body())  # no reclone_state
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    assert payload["reclone_state"] == {}
    assert payload["proxmox_hosts"][0]["reclone_state"] == {}