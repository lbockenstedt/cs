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