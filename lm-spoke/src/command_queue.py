"""CommandQueue + CSSettings — the cs (Client-Simulation) spoke's command
queue and USB-config store.

Phase D2 port of the legacy ``cs/webui-spoke/server.py`` command queue
(``_make_command``/``_enqueue_command_locked``/``_peek_pending_agent_commands``
``_ack_command_internal``: lines 3509-4513, 13356-13463) and USB-config payload
(``_proxmox_usb_config_payload``: lines 4673-4724).

The cs spoke owns the queue; the LM hub's ``CSBridgePoller``
(``lm/core/src/gateway/cs_bridge.py``) polls it via ``CS_POLL_AGENT_INBOX``,
relays each pending command to the unified pxmx agent as ``CS_COMMAND`` (through
the pxmx spoke's ``SPOKE_RELAY``), and acks the terminal result back via
``CS_ACK_COMMAND``. USB config for the agent is read from a small
``CSSettings`` store via ``CS_GET_USB_CONFIG``; the hub bridge diffs it and
pushes changes down to the agent's ``client_simulation.usb_config`` through
``SET_AGENT_CONFIG`` → ``UPDATE_CONFIG``.

Command shape (mirrors legacy ``_make_command`` — 11 keys)::

    {id, target, action, args, type, status, created_at, updated_at,
     expires_at, purge_after, result, message}

Statuses: ``pending → delivered → completed/failed/expired``.

Semantics (ported from the legacy queue):
  - **idempotent enqueue**: a second enqueue with the same target+action+args
    signature while one is still ``pending``/``delivered`` returns the existing
    command (no duplicate).
  - **stale-delivered reset**: on poll, a ``delivered`` command older than
    ``STALE_DELIVERED_SECS`` (default 30s, env ``CS_STALE_DELIVERED_SECS``) with
    no ack is reset to ``pending`` so it is re-delivered (mirrors the legacy
    WS-reconnect reset).
  - **cleanup**: ``pending``/``delivered`` older than ``COMMAND_EXPIRE_SECS``
    (default 7200s = 2h, env ``CS_COMMAND_EXPIRE_SECS``) become ``expired``;
    terminal commands past ``purge_after`` are dropped (retention
    ``COMMAND_RESULT_RETENTION_SECS``, default 86400s, env
    ``CS_COMMAND_RESULT_RETENTION_SECS``). The 2h default covers a
    cloud-connected agent offline past the old 15-min window; the env knob
    lets a site tune the stale-command tradeoff (a delete queued before a
    long outage firing much later, possibly against rebuilt state).
  - **trim**: queue capped at ``COMMAND_MAX`` (default 100, env
    ``CS_COMMAND_MAX``); terminal commands dropped oldest-first.
  - **ack**: idempotent terminal update (``completed``/``failed``); sets
    ``message``/``updated_at``/``purge_after``; no prior-state check (a late ack
    for an already-terminal command re-records the result).

Safeguard (defense-in-depth on top of the agent's execution-layer
``cs_guard``): enqueue of a ``_VM_ACTIONS`` command on ``target=="proxmox"``
refuses any ``vmid < SIM_VMIN`` (90000) or in ``protected_vmids`` (default
``{1001}``; configurable per host from the hub). Sim VMs are 90001+, so the cs
UI only ever manages sim VMs; the hub/system containers stay untouchable.

Source of truth: ``cs/webui-spoke/server.py``
  - enqueue:        ``_enqueue_command_locked`` (lines 4382-4413)
  - make command:   ``_make_command`` (4414-4430)
  - cleanup/trim:   ``_cleanup_commands_locked`` (4340-4367), ``_trim_commands_locked`` (4323-4338)
  - duplicate find: ``_find_active_duplicate_command_locked`` (4370-4381)
  - poll:           ``_peek_pending_agent_commands_locked`` (4514+), ``_reset_delivered_commands_locked`` (4454-4475)
  - ack:            ``_ack_command_internal`` (13419-13440)
  - usb config:     ``_proxmox_usb_config_payload`` (4673-4724)
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("CSCommandQueue")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except Exception:
        return default


# ── tunables (env-overridable so cloud / high-latency sites can stretch them) ─
COMMAND_MAX = _env_int("CS_COMMAND_MAX", 100, 10)        # keep last N commands in memory
# A pending/delivered command older than this from CREATION is marked expired.
# Default 2h — a cloud-connected agent can be offline (link flap / NAT idle /
# WAN brownout) well past the old 15-min window; expiring a delete_vm at 15 min
# meant the VM was never torn down when the agent finally reconnected. Bump via
# CS_COMMAND_EXPIRE_SECS for sites with longer outages; lower it for on-prem
# sites that want stale commands reaped faster (stale-command tradeoff: a
# delete queued before a long outage fires much later, possibly against
# rebuilt state — keep it bounded to what your outage window realistically is).
COMMAND_EXPIRE_SECS = _env_int("CS_COMMAND_EXPIRE_SECS", 7200, 60)
# Terminal rows (completed/failed/expired) kept for the queue view, then purged.
COMMAND_RESULT_RETENTION_SECS = _env_int("CS_COMMAND_RESULT_RETENTION_SECS", 86400, 60)
# A delivered command with no ack older than this is reset to pending so the
# next poll re-sends it. 30s is fine on a tight LAN; raise it on a high-latency
# cloud link where the agent's ack can legitimately take longer (avoids
# needless re-sends that double-execute an idempotent-but-noisy op).
STALE_DELIVERED_SECS = _env_int("CS_STALE_DELIVERED_SECS", 30, 5)

# Verified-report safety net for delete_vm / reclone_vm: a delivered long op
# that hasn't been TOUCHED (the bridge refreshes updated_at on every ACCEPTED
# re-ack while the agent is alive) or acked for this long is treated as LOST —
# the agent crashed, the WS dropped, or the long-op task died with no terminal.
# The verify-sweep requeues it (bounded) so it genuinely re-runs instead of
# spinning until the 2h COMMAND_EXPIRE_SECS (the "some VMs never get deleted
# on a bulk delete" symptom). 5 min/try × 2 tries ≈ 10 min max safety net; a
# healthy delete finishes in well under a minute so this only fires on real
# stalls. The bridge's touch keeps a slow-but-alive delete from being penalized.
DELETE_VERIFY_TIMEOUT_SECS = _env_int("CS_DELETE_VERIFY_TIMEOUT_S", 300, 30)
DELETE_VERIFY_MAX_RETRIES = _env_int("CS_DELETE_MAX_RETRIES", 2, 1)
# Actions covered by the verify-report net (the long teardown ops).
_VERIFY_ACTIONS = {"delete_vm", "reclone_vm"}

# Single-VM actions that take a ``vmid`` arg and must respect the sim range +
# protected-VMID guard at enqueue time (defense-in-depth; the agent's cs_guard
# enforces the same at execution).
_VM_ACTIONS = {"start_vm", "stop_vm", "reboot_vm", "snapshot_vm", "reclone_vm", "delete_vm"}

# Sim VMID floor (matches ``cs_guard.SIM_VMIN`` shipped in Phase B). VMs below
# this are not Client-Simulation VMs and must never be managed from the cs UI.
SIM_VMIN = 90000
DEFAULT_PROTECTED_VMIDS: Set[int] = {1001}


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_action(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalize_type(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().replace("-", "_")
    return normalized or None


def _args_signature(args: Optional[Dict[str, Any]]) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        safe = json.loads(json.dumps(args or {}, default=str))
        return json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_hostname(hostname: Any) -> str:
    return str(hostname or "").strip().rstrip(".").lower()


def _hostname_aliases(hostname: Any) -> Tuple[str, ...]:
    normalized = _normalize_hostname(hostname)
    if not normalized:
        return ()
    aliases = [normalized]
    short = normalized.split(".", 1)[0]
    if short and short not in aliases:
        aliases.append(short)
    return tuple(aliases)


def _hostnames_match(left: Any, right: Any) -> bool:
    la = set(_hostname_aliases(left))
    return bool(la and la.intersection(_hostname_aliases(right)))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── CSSettings ───────────────────────────────────────────────────────────────

class CSSettings:
    """Small persisted store for the USB-provision / watchdog knobs the cs UI
    edits and the agent consumes via ``client_simulation.usb_config``.

    The legacy ``settings`` dict was a flat key/value map read from a conf file;
    here it is a JSON document under ``data/cs_settings.json`` so the hub can
    later expose it for editing without touching app.js. Defaults mirror the
    legacy ``_proxmox_usb_config_payload`` fallbacks.
    """

    _DEFAULTS: Dict[str, Any] = {
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
        "usb_missing_timeout": 60,
        "usb_auto_provision": "off",
        "use_all_dongles": False,
        "usb_max_slots": 24,
        # VMID allocation range for new sim VMs. Defaults 90000-99999 match the
        # pxmx agent's historical default so an unset hub value preserves behavior;
        # the clone-source templates (image1/2_template_id) are OUTSIDE this range
        # (the agent excludes them from the allocator) and are cluster-consistent.
        "vmid_start": 90000,
        "vmid_end": 99999,
        "vm_set_override": 0,
        "vm_image_1_pct": 50,
        "image1_template_id": 100,
        "image2_template_id": 200,
        "image1_template_spec": None,
        "image2_template_spec": None,
        "reclone_concurrency": 1,
        "l1_vlan_start": 100,
        "l1_vlan_end": 199,
        "guest_agent_watchdog_enabled": "on",
        "guest_agent_grace_minutes": 20,
        "guest_agent_check_interval_minutes": 10,
        "guest_agent_reboot_after_minutes": 10,
        "guest_agent_reclone_after_minutes": 30,
        "watchdog_reboot_enabled": "on",
        "cpu_provision_threshold": 80,
        "cpu_delete_threshold": 90,
        "mem_provision_threshold": 80,
        "mem_delete_threshold": 90,
        # protected-vmid list (comma-separated ints/ranges) — drives the queue guard
        "protected_vmids": "",
        # shared PSK for the client API (/ws/client + mutating HTTP routes).
        # Empty = open (the t3 agent sends no key; linux agent fetches it first).
        # Set via CS_UPDATE_SETTINGS at runtime → data/cs_settings.json (never committed).
        "client_api_key": "",
    }

    def __init__(self, data_dir: Path, config_dir: Path) -> None:
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.path = data_dir / "cs_settings.json"
        self.settings: Dict[str, Any] = dict(self._DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                if isinstance(loaded, dict):
                    self.settings.update(loaded)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS settings load failed (%s): %s", self.path, exc)

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(self.settings, default=str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CS settings save failed (%s): %s", self.path, exc)

    def get(self, key: str, fallback: Any = None) -> Any:
        v = self.settings.get(key, fallback if fallback is not None else self._DEFAULTS.get(key))
        return v

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        self.settings.update(patch or {})
        self._save()
        return dict(self.settings)

    def protected_vmids(self) -> Set[int]:
        return _parse_int_ranges(self.settings.get("protected_vmids", ""))

    # ── Per-host USB VMID overrides ─────────────────────────────────────────
    # Optional per-host knobs (vmid_start / vmid_end / vm_set_override) that
    # override the global range for one proxmox host, so the cs speak can pin a
    # specific host's batch without changing the global default. Persisted as a
    # ``host_usb_overrides`` map inside cs_settings.json. The pxmx agent honors
    # a non-default vmid_start/vmid_end over its own hostname-suffix derivation.

    _HOST_USB_OVERRIDE_KEYS = ("vmid_start", "vmid_end", "vm_set_override")

    def _host_usb_store(self) -> Dict[str, Dict[str, Any]]:
        return self.settings.setdefault("host_usb_overrides", {})  # type: ignore[return-value]

    def host_usb_override(self, hostname: str) -> Dict[str, Any]:
        """Per-host USB VMID override for one hostname (empty dict if none)."""
        if not hostname:
            return {}
        return dict(self._host_usb_store().get(hostname, {}) or {})

    def all_host_usb_overrides(self) -> Dict[str, Dict[str, Any]]:
        return {h: dict(v) for h, v in self._host_usb_store().items()}

    def set_host_usb_override(self, hostname: str,
                              knobs: Dict[str, Any]) -> Dict[str, Any]:
        if not hostname:
            return {}
        store = self._host_usb_store()
        cur = dict(store.get(hostname, {}) or {})
        if "vmid_start" in knobs and knobs["vmid_start"] is not None:
            try:
                cur["vmid_start"] = int(knobs["vmid_start"])
            except (TypeError, ValueError):
                pass
        if "vmid_end" in knobs and knobs["vmid_end"] is not None:
            try:
                cur["vmid_end"] = int(knobs["vmid_end"])
            except (TypeError, ValueError):
                pass
        if "vm_set_override" in knobs and knobs["vm_set_override"] is not None:
            cur["vm_set_override"] = _sanitize_vm_set_override(knobs["vm_set_override"])
        store[hostname] = cur
        self._save()
        return dict(cur)

    def clear_host_usb_override(self, hostname: str) -> bool:
        store = self.settings.get("host_usb_overrides", {}) or {}
        if hostname in store:
            store.pop(hostname)
            self._save()
            return True
        return False

    # ── USB config payload (port of _proxmox_usb_config_payload) ───────────

    def _sim_phy_cached(self) -> str:
        """``sim_phy`` from simulation.conf (+ hub-sim-overrides.conf overlay),
        cached and re-parsed only when either file's mtime changes. Read+parse
        ran on EVERY CS_GET_USB_CONFIG (~5s) before — a per-poll disk read +
        configparser on the shared event loop. Two stat() calls (fast metadata)
        replace it when the files are unchanged (the common case)."""
        sim_conf = self.config_dir / "simulation.conf"
        ov_conf = self.config_dir / "hub-sim-overrides.conf"

        def _mtime(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        key = (_mtime(sim_conf), _mtime(ov_conf))
        cache = getattr(self, "_sim_phy_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        sim_phy = "wireless"
        try:
            if sim_conf.exists():
                parser = configparser.ConfigParser()
                parser.read_string(sim_conf.read_text(encoding="utf-8"))
                self._merge_ini_override(parser, ov_conf)
                sim_phy = parser.get("simulation", "sim_phy", fallback="wireless").strip().lower() or "wireless"
        except Exception:
            pass
        if sim_phy not in {"wireless", "ethernet", "any"}:
            sim_phy = "wireless"
        self._sim_phy_cache = (key, sim_phy)
        return sim_phy

    def usb_config_payload(self, hostname: Optional[str] = None) -> Dict[str, Any]:
        """Build the 27-key ``usb_config`` blob the agent provisions from.

        ``sim_phy`` is read from ``configs/simulation.conf`` (with the
        ``hub-sim-overrides.conf`` overlay merged on top) exactly as the legacy
        spoke did; the remaining knobs come from this settings store.
        """
        sim_phy = self._sim_phy_cached()

        vm_set_override = _sanitize_vm_set_override(self.get("vm_set_override", 0))
        img1_spec = self.get("image1_template_spec")
        img2_spec = self.get("image2_template_spec")
        img1_id = int(self.get("image1_template_id", 100) or 100)
        img2_id = int(self.get("image2_template_id", 200) or 200)

        # missing_timeout is stored in MINUTES (the UI label is "Destroy after
        # missing (minutes)"); the pxmx agent compares ``now - missing_since`` in
        # SECONDS, so emit seconds here (minutes × 60). 0 = teardown disabled
        # (preserved — the agent gates on ``missing_timeout > 0``).
        _missing_min = int(self.get("usb_missing_timeout", 60) or 0)
        # protected_vmids: the hub container (VMID 1001) is ALWAYS protected
        # (matches the UI help "VM 1001 is always protected") — merge it into the
        # emitted list so the agent's as-is resolution always includes it.
        _protected = set(_parse_int_ranges(self.get("protected_vmids", ""))) | {1001}

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

        # Per-host override: a non-default vmid_start/vmid_end (or vm_set_override)
        # pinned for this hostname overrides the global values, so the pxmx agent
        # honors it over its own hostname-suffix derivation. (vm_set_override is
        # only meaningful to the agent when vmid_start/vmid_end are at the default.)
        if hostname:
            ov = self.host_usb_override(hostname)
            if "vmid_start" in ov:
                payload["vmid_start"] = int(ov["vmid_start"])
            if "vmid_end" in ov:
                payload["vmid_end"] = int(ov["vmid_end"])
            if "vm_set_override" in ov:
                payload["vm_set_override"] = _sanitize_vm_set_override(ov["vm_set_override"])
        return payload

    @staticmethod
    def _merge_ini_override(parser: configparser.ConfigParser, override_path: Path) -> None:
        if not override_path.exists():
            return
        try:
            ov = configparser.ConfigParser()
            ov.read_string(override_path.read_text(encoding="utf-8"))
            for section in ov.sections():
                if not parser.has_section(section):
                    parser.add_section(section)
                for key, value in ov.items(section):
                    parser.set(section, key, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not apply hub override %s: %s", override_path, exc)


# ── CommandQueue ─────────────────────────────────────────────────────────────

class CommandQueue:
    """Persisted command queue + ack surface for the cs spoke.

    One instance lives on the ``CSSpoke`` module; ``handle_command`` dispatches
    ``CS_QUEUE_COMMAND``/``CS_POLL_AGENT_INBOX``/``CS_ACK_COMMAND``/``CS_GET_USB_CONFIG``
    to it.
    """

    def __init__(self, data_dir: Path, settings: CSSettings,
                 protected_vmids: Optional[Set[int]] = None,
                 sim_vmin: int = SIM_VMIN) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "command_queue.json"
        self.settings = settings
        self.lock = asyncio.Lock()
        self.sim_vmin = sim_vmin
        # Protected set: explicit per-spoke config wins; else the settings store
        # (cs UI) wins; else the hardcoded default so the hub is never unprotected.
        if protected_vmids is not None:
            self.protected_vmids = set(protected_vmids)
        else:
            cfg_set = settings.protected_vmids() if settings else set()
            self.protected_vmids = cfg_set or set(DEFAULT_PROTECTED_VMIDS)
        self.commands: List[Dict[str, Any]] = []
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                if isinstance(loaded, list):
                    self.commands = [c for c in loaded if isinstance(c, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue load failed (%s): %s", self.path, exc)

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(self.commands, default=str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue save failed (%s): %s", self.path, exc)

    async def _asave(self) -> None:
        """Persist off the event loop. The command list is serialized HERE (on
        the loop, so it's a consistent snapshot while callers hold self.lock),
        but the slow atomic disk write is offloaded to a thread. This is the hot
        path — CS_POLL_AGENT_INBOX (~5s) and CS_ACK_COMMAND both save — and a
        synchronous write on a contended disk stalled the whole shared event
        loop (hub connection + uvicorn API), producing the hub's 5s/30s Request
        Timeouts. See logging/observability + cs-svr-02 starvation notes."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.commands, default=str)
            await asyncio.to_thread(_write_atomic, self.path, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue save failed (%s): %s", self.path, exc)

    # ── shape helpers ──────────────────────────────────────────────────────

    def _make_command(self, target: str, action: str, args: Optional[dict],
                      command_type: Optional[str]) -> Dict[str, Any]:
        now = time.time()
        return {
            "id": str(uuid.uuid4()),
            "target": target,
            "action": _normalize_action(action),
            "args": dict(args or {}),
            "type": _normalize_type(command_type),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            # Last REAL agent contact for this command: refreshed by an ACCEPTED
            # touch (CS_TOUCH_COMMAND from the bridge), a progress frame, or the
            # terminal ack. NOT refreshed by the 30s stale re-probe or a requeue,
            # so it ages while the agent is silent and the delete-verify sweep
            # (DELETE_VERIFY_TIMEOUT_SECS) can bound a lost long op. Old persisted
            # commands without this key fall back to created_at via .get().
            "last_contact": now,
            "expires_at": now + COMMAND_EXPIRE_SECS,
            "purge_after": None,
            "result": None,
            "message": None,
            # How many times the hub's CSBridgePoller has re-queued this command
            # after a relay timeout (the agent was too busy to ACCEPT within
            # CS_RELAY_TIMEOUT_S). Re-queue retries up to ``max_retries`` before
            # the command is marked failed — see ``requeue_command``. A fresh
            # command has 0 attempts. Old persisted commands without this key
            # default to 0 via .get().
            "relay_attempts": 0,
        }

    @staticmethod
    def _serialize_for_agent(cmd: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": cmd["id"], "action": cmd["action"],
                "args": cmd.get("args", {}), "type": cmd.get("type")}

    def _find_active_duplicate(self, target: str, action: str,
                               args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sig = _args_signature(args)
        for cmd in self.commands:
            if cmd.get("target") != target or cmd.get("action") != action:
                continue
            if cmd.get("status") not in {"pending", "delivered"}:
                continue
            if _args_signature(cmd.get("args", {})) == sig:
                return cmd
        return None

    def _cleanup(self, now: Optional[float] = None) -> Tuple[int, int]:
        now = now if now is not None else time.time()
        expired = 0
        for cmd in self.commands:
            if cmd.get("status") in {"pending", "delivered"} and \
                    (now - float(cmd.get("created_at", now))) > COMMAND_EXPIRE_SECS:
                cmd["status"] = "expired"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                expired += 1
        before = len(self.commands)
        self.commands[:] = [
            cmd for cmd in self.commands
            if cmd.get("status") not in {"completed", "failed", "expired"}
            or now < float(cmd.get("purge_after") or
                           cmd.get("updated_at", cmd.get("created_at", now)) +
                           COMMAND_RESULT_RETENTION_SECS)
        ]
        purged = before - len(self.commands)
        self._trim()
        return expired, purged

    def _trim(self) -> None:
        if len(self.commands) <= COMMAND_MAX:
            return
        terminal = {"completed", "failed", "expired"}
        idx = 0
        while len(self.commands) > COMMAND_MAX and idx < len(self.commands):
            if self.commands[idx].get("status") in terminal:
                del self.commands[idx]
                continue
            idx += 1
        if len(self.commands) > COMMAND_MAX:
            del self.commands[:len(self.commands) - COMMAND_MAX]

    # ── safeguard ──────────────────────────────────────────────────────────

    def _refused(self, target: str, action: str, args: Dict[str, Any]) -> Optional[str]:
        """Return a refusal reason if the command must not be enqueued, else None."""
        if target != "proxmox" or action not in _VM_ACTIONS:
            return None
        vmid = args.get("vmid")
        try:
            v = int(vmid)
        except (TypeError, ValueError):
            return None  # agent validates; do not block ambiguous enqueue
        if v in self.protected_vmids:
            return f"VM {v} is protected (hub/system container)"
        if v < self.sim_vmin:
            return f"VM {v} outside Client-Simulation range (>= {self.sim_vmin})"
        return None

    # ── public API (called from CSSpoke.handle_command) ────────────────────

    async def enqueue(self, target: str, action: str,
                      args: Optional[Dict[str, Any]] = None,
                      command_type: Optional[str] = None) -> Dict[str, Any]:
        """Idempotently enqueue a command. Returns
        ``{command, created, expired, purged}``. Raises ``ValueError`` if the
        safeguard refuses the command (so the spoke surfaces a 403-style ERROR)."""
        async with self.lock:
            ntarget = _normalize_hostname(target) or str(target or "").strip()
            naction = _normalize_action(action)
            ntype = _normalize_type(command_type)
            nargs = dict(args or {})

            reason = self._refused("proxmox" if ntarget == "proxmox" else ntarget,
                                   naction, nargs)
            if reason:
                raise ValueError(reason)

            now = time.time()
            expired, purged = self._cleanup(now)
            existing = self._find_active_duplicate(ntarget, naction, nargs)
            if existing is not None:
                return {"command": existing, "created": False,
                        "expired": expired, "purged": purged}

            cmd = self._make_command(ntarget, naction, nargs, ntype)
            self.commands.append(cmd)
            self._trim()
            await self._asave()
            return {"command": cmd, "created": True,
                    "expired": expired, "purged": purged}

    def _command_matches_agent(self, cmd: Dict[str, Any], hostname: str) -> bool:
        if cmd.get("target") == hostname:
            return True
        if cmd.get("target") == "proxmox":
            return True
        return _hostnames_match(cmd.get("target", ""), hostname)

    async def poll_agent_inbox(self, hostname: str,
                                max_retries: int = 0) -> Dict[str, Any]:
        """Reset stale delivered→pending, cleanup, return serialized pending for
        this host, and mark them delivered. Returns
        ``{commands, expired, purged, reset, delivered, gave_up}``.

        ``max_retries`` caps the total re-send attempts for a command that's
        delivered but never acked (no CS_COMMAND_RESULT, no progress frames —
        the agent lost it or crashed mid-op). Each stale reset increments
        ``relay_attempts`` (shared with the relay-timeout requeue path); once
        it reaches ``max_retries`` the command is marked ``failed`` instead of
        re-sent — "re-send up to X times then give up", the same budget as a
        relay timeout. ``max_retries <= 0`` preserves the old unbounded
        re-send behavior (re-send every STALE_DELIVERED_SECS until the command
        expires). A running long op that streams CS_PROGRESS never hits this:
        ``touch_command`` refreshes ``updated_at`` on each progress frame."""
        hn = _normalize_hostname(hostname)
        async with self.lock:
            now = time.time()
            expired, purged = self._cleanup(now)

            # Reset stale delivered (>STALE_DELIVERED_SECS, no ack) → pending,
            # capped at max_retries (shared with the relay-timeout requeue
            # budget) — a command that's been re-sent max_retries times with
            # no ack is marked failed, not re-sent forever.
            reset = 0
            gave_up = 0
            for cmd in self.commands:
                if cmd.get("status") == "delivered" and \
                        (now - float(cmd.get("updated_at", now))) > STALE_DELIVERED_SECS and \
                        self._command_matches_agent(cmd, hn):
                    # delete_vm / reclone_vm: the 30s reset is a non-budgeted
                    # RE-PROBE ("is the agent back yet?"), not a give-up step.
                    # The few-minutes bound is owned by the verify-sweep below
                    # (which uses last_contact, not updated_at) so these periodic
                    # re-probes don't consume the retry budget or refresh the
                    # verify window. Other actions keep the old bounded behavior.
                    if cmd.get("action") in _VERIFY_ACTIONS:
                        cmd["status"] = "pending"
                        cmd["updated_at"] = now
                        reset += 1
                        continue
                    attempts = int(cmd.get("relay_attempts", 0)) + 1
                    cmd["relay_attempts"] = attempts
                    if max_retries > 0 and attempts >= max_retries:
                        # Exhausted the re-send budget — give up (terminal).
                        cmd["status"] = "failed"
                        cmd["message"] = (f"no ack after {attempts} "
                                          f"re-send attempt(s) — gave up")
                        cmd["updated_at"] = now
                        cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                        gave_up += 1
                    else:
                        cmd["status"] = "pending"
                        cmd["updated_at"] = now
                        reset += 1

            # Verified-report safety net for delete_vm / reclone_vm: a delivered
            # long op with no REAL agent contact (ACCEPTED touch / progress /
            # terminal → last_contact) for longer than DELETE_VERIFY_TIMEOUT_SECS
            # is LOST. Requeue it (bounded) so it genuinely re-runs — the agent
            # re-spawns a dead task (liveness-aware dedup) instead of dedup-
            # suppressing forever. last_contact is NOT refreshed by the 30s
            # re-probe above, so a silent agent ages toward this bound while a
            # slow-but-alive delete (re-acked every 30s → touched) does not.
            verify_requeued = 0
            verify_gave_up = 0
            for cmd in self.commands:
                if cmd.get("status") != "delivered" or \
                        cmd.get("action") not in _VERIFY_ACTIONS or \
                        not self._command_matches_agent(cmd, hn):
                    continue
                last_contact = float(cmd.get("last_contact",
                                             cmd.get("updated_at", now)))
                if (now - last_contact) <= DELETE_VERIFY_TIMEOUT_SECS:
                    continue
                attempts = int(cmd.get("relay_attempts", 0)) + 1
                cmd["relay_attempts"] = attempts
                if attempts < DELETE_VERIFY_MAX_RETRIES:
                    cmd["status"] = "pending"
                    cmd["message"] = (f"no verified report after "
                                      f"{DELETE_VERIFY_TIMEOUT_SECS}s — requeuing "
                                      f"(attempt {attempts}/{DELETE_VERIFY_MAX_RETRIES})")
                    cmd["updated_at"] = now
                    # Reset the verify window so the re-send gets a fresh
                    # DELETE_VERIFY_TIMEOUT_SECS to earn a touch/terminal before
                    # the next attempt — otherwise the sweep would fire on the
                    # very next poll (last_contact still old) and exhaust the
                    # budget in seconds, not minutes.
                    cmd["last_contact"] = now
                    verify_requeued += 1
                else:
                    cmd["status"] = "failed"
                    cmd["message"] = (f"no verified report after {attempts} "
                                      f"attempt(s) — gave up")
                    cmd["updated_at"] = now
                    cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                    verify_gave_up += 1

            pending = [c for c in self.commands
                       if c.get("status") == "pending" and self._command_matches_agent(c, hn)]
            delivered_ids = [c["id"] for c in pending]
            for cmd in self.commands:
                if cmd["id"] in set(delivered_ids) and cmd.get("status") == "pending":
                    cmd["status"] = "delivered"
                    cmd["updated_at"] = now

            if reset or delivered_ids or expired or purged or gave_up \
                    or verify_requeued or verify_gave_up:
                await self._asave()

            return {
                "commands": [self._serialize_for_agent(c) for c in pending],
                "expired": expired,
                "purged": purged,
                "reset": reset,
                "delivered": delivered_ids,
                "gave_up": gave_up,
                "verify_requeued": verify_requeued,
                "verify_gave_up": verify_gave_up,
            }

    async def ack_command(self, cmd_id: str, status: str,
                          message: Any = None, result: Any = None) -> Dict[str, Any]:
        """Idempotent terminal update. ``status`` must be ``completed`` or
        ``failed`` (long-op ``CS_COMMAND_RESULT`` in Phase E maps here)."""
        status = str(status or "").strip().lower()
        if status not in ("completed", "failed"):
            return {"ok": False, "message": "status must be 'completed' or 'failed'"}
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            self._cleanup()
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            cmd["status"] = status
            cmd["message"] = str(message) if message is not None else (cmd.get("message") or "")
            cmd["result"] = result if result is not None else cmd.get("result")
            cmd["updated_at"] = time.time()
            cmd["last_contact"] = cmd["updated_at"]
            cmd["purge_after"] = cmd["updated_at"] + COMMAND_RESULT_RETENTION_SECS
            await self._asave()
            return {"ok": True, "id": cmd_id, "status": status}

    async def requeue_command(self, cmd_id: str, max_retries: int = 5,
                              message: Optional[str] = None) -> Dict[str, Any]:
        """Re-queue a ``delivered`` command whose hub→agent relay TIMED OUT
        (the agent was too busy to ACCEPT within ``CS_RELAY_TIMEOUT_S``) instead
        of marking it dead. Bounded: increments ``relay_attempts`` and resets
        status → ``pending`` so the CSBridgePoller's next tick re-relays it,
        up to ``max_retries``. Once ``relay_attempts`` reaches ``max_retries``
        the command is marked ``failed`` (terminal) with the timeout message —
        this is the "retry 5 then give up" the operator asked for, instead of a
        single relay timeout killing a mass-delete on a busy agent.

        Idempotent-ish: a command that isn't currently ``delivered`` (already
        completed/failed/expired, or pending) is left alone (returns its
        current status) — the bridge may call this twice for the same timeout
        if a re-deliver races a requeue. ``max_retries <= 0`` means "never
        retry" → the first timeout fails the command (preserves the old
        behavior for a site that wants fail-fast)."""
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            self._cleanup()
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            cur = str(cmd.get("status", "")).lower()
            if cur in ("completed", "failed", "expired"):
                return {"ok": True, "id": cmd_id, "status": cur,
                        "requeued": False, "attempts": int(cmd.get("relay_attempts", 0))}
            attempts = int(cmd.get("relay_attempts", 0)) + 1
            cmd["relay_attempts"] = attempts
            now = time.time()
            if max_retries > 0 and attempts < max_retries:
                cmd["status"] = "pending"
                cmd["updated_at"] = now
                await self._asave()
                return {"ok": True, "id": cmd_id, "status": "pending",
                        "requeued": True, "attempts": attempts,
                        "max_retries": max_retries}
            # Exhausted retries (or max_retries<=0 fail-fast) → terminal failed.
            cmd["status"] = "failed"
            cmd["message"] = (message or "relay timed out — gave up after "
                              f"{attempts} attempt(s)")
            cmd["updated_at"] = now
            cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
            await self._asave()
            return {"ok": True, "id": cmd_id, "status": "failed",
                    "requeued": False, "attempts": attempts,
                    "max_retries": max_retries}

    async def touch_command(self, cmd_id: str,
                             message: Optional[str] = None) -> Dict[str, Any]:
        """Refresh a ``delivered`` command's ``updated_at`` on a CS_INGEST_PROGRESS
        tick so the ``STALE_DELIVERED_SECS`` reset doesn't re-send an in-flight
        long op (delete_vm / reclone_vm can take minutes; without this, a
        delivered command with no ack for 30s is reset to ``pending`` and
        re-relayed — duplicating the op while the first one is still running,
        the "some don't get deleted / queue grows" symptom on slow storage).

        Only refreshes a ``delivered`` command (a running long op); ``pending``
        (not yet relayed) and terminal (``completed``/``failed``/``expired``)
        are left alone. In-memory only — does NOT persist: a progress frame
        lands every second or so during a long op, and an atomic disk write per
        frame would be a write storm on the hot CS_POLL_AGENT_INBOX path. The
        next ``poll_agent_inbox`` (5s) saves, and the stale reset reads
        ``updated_at`` in-memory, which is exactly what this refreshes. A
        crash reverts to the last persisted ``updated_at`` — worst case a
        single re-send on restart, which the agent's ``_pending_delete_vmids``
        guard dedups."""
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            if cmd.get("status") != "delivered":
                return {"ok": True, "id": cmd_id, "status": cmd.get("status"),
                        "touched": False}
            now = time.time()
            cmd["updated_at"] = now
            # Real agent contact (ACCEPTED / progress) → refresh last_contact,
            # the timestamp the delete-verify sweep watches.
            cmd["last_contact"] = now
            if message:
                cmd["message"] = str(message)
            return {"ok": True, "id": cmd_id, "status": "delivered",
                    "touched": True}

    async def list_commands(self) -> List[Dict[str, Any]]:
        async with self.lock:
            self._cleanup()
            # Hand back copies so callers can't mutate the live list.
            return [dict(c) for c in self.commands]

    async def clear_commands(self, target: Optional[str] = None) -> Dict[str, Any]:
        """Cancel (expire) all non-terminal commands, optionally scoped to a
        target. Mirrors the legacy ``DELETE /api/commands`` (cancel-all) and
        ``DELETE /api/commands/pending?target=`` (pre-teardown expiry so
        in-flight commands don't fire against a gone VM). Terminal commands
        (completed/failed/expired) are left for their retention window."""
        async with self.lock:
            now = time.time()
            ntarget = _normalize_hostname(target) if target else None
            cleared = 0
            for cmd in self.commands:
                if cmd.get("status") not in {"pending", "delivered"}:
                    continue
                if ntarget and not self._command_matches_agent(cmd, ntarget):
                    continue
                cmd["status"] = "expired"
                cmd["message"] = "cleared by operator"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                cleared += 1
            if cleared:
                await self._asave()
            return {"cleared": cleared, "remaining": len(self.commands)}

    async def delete_command(self, cmd_id: str) -> Dict[str, Any]:
        """Remove a single command (any status). Mirrors the legacy
        ``DELETE /api/commands/{cmd_id}`` per-row delete."""
        cmd_id = str(cmd_id or "").strip()
        if not cmd_id:
            return {"ok": False, "message": "missing 'id'"}
        async with self.lock:
            before = len(self.commands)
            self.commands[:] = [c for c in self.commands if c.get("id") != cmd_id]
            removed = before - len(self.commands)
            if removed:
                await self._asave()
            return {"ok": bool(removed), "id": cmd_id, "removed": removed}

    async def get_usb_config(self, hostname: Optional[str] = None) -> Dict[str, Any]:
        return self.settings.usb_config_payload(hostname)


# ── small parsing helpers (ports of legacy setting coercions) ───────────────

def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _setting_int(value: Any, minimum: Optional[int] = None) -> int:
    try:
        n = int(str(value).strip())
    except Exception:
        n = 0
    if minimum is not None and n < minimum:
        n = minimum
    return n


def _setting_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _normalize_toggle(value: Any) -> str:
    return "on" if _setting_bool(value) else "off"


def _sanitize_vm_set_override(value: Any) -> int:
    try:
        n = int(str(value or "0").strip() or "0")
    except Exception:
        n = 0
    return max(0, n)


def _parse_int_ranges(raw: Any) -> Set[int]:
    """Parse ``"1001,1000-1002"`` into ``{1000,1001,1002}`` (legacy format)."""
    out: Set[int] = set()
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
                out.update(range(min(lo, hi), max(lo, hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(token))
            except ValueError:
                continue
    return out