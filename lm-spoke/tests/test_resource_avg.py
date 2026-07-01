"""ingest_telemetry records per-host CPU/mem samples and exposes their rolling
1h averages (cpu_1h_avg / mem_1h_avg) through the per-host ``proxmox`` summary
so the VM Server Details header shows live values instead of "—".

Mirrors the legacy webui-spoke ``_record_resource_samples`` /
``_resource_1h_average`` (server.py:3940/3966). The pxmx agent emits
``node.cpu_percent`` / ``node.mem_used_kb`` / ``node.mem_total_kb``
(agent.py:1723-1725); mem is stored as a percentage so hosts with different
RAM compare on the same axis.
"""

import time

import proxmox_deploy


def _body(cpu_percent=0, mem_used_kb=0, mem_total_kb=0, **over):
    body = {
        "node": {"hostname": "h1", "cpu_percent": cpu_percent,
                 "mem_used_kb": mem_used_kb, "mem_total_kb": mem_total_kb},
        "vms": [], "present_usb": [], "unknown_usb": [], "usb_state": [],
    }
    body.update(over)
    return body


def test_ingest_records_cpu_and_mem_samples():
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body(cpu_percent=42.0, mem_used_kb=2 * 1024,
                                  mem_total_kb=8 * 1024))
    assert d.cpu_samples["h1"] and d.cpu_samples["h1"][0][1] == 42.0
    # mem is a percentage: (2GiB / 8GiB) * 100 == 25.0
    assert d.mem_samples["h1"][0][1] == 25.0


def test_cpu_1h_avg_and_mem_1h_avg_land_in_entry_and_summary():
    d = proxmox_deploy.ProxmoxDeploy()
    entry = d.ingest_telemetry(
        "h1", _body(cpu_percent=50.0, mem_used_kb=4 * 1024, mem_total_kb=8 * 1024))
    assert entry["cpu_1h_avg"] == 50.0
    assert entry["mem_1h_avg"] == 50.0
    # Projected through _SUMMARY_KEYS into the relay payload's per-host block.
    payload = d.relay_payload("cs-spoke-1", "CS Spoke 1")
    assert payload["proxmox"]["cpu_1h_avg"] == 50.0
    assert payload["proxmox"]["mem_1h_avg"] == 50.0
    assert payload["proxmox_hosts"][0]["proxmox"]["cpu_1h_avg"] == 50.0


def test_1h_avg_is_rolling_mean_of_multiple_samples():
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body(cpu_percent=20.0))
    d.ingest_telemetry("h1", _body(cpu_percent=40.0))
    d.ingest_telemetry("h1", _body(cpu_percent=60.0))
    entry = d.ingest_telemetry("h1", _body(cpu_percent=80.0))
    # mean of [20, 40, 60, 80] == 50
    assert entry["cpu_1h_avg"] == 50.0


def test_1h_avg_rounded_to_2_decimal_places():
    d = proxmox_deploy.ProxmoxDeploy()
    # mean of [10, 10, 11] == 10.333… → rounds to 10.33, not 10.333333333333334.
    d.ingest_telemetry("h1", _body(cpu_percent=10.0))
    d.ingest_telemetry("h1", _body(cpu_percent=10.0))
    entry = d.ingest_telemetry("h1", _body(cpu_percent=11.0))
    assert entry["cpu_1h_avg"] == 10.33
    # And the relay payload carries the rounded value too.
    assert d.relay_payload("cs-spoke-1")["proxmox"]["cpu_1h_avg"] == 10.33


def test_missing_resource_fields_yield_none_rendered_as_dash():
    d = proxmox_deploy.ProxmoxDeploy()
    # Agent reports a node block with NO resource fields — no sample recorded,
    # averages stay None (the UI renders None as "—"). Must not raise.
    entry = d.ingest_telemetry("h1", {"node": {"hostname": "h1"}, "vms": []})
    assert entry["cpu_1h_avg"] is None
    assert entry["mem_1h_avg"] is None
    payload = d.relay_payload("cs-spoke-1")
    assert payload["proxmox"]["cpu_1h_avg"] is None
    assert payload["proxmox"]["mem_1h_avg"] is None


def test_samples_prune_past_the_1h_window():
    d = proxmox_deploy.ProxmoxDeploy()
    # Seed a stale sample (2h ago) + a fresh one (now); the stale one must drop
    # out of the ring and the average must reflect only the fresh sample.
    now = time.time()
    d.cpu_samples["h1"] = [(now - 4000, 99.0)]
    d.ingest_telemetry("h1", _body(cpu_percent=10.0))
    assert all(ts >= now - proxmox_deploy._RESOURCE_SAMPLE_WINDOW
               for ts, _ in d.cpu_samples["h1"])
    assert d.relay_payload("cs-spoke-1")["proxmox"]["cpu_1h_avg"] == 10.0


def test_per_host_rings_are_independent():
    d = proxmox_deploy.ProxmoxDeploy()
    d.ingest_telemetry("h1", _body(cpu_percent=10.0))
    d.ingest_telemetry("h2", _body(cpu_percent=90.0))
    p = d.relay_payload("cs-spoke-1")
    by_host = {h["hostname"]: h["proxmox"]["cpu_1h_avg"]
               for h in p["proxmox_hosts"]}
    assert by_host["h1"] == 10.0
    assert by_host["h2"] == 90.0