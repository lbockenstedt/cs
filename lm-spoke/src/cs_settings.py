"""CSSettings — the cs (Client-Simulation) spoke's USB-config / watchdog knob store.

Extracted verbatim from ``command_queue.py`` (Phase D2 port of the legacy
``cs/webui-spoke/server.py`` ``_proxmox_usb_config_payload``: lines 4673-4724).
The class is a small persisted store the cs UI edits and the pxmx agent consumes
via ``client_simulation.usb_config``; ``CommandQueue`` couples to it only through
its constructor's ``settings`` argument.

The module-level parsing normalizers this class relies on remain in
``command_queue.py`` (shared with ``CommandQueue``) and are imported here.
``command_queue`` re-exports ``CSSettings`` at import time so the historical
``from command_queue import CommandQueue, CSSettings`` keeps working.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Reads the INI-style simulation.conf to find the sim_phy setting.
import configparser
# Settings are stored on disk as JSON.
import json
# Standard-library logging for load/save problems.
import logging
# Generates a strong random client API key on first start.
import secrets
# Filesystem path handling.
from pathlib import Path
# Type hints only.
from typing import Any, Dict, Optional, Set

# Shared parsing/normalising helpers and the atomic file writer from command_queue.
from command_queue import (
    _normalize_toggle,
    _parse_int_ranges,
    _parse_json_list,
    _sanitize_vm_set_override,
    _setting_bool,
    _write_atomic,
)

# Reuse the command-queue logger so settings messages appear with queue messages.
logger = logging.getLogger("CSCommandQueue")


def _template_id_or_name(value: Any, default: Any) -> Any:
    """Coerce a configured clone-source template field to its emitted form.

    The template field accepts EITHER a vmid (numeric) OR a template NAME
    (text); the pxmx agent's ``_resolve_template_vmid`` does the actual lookup
    by vmid or by ``qm list`` name. Emit an int when the value is numeric
    (back-compat for any consumer expecting an int vmid, and the agent's
    numeric fast-path), else the raw stripped string (a NAME the agent
    resolves). Empty/None → ``default`` (an int vmid)."""
    # Turn the value into trimmed text ("" for None).
    s = str(value).strip() if value is not None else ""
    # Empty means "use the default vmid".
    if not s:
        return default
    # Numeric text becomes an int vmid.
    try:
        return int(s)
    # Anything else is a template name for the agent to look up.
    except ValueError:
        return s


class CSSettings:
    """Small persisted store for the USB-provision / watchdog knobs the cs UI
    edits and the agent consumes via ``client_simulation.usb_config``.

    The legacy ``settings`` dict was a flat key/value map read from a conf file;
    here it is a JSON document under ``data/cs_settings.json`` so the hub can
    later expose it for editing without touching app.js. Defaults mirror the
    legacy ``_proxmox_usb_config_payload`` fallbacks.
    """

    _DEFAULTS: Dict[str, Any] = {
        # USB dongle VID:PID values to provision, and ones to ignore (JSON arrays as text).
        "usb_vidpids": "[]",
        "usb_ignored_vidpids": "[]",
        # PCI-passthrough VID:PID lists that classify a VM's tier: a VM whose
        # hostpciN device matches one of these IDs is that tier. T1 and T3 are
        # PCI passthrough (T2 is the USB-passthrough tier). JSON array of bare
        # "vvvv:pppp" strings, mirroring usb_ignored_vidpids. Defaults match the
        # solutions-hpe originals (T1 1912:0015, T3 Atheros 168c:0034); editable
        # in Setup → Proxmox so the classifier gates on configured values.
        "t1_pci_vidpids": "[\"1912:0015\"]",
        "t3_pci_vidpids": "[\"168c:0034\"]",
        # Proxmox resource pool that auto-provisioned sim clients are cloned
        # into (qm clone --pool). Empty = no pool, the historical behaviour. The
        # WebUI populates its dropdown from the pools the hosts actually report,
        # because a pool that does not exist makes every clone FAIL.
        "sim_pool": "",
        # Minutes a dongle may be missing before its VM is destroyed (0 disables).
        "usb_missing_timeout": 60,
        # Whether VMs are auto-provisioned for newly found dongles ("on"/"off").
        "usb_auto_provision": "off",
        # Use every dongle found rather than only listed VID:PIDs.
        "use_all_dongles": False,
        # Maximum USB slots (VMs) per host.
        "usb_max_slots": 24,
        # VMID allocation range for new sim VMs. Defaults 90000-99999 match the
        # pxmx agent's historical default so an unset hub value preserves behavior;
        # the clone-source templates (image1/2_template_id) are OUTSIDE this range
        # (the agent excludes them from the allocator) and are cluster-consistent.
        "vmid_start": 90000,
        "vmid_end": 99999,
        # Pins a specific VM set/batch (0 = derive from hostname).
        "vm_set_override": 0,
        # Percentage of new VMs cloned from image 1 (the rest use image 2).
        "vm_image_1_pct": 50,
        # Clone-source templates (vmid or name) and optional per-image template specs.
        "image1_template_id": 100,
        "image2_template_id": 200,
        "image1_template_spec": None,
        "image2_template_spec": None,
        # How many reclones may run at once per host.
        "reclone_concurrency": 1,
        # VLAN id range (start-end) handed out for L1 simulation networks.
        "l1_vlan_start": 100,
        "l1_vlan_end": 199,
        # Guest-agent watchdog: enable switch, grace period, check interval, and the delays before
        # rebooting and then recloning an unresponsive VM (all in minutes).
        "guest_agent_watchdog_enabled": "on",
        "guest_agent_grace_minutes": 20,
        "guest_agent_check_interval_minutes": 10,
        "guest_agent_reboot_after_minutes": 10,
        "guest_agent_reclone_after_minutes": 30,
        # Whether the watchdog may reboot VMs.
        "watchdog_reboot_enabled": "on",
        # Host CPU/memory % above which provisioning pauses / VMs are deleted.
        "cpu_provision_threshold": 80,
        "cpu_delete_threshold": 90,
        "mem_provision_threshold": 80,
        "mem_delete_threshold": 90,
        # protected-vmid list (comma-separated ints/ranges) — drives the queue guard
        "protected_vmids": "",
        # shared PSK for the client API (/ws/client + mutating HTTP routes) for
        # callers OFF the isolated sim segment (169.253.1.0/24) — see
        # client_api._key_ok. Never persisted empty: __init__ generates one on
        # first start (see _ensure_client_api_key) so an unconfigured deployment
        # denies off-segment callers instead of allowing them.
        "client_api_key": "",
    }

    def __init__(self, data_dir: Path, config_dir: Path) -> None:
        """Load settings from ``data_dir`` and make sure a client API key exists."""
        # Where settings are stored, and where the simulation config files live.
        self.data_dir = data_dir
        self.config_dir = config_dir
        # Settings file path.
        self.path = data_dir / "cs_settings.json"
        # Start from the defaults, then overlay what is saved on disk.
        self.settings: Dict[str, Any] = dict(self._DEFAULTS)
        self._load()
        # Create a client API key if none is set.
        self._ensure_client_api_key()

    def _ensure_client_api_key(self) -> None:
        """Deny-by-default: an empty/unset client_api_key must not leave the
        off-segment routes wide open (client_api._key_ok treated empty as
        allow-all). Generate a strong PSK on first start and persist it, so an
        existing deployment that never set one keeps its sim-segment clients
        working (they're trusted by network position, not by this key) while
        off-segment callers can no longer skip auth entirely."""
        # No key set yet: generate a random 32-byte URL-safe key and save it.
        if not str(self.settings.get("client_api_key") or "").strip():
            self.settings["client_api_key"] = secrets.token_urlsafe(32)
            self._save()

    def _load(self) -> None:
        """Overlay saved settings from disk onto the defaults."""
        try:
            # Only read a file that exists ("" is treated as an empty object).
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                # Accept only a JSON object.
                if isinstance(loaded, dict):
                    self.settings.update(loaded)
        # A corrupt file is logged; defaults stay in effect.
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS settings load failed (%s): %s", self.path, exc)

    def _save(self) -> None:
        """Write the settings to disk atomically."""
        try:
            # Create the data directory if needed and write the file atomically.
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(self.settings, default=str))
        # A write failure is logged but never crashes the spoke.
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS settings save failed (%s): %s", self.path, exc)

    def get(self, key: str, fallback: Any = None) -> Any:
        """Return a setting, falling back to ``fallback`` or the built-in default."""
        # Use the saved value, else the caller's fallback, else the default.
        v = self.settings.get(key, fallback if fallback is not None else self._DEFAULTS.get(key))
        return v

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``patch`` into the settings, save, and return a copy of all settings."""
        # Apply the changes and save them.
        self.settings.update(patch or {})
        self._save()
        # Return a copy so callers cannot change the internal dict.
        return dict(self.settings)

    def protected_vmids(self) -> Set[int]:
        """Return the set of VM ids that must never be modified."""
        # Parse the comma-separated ids/ranges into a set.
        return _parse_int_ranges(self.settings.get("protected_vmids", ""))

    # ── Per-host USB VMID overrides ─────────────────────────────────────────
    # Optional per-host knobs (vmid_start / vmid_end / vm_set_override) that
    # override the global range for one proxmox host, so the cs speak can pin a
    # specific host's batch without changing the global default. Persisted as a
    # ``host_usb_overrides`` map inside cs_settings.json. The pxmx agent honors
    # a non-default vmid_start/vmid_end over its own hostname-suffix derivation.

    # The only settings a per-host override may change.
    _HOST_USB_OVERRIDE_KEYS = ("vmid_start", "vmid_end", "vm_set_override")

    def _host_usb_store(self) -> Dict[str, Dict[str, Any]]:
        """Return the per-host override map, creating it if needed."""
        # The map lives inside the settings dict so it is saved with everything else.
        return self.settings.setdefault("host_usb_overrides", {})  # type: ignore[return-value]

    def host_usb_override(self, hostname: str) -> Dict[str, Any]:
        """Per-host USB VMID override for one hostname (empty dict if none)."""
        # No hostname: no override.
        if not hostname:
            return {}
        # Return a copy of that host's override (empty if none).
        return dict(self._host_usb_store().get(hostname, {}) or {})

    def all_host_usb_overrides(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of every host's USB override."""
        # Copy each host's dict so callers cannot change stored data.
        return {h: dict(v) for h, v in self._host_usb_store().items()}

    def set_host_usb_override(self, hostname: str,
                              knobs: Dict[str, Any]) -> Dict[str, Any]:
        """Merge new USB VMID override values for ``hostname``, save, and return them."""
        # No hostname: nothing to store.
        if not hostname:
            return {}
        # Start from the host's current override.
        store = self._host_usb_store()
        cur = dict(store.get(hostname, {}) or {})
        # Store the start of the VMID range when supplied and numeric.
        if "vmid_start" in knobs and knobs["vmid_start"] is not None:
            try:
                cur["vmid_start"] = int(knobs["vmid_start"])
            # Ignore non-numeric values.
            except (TypeError, ValueError):
                pass
        # Store the end of the VMID range when supplied and numeric.
        if "vmid_end" in knobs and knobs["vmid_end"] is not None:
            try:
                cur["vmid_end"] = int(knobs["vmid_end"])
            # Ignore non-numeric values.
            except (TypeError, ValueError):
                pass
        # Store a sanitised VM-set override when supplied.
        if "vm_set_override" in knobs and knobs["vm_set_override"] is not None:
            cur["vm_set_override"] = _sanitize_vm_set_override(knobs["vm_set_override"])
        # Save the updated override and return a copy.
        store[hostname] = cur
        self._save()
        return dict(cur)

    def clear_host_usb_override(self, hostname: str) -> bool:
        """Remove a host's USB override; return True if one existed."""
        # The current override map (may be absent).
        store = self.settings.get("host_usb_overrides", {}) or {}
        # Remove the host's entry and save.
        if hostname in store:
            store.pop(hostname)
            self._save()
            return True
        # Nothing to remove.
        return False

    # ── USB config payload (port of _proxmox_usb_config_payload) ───────────

    def _sim_phy_cached(self) -> str:
        """``sim_phy`` from simulation.conf (+ hub-sim-overrides.conf overlay),
        cached and re-parsed only when either file's mtime changes. Read+parse
        ran on EVERY CS_GET_USB_CONFIG (~5s) before — a per-poll disk read +
        configparser on the shared event loop. Two stat() calls (fast metadata)
        replace it when the files are unchanged (the common case)."""
        # Paths of the base config and the hub's override file.
        sim_conf = self.config_dir / "simulation.conf"
        ov_conf = self.config_dir / "hub-sim-overrides.conf"

        def _mtime(p: Path) -> int:
            """Return the file's modification time in nanoseconds, or 0 if missing."""
            # Read the file's modification time.
            try:
                return p.stat().st_mtime_ns
            # Missing file.
            except OSError:
                return 0
        # Cache key: both files' modification times.
        key = (_mtime(sim_conf), _mtime(ov_conf))
        # Reuse the cached value if neither file changed.
        cache = getattr(self, "_sim_phy_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        # Default when the setting is missing or invalid.
        sim_phy = "wireless"
        try:
            # Parse simulation.conf, overlay the hub overrides, and read [simulation] sim_phy.
            if sim_conf.exists():
                parser = configparser.ConfigParser()
                parser.read_string(sim_conf.read_text(encoding="utf-8"))
                self._merge_ini_override(parser, ov_conf)
                sim_phy = parser.get("simulation", "sim_phy", fallback="wireless").strip().lower() or "wireless"
        # Unreadable config: keep the default.
        except Exception:
            pass
        # Only these three values are valid.
        if sim_phy not in {"wireless", "ethernet", "any"}:
            sim_phy = "wireless"
        # Cache the result with its key and return it.
        self._sim_phy_cache = (key, sim_phy)
        return sim_phy

    def usb_config_payload(self, hostname: Optional[str] = None) -> Dict[str, Any]:
        """Build the 27-key ``usb_config`` blob the agent provisions from.

        ``sim_phy`` is read from ``configs/simulation.conf`` (with the
        ``hub-sim-overrides.conf`` overlay merged on top) exactly as the legacy
        spoke did; the remaining knobs come from this settings store.
        """
        # Physical-layer mode (wireless/ethernet/any).
        sim_phy = self._sim_phy_cached()

        # Sanitised VM-set override and the optional template specs.
        vm_set_override = _sanitize_vm_set_override(self.get("vm_set_override", 0))
        img1_spec = self.get("image1_template_spec")
        img2_spec = self.get("image2_template_spec")
        # Template field accepts a vmid OR a name — emit int when numeric, else
        # the raw name string (the pxmx agent resolves it). See
        # _template_id_or_name.
        # Clone-source templates for images 1 and 2.
        img1_id = _template_id_or_name(self.get("image1_template_id", 100), 100)
        img2_id = _template_id_or_name(self.get("image2_template_id", 200), 200)

        # missing_timeout is stored in MINUTES (the UI label is "Destroy after
        # missing (minutes)"); the pxmx agent compares ``now - missing_since`` in
        # SECONDS, so emit seconds here (minutes × 60). 0 = teardown disabled
        # (preserved — the agent gates on ``missing_timeout > 0``).
        # Missing-dongle timeout in minutes.
        _missing_min = int(self.get("usb_missing_timeout", 60) or 0)
        # protected_vmids: the hub container (VMID 1001) is ALWAYS protected
        # (matches the UI help "VM 1001 is always protected") — merge it into the
        # emitted list so the agent's as-is resolution always includes it.
        # Protected VM ids, always including the hub container (1001).
        _protected = set(_parse_int_ranges(self.get("protected_vmids", ""))) | {1001}

        # Build the agent payload; numeric settings are clamped to safe ranges (percentages 0-100,
        # VLANs 1-4094, at least 1 for counts and minutes).
        payload = {
            "vidpids": _parse_json_list(self.get("usb_vidpids", "[]")),
            "missing_timeout": _missing_min * 60 if _missing_min > 0 else 0,
            "image1_template_id": img1_id,
            "image1_template_spec": img1_spec,
            "image2_template_id": img2_id,
            "image2_template_spec": img2_spec,
            "template_vmid_specs": [img1_spec, img2_spec],
            "image1_pct": max(0, min(100, int(self.get("vm_image_1_pct", 50) or 50))),
            "auto_provision": _normalize_toggle(self.get("usb_auto_provision", "off")),
            "use_all_dongles": _setting_bool(self.get("use_all_dongles", False)),
            "max_slots": max(1, min(256, int(self.get("usb_max_slots", 24) or 24))),
            "vmid_start": int(self.get("vmid_start", 90000) or 90000),
            "vmid_end": int(self.get("vmid_end", 99999) or 99999),
            "vm_set_override": vm_set_override,
            "ignored_vidpids": _parse_json_list(self.get("usb_ignored_vidpids", "[]")),
            "t1_pci_vidpids": _parse_json_list(self.get("t1_pci_vidpids", "[]")),
            "t3_pci_vidpids": _parse_json_list(self.get("t3_pci_vidpids", "[]")),
            # Per-host T1/T3 opt-out. Consumed spoke-side in
            # client_rows.build_client_rows (_host_t1_excluded/_host_t3_excluded): a
            # client on a listed pxmx server that would classify as T1/T3 (no USB
            # dongle) is forced to T2, so an excluded host never deploys/renders
            # T1/T3. Also emitted here for the agent payload, where
            # usb_provision._host_t1_excluded/_host_t3_excluded gate the actual PCI
            # passthrough at provision time.
            "t1_exclude_hosts": _parse_json_list(self.get("t1_exclude_hosts", "[]")),
            "t3_exclude_hosts": _parse_json_list(self.get("t3_exclude_hosts", "[]")),
            "sim_pool": str(self.get("sim_pool", "") or "").strip(),
            "sim_phy": sim_phy,
            "reclone_concurrency": max(1, int(self.get("reclone_concurrency", 1) or 1)),
            "l1_vlan_start": max(1, min(4094, int(self.get("l1_vlan_start", 100) or 100))),
            "l1_vlan_end": max(1, min(4094, int(self.get("l1_vlan_end", 199) or 199))),
            "guest_agent_watchdog_enabled": _normalize_toggle(self.get("guest_agent_watchdog_enabled", "on")),
            "guest_agent_grace_minutes": max(1, int(self.get("guest_agent_grace_minutes", 20) or 20)),
            "guest_agent_check_interval_minutes": max(1, int(self.get("guest_agent_check_interval_minutes", 10) or 10)),
            "guest_agent_reboot_after_minutes": max(1, int(self.get("guest_agent_reboot_after_minutes", 10) or 10)),
            "guest_agent_reclone_after_minutes": max(1, int(self.get("guest_agent_reclone_after_minutes", 30) or 30)),
            "watchdog_reboot_enabled": _normalize_toggle(self.get("watchdog_reboot_enabled", "on")),
            "cpu_provision_threshold": max(0, min(100, int(self.get("cpu_provision_threshold", 80) or 80))),
            "cpu_delete_threshold": max(0, min(100, int(self.get("cpu_delete_threshold", 90) or 90))),
            "mem_provision_threshold": max(0, min(100, int(self.get("mem_provision_threshold", 80) or 80))),
            "mem_delete_threshold": max(0, min(100, int(self.get("mem_delete_threshold", 90) or 90))),
            "protected_vmids": sorted(_protected),
        }

        # N-image clone sources. Emit image_count + image{i}_template_id/_pct for
        # the pxmx agent's _resolve_images ONLY when the operator set vm_image_count
        # via the new VM Images UI. Absent → leave image1/image2/image1_pct (above)
        # as the source of truth so existing 2-image configs keep working unchanged.
        # Optional number of clone images configured in the VM Images UI.
        _raw_count = self.get("vm_image_count")
        # Only emit the N-image fields when the operator set a count.
        if _raw_count is not None and str(_raw_count).strip() != "":
            # Clamp the count to 1-20 (1 if invalid).
            try:
                _img_count = max(1, min(20, int(_raw_count or 1)))
            except (TypeError, ValueError):
                _img_count = 1
            # Include the count.
            payload["image_count"] = _img_count
            # For each image, emit its template and its percentage.
            for _i in range(1, _img_count + 1):
                # Template configured for this image.
                _raw = self.get(f"image{_i}_template_id")
                # Images 1 and 2 fall back to the legacy template fields.
                if (_raw is None or str(_raw).strip() == "") and _i <= 2:
                    _raw = {1: img1_id, 2: img2_id}.get(_i)
                # Emit the template when set (int if numeric, else a name).
                if _raw is not None and str(_raw).strip() != "":
                    payload[f"image{_i}_template_id"] = _template_id_or_name(_raw, _raw)
                # Emit the image's percentage, clamped to 0-100 (0 if invalid).
                try:
                    payload[f"image{_i}_pct"] = max(0, min(100, int(self.get(f"vm_image_{_i}_pct", 0) or 0)))
                except (TypeError, ValueError):
                    payload[f"image{_i}_pct"] = 0

        # Per-host override: a non-default vmid_start/vmid_end (or vm_set_override)
        # pinned for this hostname overrides the global values, so the pxmx agent
        # honors it over its own hostname-suffix derivation. (vm_set_override is
        # only meaningful to the agent when vmid_start/vmid_end are at the default.)
        # Apply this host's overrides on top of the global values.
        if hostname:
            # The host's override settings.
            ov = self.host_usb_override(hostname)
            # Override the start, end and VM set when present.
            if "vmid_start" in ov:
                payload["vmid_start"] = int(ov["vmid_start"])
            if "vmid_end" in ov:
                payload["vmid_end"] = int(ov["vmid_end"])
            if "vm_set_override" in ov:
                payload["vm_set_override"] = _sanitize_vm_set_override(ov["vm_set_override"])
        # Return the finished payload.
        return payload

    @staticmethod
    def _merge_ini_override(parser: configparser.ConfigParser, override_path: Path) -> None:
        """Copy every key from the INI file at ``override_path`` into ``parser``."""
        # No override file: nothing to merge.
        if not override_path.exists():
            return
        # Parse the override file.
        try:
            ov = configparser.ConfigParser()
            ov.read_string(override_path.read_text(encoding="utf-8"))
            # Copy each section, creating it in the base config when missing.
            for section in ov.sections():
                if not parser.has_section(section):
                    parser.add_section(section)
                # Override values replace the base values.
                for key, value in ov.items(section):
                    parser.set(section, key, value)
        # A broken override file is logged and ignored.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not apply hub override %s: %s", override_path, exc)
