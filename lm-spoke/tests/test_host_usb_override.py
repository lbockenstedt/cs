"""Per-host USB VMID overrides — the cs-spoke side of per-host batch assignment.

The pxmx agent derives each proxmox host's sim-VMID block from its hostname
suffix (svr-02→90025-90048) by default. The cs speak can OVERRIDE that for one
host via a per-host ``vmid_start``/``vmid_end``/``vm_set_override`` pin: the
overlay is applied in ``CSSettings.usb_config_payload(hostname)`` (the only
place hostname matters — cs_bridge already relays per-agent) and the agent
honors a non-default range over its own derivation. Covers: default = global,
overlay for one host only, the 3 CS_GET/SET/CLEAR_HOST_USB_OVERRIDE commands,
and a persist→reload round-trip.
"""

import asyncio
from pathlib import Path

import pytest

from command_queue import CSSettings
from cs_spoke import CSSpoke

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    # CSSpoke builds its own settings/registry/queue in __init__; rebind to the
    # tmp-dir-backed ones so persistence is isolated to the tmp dir.
    from client_registry import ClientRegistry
    from command_queue import CommandQueue
    s.registry = ClientRegistry(data_dir)
    s.queue = CommandQueue(data_dir, s.settings)
    return s, loop


def _run(loop, coro):
    return loop.run_until_complete(coro)


@pytest.fixture
def spoke_loop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    s, loop = _make_spoke(data, CONFIGS)
    try:
        yield s, loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _usb(spoke, loop, hostname):
    return _run(loop, spoke.queue.get_usb_config(hostname))


def test_no_override_returns_global_defaults(spoke_loop):
    spoke, loop = spoke_loop
    cfg = _usb(spoke, loop, "pxmx-cs-svr-02")
    assert cfg["vmid_start"] == 90000
    assert cfg["vmid_end"] == 99999
    assert cfg["vm_set_override"] == 0


def test_per_host_override_overlays_only_that_host(spoke_loop):
    spoke, loop = spoke_loop
    spoke.settings.set_host_usb_override(
        "pxmx-cs-svr-02", {"vmid_start": 91000, "vmid_end": 91999, "vm_set_override": 0})

    hit = _usb(spoke, loop, "pxmx-cs-svr-02")
    assert hit["vmid_start"] == 91000
    assert hit["vmid_end"] == 91999

    # Other hosts are unaffected — still the global default.
    other = _usb(spoke, loop, "pxmx-cs-svr-01")
    assert other["vmid_start"] == 90000
    assert other["vmid_end"] == 99999


def test_partial_override_only_sets_provided_knobs(spoke_loop):
    spoke, loop = spoke_loop
    spoke.settings.set_host_usb_override(
        "pxmx-cs-svr-03", {"vm_set_override": 5})
    hit = _usb(spoke, loop, "pxmx-cs-svr-03")
    assert hit["vm_set_override"] == 5
    # vmid_start/vmid_end stay at the global default (override is partial).
    assert hit["vmid_start"] == 90000
    assert hit["vmid_end"] == 99999


def test_set_host_usb_override_command_handler(spoke_loop):
    spoke, loop = spoke_loop
    res = _run(loop, spoke.handle_command(
        "CS_SET_HOST_USB_OVERRIDE",
        {"hostname": "pxmx-cs-svr-02", "knobs": {"vmid_start": 91000, "vmid_end": 91999}}))
    assert res["status"] == "SUCCESS"
    assert res["hostname"] == "pxmx-cs-svr-02"
    assert res["knobs"]["vmid_start"] == 91000
    assert res["knobs"]["vmid_end"] == 91999
    # Reflected in the payload.
    assert _usb(spoke, loop, "pxmx-cs-svr-02")["vmid_start"] == 91000


def test_set_host_usb_override_missing_hostname(spoke_loop):
    spoke, loop = spoke_loop
    res = _run(loop, spoke.handle_command("CS_SET_HOST_USB_OVERRIDE", {"knobs": {}}))
    assert res["status"] == "ERROR"


def test_get_and_clear_host_usb_override_commands(spoke_loop):
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_SET_HOST_USB_OVERRIDE",
        {"hostname": "pxmx-cs-svr-02", "knobs": {"vmid_start": 91000, "vmid_end": 91999}}))

    got = _run(loop, spoke.handle_command("CS_GET_HOST_USB_OVERRIDES", {}))
    assert got["status"] == "SUCCESS"
    assert "pxmx-cs-svr-02" in got["overrides"]
    assert got["overrides"]["pxmx-cs-svr-02"]["vmid_start"] == 91000

    cleared = _run(loop, spoke.handle_command(
        "CS_CLEAR_HOST_USB_OVERRIDE", {"hostname": "pxmx-cs-svr-02"}))
    assert cleared["status"] == "SUCCESS"
    assert cleared["cleared"] is True
    # Back to the global default after clear.
    assert _usb(spoke, loop, "pxmx-cs-svr-02")["vmid_start"] == 90000

    # Clearing a non-existent host reports cleared=False but still SUCCESS.
    again = _run(loop, spoke.handle_command(
        "CS_CLEAR_HOST_USB_OVERRIDE", {"hostname": "ghost"}))
    assert again["status"] == "SUCCESS"
    assert again["cleared"] is False


def test_host_usb_override_persists_across_reload(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    s1 = CSSettings(data, CONFIGS)
    s1.set_host_usb_override("pxmx-cs-svr-02", {"vmid_start": 91000, "vmid_end": 91999})

    # A fresh settings instance pointing at the same dir rehydrates the override.
    s2 = CSSettings(data, CONFIGS)
    assert s2.host_usb_override("pxmx-cs-svr-02")["vmid_start"] == 91000
    assert s2.all_host_usb_overrides()["pxmx-cs-svr-02"]["vmid_end"] == 91999