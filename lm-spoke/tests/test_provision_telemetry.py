"""ingest_telemetry threads the pxmx agent's ``provision`` diagnostic into the
per-host summary so the hub cache → ``/usb-provisioning-status`` → WebUI
Auto-Provisioning card can show WHY nothing provisions (mirrors the existing
``present_usb`` threading via ``_SUMMARY_KEYS``). Without this, a silent gate
(no dongle_vidpids / no template ids) is invisible in the UI.
"""

import proxmox_deploy


def _body(**over):
    body = {
        "node": {"hostname": "h1", "status": "online"},
        "vms": [],
        "present_usb": [],
        "unknown_usb": [],
        "usb_state": [],
        "provision": {
            "cs_enabled": True,
            "loop_running": True,
            "auto_provision_on": True,
            "reason": "no dongle_vidpids configured",
            "halt": None,
            "config": {"dongle_vidpids": 0, "image1_template_id": False,
                       "image2_template_id": False, "max_slots": 24,
                       "vmid_range": {"start": 90000, "end": 99999},
                       "active_usb_vms": None},
        },
    }
    body.update(over)
    return body


def test_ingest_telemetry_stores_provision_block():
    d = proxmox_deploy.ProxmoxDeploy()
    entry = d.ingest_telemetry("h1", _body())
    assert entry["provision"]["reason"] == "no dongle_vidpids configured"
    assert entry["provision"]["cs_enabled"] is True
    assert entry["provision"]["config"]["dongle_vidpids"] == 0


def test_provision_projected_into_host_summary_and_relay():
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body())
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    # Top-level primary ``proxmox`` block carries provision (via _SUMMARY_KEYS).
    assert payload["proxmox"]["provision"]["reason"] == "no dongle_vidpids configured"
    # And the per-host ``proxmox_hosts`` entry carries it too (one row per host).
    hosts = payload["proxmox_hosts"]
    assert hosts and hosts[0]["proxmox"]["provision"]["cs_enabled"] is True


def test_ingest_telemetry_defaults_missing_provision_to_empty():
    d = proxmox_deploy.ProxmoxDeploy()
    # Agent omits the provision block entirely (e.g. older agent build) — ingest
    # must default to {} and never KeyError.
    entry = d.ingest_telemetry("h1", {"node": {"hostname": "h1"}, "vms": []})
    assert entry["provision"] == {}


def test_prov_run_relayed_and_per_vm_status_stamped():
    d = proxmox_deploy.ProxmoxDeploy()
    body = _body(
        vms=[
            {"vmid": 90014, "name": "amoran-90014", "status": "running"},   # provisioning
            {"vmid": 90020, "name": "bsmith-90020", "status": "running"},   # deleting
            {"vmid": 90021, "name": "idle-90021", "status": "stopped"},     # neither
        ],
        prov_run={"running": True, "total": 2, "completed": 0, "failed": 0,
                  "items": [{"vmid": 90014, "status": "provisioning"},
                            {"vmid": 90099, "status": "done"}]},
        deleting_vmids=[90020],
    )
    entry = d.ingest_telemetry("h1", body)
    by_vmid = {v["vmid"]: v for v in entry["vms"]}
    # prov_run item with status 'provisioning' → 🔵 provisioning on that VM
    assert by_vmid[90014]["prov_status"] == "provisioning"
    # deleting_vmids wins → 🔴 tearing_down
    assert by_vmid[90020]["prov_status"] == "tearing_down"
    # untouched VM keeps the default empty prov_status
    assert by_vmid[90021]["prov_status"] == ""
    # prov_run relays for the live panel + rides the per-host summary
    assert entry["prov_run"]["total"] == 2
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    assert payload["proxmox"]["prov_run"]["running"] is True


def test_deleting_wins_over_provisioning():
    d = proxmox_deploy.ProxmoxDeploy()
    body = _body(
        vms=[{"vmid": 90014, "name": "amoran-90014", "status": "running"}],
        prov_run={"running": True, "items": [{"vmid": 90014, "status": "provisioning"}]},
        deleting_vmids=[90014],
    )
    entry = d.ingest_telemetry("h1", body)
    assert entry["vms"][0]["prov_status"] == "tearing_down"