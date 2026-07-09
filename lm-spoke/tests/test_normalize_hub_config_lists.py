"""``normalize_hub_config_lists`` (cs spoke local_store) — the Setup/Proxmox list
fields are collected in the local Setup UI as comma/space-delimited text (no raw
JSON) and normalized to lists at the LocalStore storage boundary so downstream
``_parse_json_list`` / ``_apply_hub_config`` read a real list. Mirrors the hub-
side ``normalize_hub_config_lists`` in lm/core/src/simulations/routes.py.
"""
import json

from local_store import normalize_hub_config_lists


def test_delimited_comma_to_list():
    out = normalize_hub_config_lists({"usb_ignored_vidpids": "1a2b:3c4d, 5678:9abc"})
    assert out["usb_ignored_vidpids"] == ["1a2b:3c4d", "5678:9abc"]


def test_delimited_whitespace_to_list():
    out = normalize_hub_config_lists({"t1_pci_vidpids": "1912:0015 168c:0034"})
    assert out["t1_pci_vidpids"] == ["1912:0015", "168c:0034"]


def test_already_list_passthrough_dedup_lowercases():
    out = normalize_hub_config_lists({"usb_ignored_vidpids": ["1A2B:3C4D", "1a2b:3c4d", "x"]})
    assert out["usb_ignored_vidpids"] == ["1a2b:3c4d"]


def test_already_json_string_passthrough():
    out = normalize_hub_config_lists({"usb_ignored_vidpids": '["1a2b:3c4d"]'})
    assert out["usb_ignored_vidpids"] == ["1a2b:3c4d"]


def test_empty_string_to_empty_list():
    out = normalize_hub_config_lists({"usb_ignored_vidpids": ""})
    assert out["usb_ignored_vidpids"] == []


def test_ignored_hostnames_keeps_order_dedup():
    out = normalize_hub_config_lists({"ignored_hostnames": "sim-rpi-0000, sim-rpi-0001, sim-rpi-0000"})
    assert out["ignored_hostnames"] == ["sim-rpi-0000", "sim-rpi-0001"]


def test_usb_vidpids_delimited_to_objects():
    out = normalize_hub_config_lists({"usb_vidpids": "1a2b:3c4d, 5678:9abc"})
    assert out["usb_vidpids"] == [
        {"vidpid": "1a2b:3c4d", "type": "wireless", "label": "1a2b:3c4d"},
        {"vidpid": "5678:9abc", "type": "wireless", "label": "5678:9abc"},
    ]


def test_usb_vidpids_preserves_type_label_from_stored():
    stored = {"usb_vidpids": [{"vidpid": "1a2b:3c4d", "type": "wired", "label": "My Dongle"}]}
    out = normalize_hub_config_lists({"usb_vidpids": "1a2b:3c4d, 9999:8888"}, stored)
    assert out["usb_vidpids"] == [
        {"vidpid": "1a2b:3c4d", "type": "wired", "label": "My Dongle"},
        {"vidpid": "9999:8888", "type": "wireless", "label": "9999:8888"},
    ]


def test_usb_vidpids_already_objects_keep_type_label():
    raw = [{"vidpid": "1a2b:3c4d", "type": "wired", "label": "x"}]
    out = normalize_hub_config_lists({"usb_vidpids": raw})
    assert out["usb_vidpids"][0]["type"] == "wired"


def test_fields_not_present_left_untouched():
    out = normalize_hub_config_lists({"usb_auto_provision": "on"})
    assert out == {"usb_auto_provision": "on"}


def test_does_not_mutate_caller_dict():
    src = {"usb_ignored_vidpids": "1a2b:3c4d"}
    normalize_hub_config_lists(src)
    assert src == {"usb_ignored_vidpids": "1a2b:3c4d"}


def test_round_trip_downstream_parse():
    out = normalize_hub_config_lists({"usb_vidpids": "1a2b:3c4d", "usb_ignored_vidpids": "dead:beef"})
    assert isinstance(json.loads(json.dumps(out["usb_vidpids"])), list)
    assert isinstance(json.loads(json.dumps(out["usb_ignored_vidpids"])), list)