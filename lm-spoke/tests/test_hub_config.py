"""CS_CONFIG_UPDATE applies hub-pushed provisioning config to the cs settings
store.

The hub pushes hub-owned config (usb_vidpids, usb_ignored_vidpids,
usb_auto_provision, template ids, VLAN ranges, ... + optional sim/user-override
INI text) via ``CS_CONFIG_UPDATE``. The legacy webui-spoke applied these via
``_apply_hub_config``; the new lm-spoke MUST do the same or certification pushes
are silently dropped — ``usb_vidpids`` stays ``"[]"`` in settings, the cs_bridge
pulls an empty ``vidpids`` via ``CS_GET_USB_CONFIG``, the agent's
``_dongle_vidpids`` returns 0, and auto-provision never fires with reason
``"no dongle_vidpids configured"``.
"""

import asyncio
import json
from pathlib import Path

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path):
    """Build a CSSpoke with an isolated tmp data dir + an explicit event loop.

    Python 3.9's ``asyncio.Lock()`` (constructed in ``ClientRegistry.__init__``)
    needs a current event loop; ``asyncio.run()`` closes the loop after each
    call, so we create one loop per spoke and run coros on it via ``_run``.
    Returns ``(spoke, loop)``.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
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


def test_config_update_applies_usb_vidpids_to_settings(spoke_loop):
    """The immediate auto-provision blocker: certifying a dongle must land in
    settings.usb_vidpids so usb_config_payload emits a non-empty ``vidpids``."""
    spoke, loop = spoke_loop
    cert = [{"vidpid": "1a2b:3c4d", "type": "wireless", "label": "1a2b:3c4d"}]
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "usb_vidpids": json.dumps(cert),
        "usb_ignored_vidpids": json.dumps(["dead:beef"]),
    }))
    assert resp["status"] == "SUCCESS"
    assert "usb_vidpids" in resp["applied"]
    # Settings now carry the certified list (as the JSON string the hub sent).
    assert json.loads(spoke.settings.get("usb_vidpids")) == cert
    assert json.loads(spoke.settings.get("usb_ignored_vidpids")) == ["dead:beef"]
    # usb_config_payload — what the cs_bridge pushes to the agent — now has vidpids.
    cfg = spoke.settings.usb_config_payload()
    assert cfg["vidpids"] == cert
    assert "dead:beef" in cfg["ignored_vidpids"]


def test_config_update_remaps_vm_image_template_ids(spoke_loop):
    """The hub UI labels template ids ``vm_image_*``; the settings store + agent
    read ``image*_template_id``. Without the remap the template id never reaches
    the agent even after certification is unblocked."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "vm_image_1_template_id": "100",
        "vm_image_2_template_id": "200",
    }))
    assert resp["status"] == "SUCCESS"
    assert any("vm_image_1_template_id->image1_template_id" in a for a in resp["applied"])
    assert str(spoke.settings.get("image1_template_id")) == "100"
    assert str(spoke.settings.get("image2_template_id")) == "200"
    cfg = spoke.settings.usb_config_payload()
    assert cfg["image1_template_id"] == 100
    assert cfg["image2_template_id"] == 200


def test_config_update_template_name_passes_through_uncoerced(spoke_loop):
    """The template field accepts EITHER a vmid (numeric) OR a template NAME
    (text) — the pxmx agent's ``_resolve_template_vmid`` resolves a name to its
    vmid via ``qm list``. A NAME must survive hub→spoke→usb_config UNCOERCED
    (the old ``int(... or 100)`` would either crash on a name or, with the
    try/except, silently drop it to the default). A numeric value still emits
    an int (back-compat)."""
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "vm_image_1_template_id": "debian-12-template",
        "vm_image_2_template_id": "win11-golden",
    }))
    assert resp["status"] == "SUCCESS"
    # Stored verbatim (the remap path writes the hub value as-is).
    assert spoke.settings.get("image1_template_id") == "debian-12-template"
    assert spoke.settings.get("image2_template_id") == "win11-golden"
    cfg = spoke.settings.usb_config_payload()
    # A name passes through as a str; the agent resolves it.
    assert cfg["image1_template_id"] == "debian-12-template"
    assert cfg["image2_template_id"] == "win11-golden"
    # A numeric value still emits an int (back-compat for int-expecting
    # consumers + the agent's numeric fast-path).
    resp2 = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "vm_image_1_template_id": "100",
    }))
    assert resp2["status"] == "SUCCESS"
    assert spoke.settings.usb_config_payload()["image1_template_id"] == 100


def test_config_update_marks_hub_managed_and_persists(spoke_loop):
    """A hub push marks the spoke hub-managed and persists to cs_settings.json
    so the certified list survives a spoke restart (the bridge re-pushes
    usb_config from the persisted settings on reconnect)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "usb_vidpids": json.dumps([{"vidpid": "1a2b:3c4d", "type": "wireless"}]),
    }))
    assert spoke.settings.get("hub_managed") is True
    # Reload from disk to confirm persistence.
    reloaded = CSSettings(spoke.settings.data_dir, CONFIGS)
    assert json.loads(reloaded.get("usb_vidpids"))
    assert reloaded.get("hub_managed") is True


def test_config_update_writes_sim_conf_override(tmp_path):
    """Optional sim_conf_override INI text is written to
    configs/hub-sim-overrides.conf (None clears it). Uses an isolated tmp
    config_dir so the real repo configs/ is never touched."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    spoke, loop = _make_spoke(data, cfg_dir)
    try:
        resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
            "sim_conf_override": "[simulation]\nsim_phy = ethernet\n",
        }))
        assert resp["status"] == "SUCCESS"
        assert any(a.startswith("sim_conf_override") for a in resp["applied"])
        override = cfg_dir / "hub-sim-overrides.conf"
        assert override.exists()
        assert "ethernet" in override.read_text(encoding="utf-8")
        # Clear it.
        _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {"sim_conf_override": None}))
        assert not override.exists()
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_config_update_empty_patch_is_noop(spoke_loop):
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {}))
    assert resp["status"] == "SUCCESS"
    assert resp["applied"] == []


def test_usb_config_payload_emits_thresholds_protected_vmids_and_missing_timeout_seconds(spoke_loop):
    """The Setup/Proxmox 'VM Auto-Provisioning' card owns resource thresholds
    (cpu/mem provision + delete), protected_vmids, and the missing-dongle
    teardown timeout. The cs speak must accept+persist+emit all of them in
    usb_config (the agent reads them there), and:

    * missing_timeout is stored in MINUTES (UI label "Destroy after missing
      (minutes)") but the agent compares in SECONDS → emit minutes × 60.
    * VM 1001 is always protected (UI help) → merge it into the emitted list.
    * protected_vmids accepts comma-separated ints AND ranges.
    """
    spoke, loop = spoke_loop
    resp = _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {
        "cpu_provision_threshold": "75",
        "cpu_delete_threshold": "85",
        "mem_provision_threshold": "70",
        "mem_delete_threshold": "80",
        "usb_missing_timeout": "5",          # minutes
        "protected_vmids": "9000, 9005-9007",  # int + range, no 1001
    }))
    assert resp["status"] == "SUCCESS"
    for k in ("cpu_provision_threshold", "cpu_delete_threshold",
              "mem_provision_threshold", "mem_delete_threshold",
              "usb_missing_timeout", "protected_vmids"):
        assert k in resp["applied"], f"{k} not applied"

    cfg = spoke.settings.usb_config_payload()
    # Thresholds clamped + threaded through.
    assert cfg["cpu_provision_threshold"] == 75
    assert cfg["cpu_delete_threshold"] == 85
    assert cfg["mem_provision_threshold"] == 70
    assert cfg["mem_delete_threshold"] == 80
    # missing_timeout: 5 minutes → 300 seconds (agent compares in seconds).
    assert cfg["missing_timeout"] == 300
    # protected_vmids: 9000 + 9005-9007 range, AND 1001 always merged.
    assert set(cfg["protected_vmids"]) == {1001, 9000, 9005, 9006, 9007}
    assert cfg["protected_vmids"] == sorted(cfg["protected_vmids"])  # JSON-safe sorted list


def test_usb_config_payload_missing_timeout_zero_disables_teardown(spoke_loop):
    """usb_missing_timeout=0 must emit missing_timeout=0 so the agent's
    ``if missing_timeout > 0`` gate skips teardown entirely (disable-by-zero
    preserved across the minutes→seconds conversion)."""
    spoke, loop = spoke_loop
    _run(loop, spoke.handle_command("CS_CONFIG_UPDATE", {"usb_missing_timeout": "0"}))
    assert spoke.settings.usb_config_payload()["missing_timeout"] == 0