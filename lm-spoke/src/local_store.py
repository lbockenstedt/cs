"""JSON-backed local config store for a standalone/hub-connected cs spoke's
own Setup-tab knobs: hub_config (auto-provisioning thresholds/templates/VMID
range — the settings the LM hub would otherwise own per-tenant) and
central_config/central_sites_config (Aruba Central API credentials + the
sites to poll).

Adapted from lm/core/src/simulations/store.py's SimulationsStore — same
defaults, same get/set/reset semantics — but single-tenant: this spoke IS
the tenant, so every method drops the tenant_id parameter the hub version
threads through per-tenant. Re-sync _DEFAULT_HUB_CONFIG from there if the
hub's knob set changes.

Persisted to ``data/local_store.json`` next to client_registry.py's
``data/clients.json`` (same runtime-state directory, gitignored).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("LocalStore")

# Mirror of lm/core/src/simulations/store.py's _DEFAULT_HUB_CONFIG. Keep in
# sync if the hub's knob set changes — this is what seeds a fresh standalone
# deployment's Setup/Proxmox card instead of showing a blank grid.
_DEFAULT_HUB_CONFIG: Dict[str, Any] = {
    # Provisioning Behavior
    "usb_auto_provision": "off",
    "usb_missing_timeout": 60,            # minutes (cs spoke ×60 → seconds)
    "usb_max_slots": 24,
    # Resource Thresholds (% — 1-hour average)
    "cpu_provision_threshold": 80,
    "cpu_delete_threshold": 90,
    "mem_provision_threshold": 80,
    "mem_delete_threshold": 90,
    # Tier classification by PCI passthrough (T1/T3 are PCI, T2 is USB). A VM
    # whose hostpciN device matches one of these VID:PIDs is that tier. Defaults
    # match the solutions-hpe originals; edited in the Hub Config card. NOT in
    # preserve-on-reset: reset restores these canonical tier IDs.
    "t1_pci_vidpids": ["1912:0015"],
    "t3_pci_vidpids": ["168c:0034"],
    # VM Templates (clone-source VMIDs + image1 mix)
    "vm_image_1_template_id": 100,
    "vm_image_2_template_id": 200,
    "vm_image_1_pct": 50,
    # Parallel Provisioning
    "reclone_concurrency": 1,
    # VMID allocation range for new sim VMs (templates excluded by the agent)
    "vmid_start": 90000,
    "vmid_end": 99999,
    # Remaining hub-owned knobs (the Hub Config card)
    "use_all_dongles": "off",
    "vm_silent_timeout": 24,
    "l1_vlan_start": 100,
    "l1_vlan_end": 199,
    "reclone_schedule_enabled": "off",
    # Guest-agent watchdog group
    "guest_agent_watchdog_enabled": "on",
    "guest_agent_grace_minutes": 20,
    "guest_agent_check_interval_minutes": 10,
    "guest_agent_reboot_after_minutes": 10,
    "guest_agent_reclone_after_minutes": 30,
    "watchdog_reboot_enabled": "on",
}

# JSON-list fields holding real certified/ignored data — preserved across a
# "reset to default" (resetting the knobs must not de-certify dongles).
_HUB_CONFIG_PRESERVE_ON_RESET = (
    "usb_vidpids", "usb_ignored_vidpids", "ignored_hostnames",
)


class LocalStore:
    def __init__(self, data_dir: os.PathLike | str) -> None:
        self._path = Path(data_dir) / "local_store.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f) or {}
        except FileNotFoundError:
            self._data = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("LocalStore: load failed (%s): %s — starting empty",
                           self._path, exc)
            self._data = {}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LocalStore: save failed (%s): %s", self._path, exc)

    # ── hub-config (usb provisioning / vm images / reclone knobs) ──────────

    def get_hub_config(self) -> Dict[str, Any]:
        stored = self._data.get("hub_config") or {}
        merged = dict(_DEFAULT_HUB_CONFIG)
        merged.update(stored)
        return {"hub_config_enabled": bool(self._data.get("hub_config_enabled", False)),
                "hub_config": merged}

    def set_hub_config(self, enabled: bool, hub_config: Dict[str, Any]) -> None:
        with self._lock:
            self._data["hub_config_enabled"] = bool(enabled)
            self._data["hub_config"] = hub_config or {}
            self._save()

    def reset_hub_config(self) -> Dict[str, Any]:
        with self._lock:
            stored = self._data.get("hub_config") or {}
            preserved = {k: stored.get(k, "[]") for k in _HUB_CONFIG_PRESERVE_ON_RESET}
            new_cfg = dict(_DEFAULT_HUB_CONFIG)
            new_cfg.update({"protected_vmids": "", "repo_branch": "",
                           "reclone_schedule_cron": ""})
            new_cfg.update(preserved)
            self._data["hub_config"] = new_cfg
            self._save()
            return {"hub_config_enabled": bool(self._data.get("hub_config_enabled", False)),
                    "hub_config": dict(new_cfg)}

    # ── central API config (mode + cluster creds) ──────────────────────────

    def get_central_config(self) -> Dict[str, Any]:
        return dict(self._data.get("central_config") or {})

    def set_central_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._data["central_config"] = cfg or {}
            self._save()

    # ── central sites config (which sites to poll + monitored checks) ──────

    def get_central_sites_config(self) -> Dict[str, Any]:
        return dict(self._data.get("central_sites_config") or {})

    def set_central_sites_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._data["central_sites_config"] = cfg or {}
            self._save()
