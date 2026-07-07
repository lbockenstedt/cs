from __future__ import annotations

import asyncio
import configparser
import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import logging
import os
import pty
import random
import re
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import termios
import time
import traceback
import uuid
import zlib
from dataclasses import asdict, dataclass

import acme as spoke_acme
import state
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    _HTTPX_AVAILABLE = False

try:
    import websockets
    from websockets.exceptions import InvalidStatus as WebSocketInvalidStatus
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    websockets = None
    WebSocketInvalidStatus = Exception
    _WEBSOCKETS_AVAILABLE = False

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
configure_logging()
logger = logging.getLogger("client_sim_dashboard")
if not _HTTPX_AVAILABLE:
    logger.warning("httpx not installed — network-backed features may be limited. Install with: pip install httpx")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SETTINGS_FILE = BASE_DIR / "settings.json"
STATE_CACHE_FILE = BASE_DIR / "state_cache.json"
COMMAND_QUEUE_FILE = BASE_DIR / "command_queue.json"
RECLONE_STATE_FILE = BASE_DIR / "reclone_state.json"
RELAY_STATE_FILE = BASE_DIR / "relay_state.json"
UPDATE_STATE_FILE = BASE_DIR / "update_state.json"
VM_WATCHDOG_FILE = BASE_DIR / "vm_watchdog.json"
RESOURCE_CACHE_FILE = BASE_DIR / "resource_cache.json"
HISTORY_FILE = BASE_DIR / "central_history.jsonl"
CLIENT_HISTORY_FILE = BASE_DIR / "client_history.json"
CLIENT_COUNT_BASELINE_FILE = BASE_DIR / "client_count_baseline.json"
CLIENT_COUNT_7DAY_FILE = BASE_DIR / "client_count_7day.json"
CLIENT_COUNT_7DAY_WINDOW = 7 * 24 * 3600   # 7 days of hourly history
CLIENT_HISTORY_DAYS = 7          # remove clients not seen within this many days
CLIENT_SAVE_INTERVAL = 60        # seconds between periodic disk saves
REPO_DIR = Path(os.getenv("REPO_DIR", "/app/client-sim")).resolve()
REPO_URL = os.getenv("REPO_URL", "https://github.com/lbockenstedt/cs.git")
CLIENT_SIM_REPO_RAW = os.getenv("CLIENT_SIM_REPO_RAW", "https://raw.githubusercontent.com/lbockenstedt/cs")
CS_WEBUI_REPO_RAW = os.getenv("CS_WEBUI_REPO_RAW", "https://raw.githubusercontent.com/solutions-hpe/cs-webui")


def _detect_own_vmid() -> int | None:
    """Detect this process's Proxmox container/VM ID from /proc/self/cgroup.
    Returns the integer VMID if running inside a Proxmox LXC container, else None."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        m = re.search(r'/(?:lxc|qemu)/(\d+)', cgroup)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


WEBUI_VMID: int | None = _detect_own_vmid()

# VMIDs that must never be started, stopped, rebooted, recloned, snapshotted,
# or deleted through this UI.  1001 is the conventional spoke-container VMID;
# WEBUI_VMID covers whatever container we detect ourselves running in.
_HARDCODED_PROTECTED_VMIDS: frozenset[int] = frozenset({1001})


# Pure VMID / template-spec parsing helpers moved to services/vmid.py
from services.vmid import (  # noqa: E402
    _parse_protected_vmids,
    _TEMPLATE_VMID_RANGE_CAP,
    _normalize_vmid_spec,
    _parse_vmid_spec,
    _template_spec_key,
    _template_id_key,
    _legacy_template_id,
    _resolved_template_spec,
    _primary_template_id,
    _validate_template_specs,
)


def _is_protected_vmid(vmid: int | str | None) -> bool:
    """Return True if this VMID must never be touched by any UI action."""
    if vmid is None:
        return False
    try:
        v = int(vmid)
    except (TypeError, ValueError):
        return False
    if v in _HARDCODED_PROTECTED_VMIDS:
        return True
    if WEBUI_VMID is not None and v == WEBUI_VMID:
        return True
    # Check user-configured protected VMIDs (individual IDs or ranges)
    raw = str(settings.get("protected_vmids", "") or "")
    for entry in _parse_protected_vmids(raw):
        if isinstance(entry, tuple):
            lo, hi = entry
            if lo <= v <= hi:
                return True
        elif entry == v:
            return True
    return False

# ── Credential encryption ─────────────────────────────────────────────────────
# Fernet symmetric encryption for sensitive fields in settings.json.
# Key is generated once at install time and stored in .secret_key (chmod 600).
# Falls back to plaintext if key file or cryptography package is unavailable.
from services.credential_store import (  # noqa: E402
    _ENC_PREFIX,
    _SENSITIVE_CFG_KEYS,
    _SENSITIVE_CLASSIC_API_KEYS,
    _SENSITIVE_CENTRAL_API_KEYS,
    _SENSITIVE_TOP_KEYS,
    _SENSITIVE_TOP_DICT_KEYS,
    _SENSITIVE_NOTIF_KEYS,
    _fernet,
    _encrypt_secret,
    _decrypt_secret,
    _encrypt_settings,
    _decrypt_settings,
)


# Installer version — written by install-lxc.sh at install time
_version_file = BASE_DIR / "INSTALLER_VERSION"
INSTALLER_VERSION: str = _version_file.read_text().strip() if _version_file.exists() else "dev"
# App version — from VERSION file in repo root
_app_version_file = BASE_DIR / "VERSION"
APP_VERSION: str = _app_version_file.read_text().strip() if _app_version_file.exists() else INSTALLER_VERSION


class UpstreamJSONError(RuntimeError):
    """Raised when an upstream service returns malformed JSON."""


REPO_BRANCH = os.getenv("REPO_BRANCH", "main")
OFFLINE_TIMEOUT = int(os.getenv("OFFLINE_TIMEOUT", "300"))
# Max error entries kept per client in memory.
# WHY: errors accumulate over a long run; capping prevents unbounded memory growth.
MAX_CLIENT_ERRORS = 50
SYNC_INTERVAL = 300
HEARTBEAT_INTERVAL = 30
RELAY_INTERVAL_DEFAULT = 5    # base interval in seconds
CENTRAL_POLL_INTERVAL = 900   # 15 minutes
HUB_RELAY_KEYS = {
    "relay_server_url",
    "relay_api_key",
    "relay_tenant_id",
    "relay_onboarding_psk",
    "relay_spoke_id",
    "relay_spoke_hostname",
    "relay_spoke_name",
    "hub_tls_verify",
    "hub_isolation_timeout",  # Allow the isolation timeout through the relay settings gate because operators configure this safeguard from the Hub setup card.
}
HUB_LOCAL_ALLOWED_KEYS = HUB_RELAY_KEYS | {"relay_tenant_hint"}
# Keys that the hub UI owns and may clear by omitting them from a config_update push.
# When a key is absent from the hub's config payload but was previously set, the spoke
# should reset it to default so stale values don't persist after an operator clears a field.
# Mirrors HUB_CONFIG_FIELDS in the hub's dashboard.js.
HUB_CONFIG_OWNED_KEYS: frozenset[str] = frozenset({
    "repo_branch", "reclone_schedule_enabled", "reclone_schedule_cron", "reclone_concurrency",
    "vm_image_1_template_id", "vm_image_1_template_spec", "vm_image_2_template_id", "vm_image_2_template_spec", "vm_image_1_pct",
    "usb_auto_provision", "usb_missing_timeout", "usb_max_slots", "vm_silent_timeout",
    "l1_vlan_start", "l1_vlan_end", "usb_vidpids", "usb_ignored_vidpids", "ignored_hostnames",
    "guest_agent_watchdog_enabled", "guest_agent_grace_minutes",
    "guest_agent_check_interval_minutes", "guest_agent_reboot_after_minutes",
    "guest_agent_reclone_after_minutes",
    "watchdog_reboot_enabled",
})
HUB_NOTIFICATION_KEY_MAP = {
    "teams_webhook_url": "teams_webhook_url",
    "smtp_host": "smtp_host",
    "smtp_port": "smtp_port",
    "smtp_user": "smtp_user",
    "smtp_password": "smtp_password",
    "smtp_from": "smtp_from",
    "smtp_to": "smtp_to",
}
HISTORY_HOURS = 24
UPDATE_CHECK_INTERVAL = 86400  # 24 hours
VM_WATCHDOG_TIMEOUT_SECS = 86400
VM_WATCHDOG_INTERVAL_SECS = 1800

# Self-update: the installer lives inside the synced repo
_INSTALLER_PATH = REPO_DIR / "install-lxc.sh"

update_state: dict[str, Any] = {
    "current_version": INSTALLER_VERSION,
    "available_version": None,
    "update_available": False,
    "last_checked": None,
    "update_in_progress": False,
    "update_log": [],
    "update_error": None,
    "cswebui_current": APP_VERSION,
    "cswebui_available": None,
}


# ── Runtime settings (persisted to settings.json) ────────────────────────────
def _load_persisted_settings() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return _decrypt_settings(raw)
    except Exception:
        return {}


state._settings_cache = {}
state._settings_cache_time = 0.0
_SETTINGS_CACHE_TTL: float = 30.0  # seconds


def _invalidate_settings_cache() -> None:
    state._settings_cache = {}
    state._settings_cache_time = 0.0


def _save_settings() -> None:
    _invalidate_settings_cache()
    try:
        SETTINGS_FILE.write_text(json.dumps(_encrypt_settings(settings), indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist settings to %s: %s", SETTINGS_FILE, exc)


def _get_machine_id() -> str:
    """Return a stable machine identifier for clone detection (Linux/LXC)."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            val = Path(path).read_text().strip()
            if val:
                return val
        except OSError:
            pass
    return ""


def _candidate_relay_spoke_id(persisted: dict[str, Any]) -> str:
    return str(
        persisted.get("relay_spoke_id")
        or persisted.get("relay_island_id")
        or persisted.get("relay_site_id")
        or os.getenv("SPOKE_ID", "")
        or ""
    ).strip()


def _is_uuid(value: str) -> bool:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return False
    try:
        return str(uuid.UUID(candidate)) == candidate
    except (TypeError, ValueError, AttributeError):
        return False


def _relay_spoke_id_needs_rotation(value: str, persisted: dict[str, Any] | None = None) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return True
    if _is_uuid(candidate):
        # Detect container clone: rotate if hostname OR machine-id changed.
        persisted = persisted or _persisted
        current_hostname = socket.gethostname()
        stored_hostname = str(persisted.get("relay_spoke_hostname") or "").strip()
        if stored_hostname and stored_hostname != current_hostname:
            logger.warning(
                "Spoke hostname changed from '%s' to '%s' — rotating spoke_id to avoid duplicate IDs after clone",
                stored_hostname, current_hostname,
            )
            return True
        # Check machine-id (works on Linux/LXC; catches same-hostname clones)
        current_machine_id = _get_machine_id()
        stored_machine_id = str(persisted.get("relay_machine_id") or "").strip()
        if current_machine_id and stored_machine_id and stored_machine_id != current_machine_id:
            logger.warning(
                "Machine ID changed — rotating spoke_id to avoid duplicate IDs after clone"
            )
            return True
        return False

    persisted = persisted or _persisted
    bad_values = {"main", "master"}
    repo_branch = str(persisted.get("repo_branch") or REPO_BRANCH or "").strip().lower()
    if repo_branch:
        bad_values.add(repo_branch)
    version_values = {str(APP_VERSION).strip().lower(), str(INSTALLER_VERSION).strip().lower()}
    lowered = candidate.lower()
    if lowered in bad_values:
        logger.warning("Rotating invalid relay_spoke_id '%s' that matched a branch name", candidate)
    elif lowered in version_values or re.fullmatch(r"\d+(?:\.\d+)+", candidate):
        logger.warning("Rotating invalid relay_spoke_id '%s' that matched a version string", candidate)
    else:
        logger.warning("Rotating invalid non-UUID relay_spoke_id '%s'", candidate)
    return True


def _ensure_relay_spoke_id(persisted: dict[str, Any] | None = None) -> str:
    persisted = persisted or _persisted
    candidate = str(settings.get("relay_spoke_id") or _candidate_relay_spoke_id(persisted)).strip()
    if _relay_spoke_id_needs_rotation(candidate, persisted):
        candidate = str(uuid.uuid4())
    machine_id = _get_machine_id()
    if settings.get("relay_spoke_id") != candidate or _candidate_relay_spoke_id(persisted) != candidate:
        settings["relay_spoke_id"] = candidate
        settings["relay_spoke_hostname"] = socket.gethostname()
        if machine_id:
            settings["relay_machine_id"] = machine_id
        _save_settings()
    else:
        settings["relay_spoke_id"] = candidate
        # Ensure hostname and machine_id are recorded even if spoke_id was already correct
        changed = False
        if not settings.get("relay_spoke_hostname"):
            settings["relay_spoke_hostname"] = socket.gethostname()
            changed = True
        if machine_id and not settings.get("relay_machine_id"):
            settings["relay_machine_id"] = machine_id
            changed = True
        if changed:
            _save_settings()
    return candidate


# ── State snapshot cache (JSON file, no DB) ──────────────────────────────────
state._state_cache_last_save = 0.0
STATE_CACHE_MIN_INTERVAL = 10.0  # max one write per 10 s

# WS delta: skip proxmox broadcast when payload hasn't changed
state._last_proxmox_hash = ""

# INI cache: avoid re-parsing simulation.conf + client-setup.conf on every request
_sim_conf_cache: dict[str, Any] = {
    "sim_mtime": -1.0,
    "client_mtime": -1.0,
    "simulations": {},
}

# mtime-keyed cache for api_sim_clients' conf-derived view (wsite, central_site,
# and the configured-client dict) — api_sim_clients re-reads + re-parses both
# simulation.conf and client-setup.conf on every call. The live heartbeat
# overlay + Central fetch stay live (not cached); only the parts derived purely
# from the conf files are memoized, invalidated when either conf's mtime moves.
# Keyed by sim_id; entries: {sim_id: (sim_mtime, client_mtime, {wsite, central_site, configured})}.
_sim_clients_cache: dict[str, Any] = {}

# Raw text cache for sim_conf_content sent in telemetry.
# Refreshed by _sim_conf_content_refresh_loop every 30s in a thread-pool worker
# so reads never block the event loop or the telemetry_loop task.
_sim_conf_content_cache: dict[str, Any] = {"content": "", "mtime_ns": -1, "error": None}
_user_overrides_conf_content_cache: dict[str, Any] = {"content": "", "mtime_ns": -1, "error": None}


def _refresh_sim_conf_content() -> None:
    """Stat + conditionally re-read simulation.conf into _sim_conf_content_cache.
    Designed to run inside asyncio.to_thread — never call from the event loop directly."""
    path = REPO_DIR / "configs" / "simulation.conf"
    try:
        st = path.stat()
        if st.st_mtime_ns != _sim_conf_content_cache["mtime_ns"]:
            _sim_conf_content_cache["content"] = path.read_text(encoding="utf-8")
            _sim_conf_content_cache["mtime_ns"] = st.st_mtime_ns
            _sim_conf_content_cache["error"] = None
    except FileNotFoundError:
        _sim_conf_content_cache["content"] = ""
        _sim_conf_content_cache["mtime_ns"] = -1
        _sim_conf_content_cache["error"] = None
    except Exception as exc:
        _sim_conf_content_cache["error"] = str(exc)


def _refresh_user_overrides_conf_content() -> None:
    """Stat + conditionally re-read user-overrides.conf into _user_overrides_conf_content_cache.
    Designed to run inside asyncio.to_thread — never call from the event loop directly."""
    path = REPO_DIR / "configs" / "user-overrides.conf"
    try:
        st = path.stat()
        if st.st_mtime_ns != _user_overrides_conf_content_cache["mtime_ns"]:
            _user_overrides_conf_content_cache["content"] = path.read_text(encoding="utf-8")
            _user_overrides_conf_content_cache["mtime_ns"] = st.st_mtime_ns
            _user_overrides_conf_content_cache["error"] = None
    except FileNotFoundError:
        _user_overrides_conf_content_cache["content"] = ""
        _user_overrides_conf_content_cache["mtime_ns"] = -1
        _user_overrides_conf_content_cache["error"] = None
    except Exception as exc:
        _user_overrides_conf_content_cache["error"] = str(exc)


async def _sim_conf_content_refresh_loop() -> None:
    """Background task: keep _sim_conf_content_cache fresh without blocking the event loop."""
    while True:
        try:
            await asyncio.to_thread(_refresh_sim_conf_content)
        except Exception as exc:
            _sim_conf_content_cache["error"] = str(exc)
        await asyncio.sleep(30)


async def _user_overrides_conf_content_refresh_loop() -> None:
    """Background task: keep _user_overrides_conf_content_cache fresh without blocking the event loop."""
    while True:
        try:
            await asyncio.to_thread(_refresh_user_overrides_conf_content)
        except Exception as exc:
            _user_overrides_conf_content_cache["error"] = str(exc)
        await asyncio.sleep(30)


def _atomic_write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=indent, default=str), encoding="utf-8")
    tmp_path.replace(path)


def _write_atomic_str(path: Path, serialized: str) -> None:
    """Write a pre-serialised JSON string atomically using a unique tmp file.
    Safe to call from a thread-pool worker (unique tmp name avoids races when
    multiple saves for the same file are dispatched concurrently)."""
    import uuid as _uuid_mod
    tmp = path.with_name(f"{path.name}.{_uuid_mod.uuid4().hex}.tmp")
    try:
        tmp.write_text(serialized, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _merge_ini_override(parser: "configparser.ConfigParser", override_path: "Path") -> None:
    """Merge a hub-managed override .conf file on top of an already-parsed INI parser.

    Keys and sections in the override file take precedence over whatever the
    base file loaded.  The override file uses the same INI format as
    simulation.conf / user-overrides.conf — no format changes.
    """
    if not override_path.exists():
        return
    try:
        override_parser = configparser.ConfigParser()
        override_parser.read_string(override_path.read_text(encoding="utf-8"))
        for section in override_parser.sections():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in override_parser.items(section):
                parser.set(section, key, value)
    except Exception as exc:
        logger.warning("Could not apply hub override %s: %s", override_path, exc)


def _save_relay_state() -> None:
    try:
        _atomic_write_json(RELAY_STATE_FILE, relay_state)
    except Exception as exc:
        logger.warning("Could not persist relay state to %s: %s", RELAY_STATE_FILE, exc)


def _load_relay_state() -> None:
    try:
        if not RELAY_STATE_FILE.exists():
            return
        raw = json.loads(RELAY_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("relay state must be an object")
        for key in relay_state:
            if key in raw:
                relay_state[key] = raw[key]
        relay_state["connected"] = False
        state.relay_registration_refresh_needed = bool(relay_state.get("enabled"))
        _save_relay_state()
        logger.info("Restored relay state from disk")
    except Exception as exc:
        logger.warning("Could not load relay state: %s", exc)


def _normalize_relay_enabled(value: Any) -> str:
    if isinstance(value, str):
        return "on" if value.lower() == "on" else "off"
    return "on" if value else "off"


def _clamp_relay_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = RELAY_INTERVAL_DEFAULT
    return max(5, min(86400, interval))


def _relay_registration_status_from_settings() -> str:
    relay_on = settings.get("relay_enabled") == "on" and bool(settings.get("relay_server_url"))
    if not relay_on:
        return "unregistered"
    if settings.get("relay_api_key") and settings.get("relay_tenant_id"):
        return "approved"
    if settings.get("relay_spoke_id"):
        return "pending"
    return "unregistered"


def _default_central_api_settings() -> dict[str, Any]:
    return {
        "mode": "classic",
        "classic": {
            "url": "",
            "username": "",
            "password": "",
        },
        "central": {
            "url": "",
            "client_id": "",
            "client_secret": "",
            "customer_id": "",
        },
    }


def _central_runtime_defaults() -> dict[str, str]:
    return {
        "api_version": "classic",
        "cluster_url": "",
        "access_token": "",
        "refresh_token": "",
        "client_id": "",
        "client_secret": "",
        "customer_id": "",
    }


def _normalize_central_api_settings(raw: Any, legacy: Any = None) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    legacy_cfg = legacy if isinstance(legacy, dict) else {}
    defaults = _default_central_api_settings()
    raw_classic = data.get("classic") if isinstance(data.get("classic"), dict) else {}
    raw_central = data.get("central") if isinstance(data.get("central"), dict) else {}

    mode = str(data.get("mode") or ("central" if legacy_cfg.get("api_version") == "new_central" else "classic")).strip().lower()
    if mode not in {"classic", "central"}:
        mode = "classic"

    legacy_central = legacy_cfg if legacy_cfg.get("api_version") == "new_central" else {}
    return {
        "mode": mode,
        "classic": {
            "url": str(raw_classic.get("url", defaults["classic"]["url"])).strip(),
            "username": str(raw_classic.get("username", defaults["classic"]["username"])).strip(),
            "password": str(raw_classic.get("password", defaults["classic"]["password"])),
        },
        "central": {
            "url": str(raw_central.get("url", legacy_central.get("cluster_url", defaults["central"]["url"]))).strip(),
            "client_id": str(raw_central.get("client_id", legacy_central.get("client_id", defaults["central"]["client_id"]))).strip(),
            "client_secret": str(raw_central.get("client_secret", legacy_central.get("client_secret", defaults["central"]["client_secret"]))),
            "customer_id": str(raw_central.get("customer_id", legacy_central.get("customer_id", defaults["central"]["customer_id"]))).strip(),
        },
    }


def _build_runtime_central_config(central_api_cfg: dict[str, Any], legacy_cfg: Any = None) -> dict[str, str]:
    legacy = {**_central_runtime_defaults(), **(legacy_cfg if isinstance(legacy_cfg, dict) else {})}
    mode = str(central_api_cfg.get("mode", "classic")).strip().lower()
    if mode == "central":
        central_cfg = central_api_cfg.get("central", {}) if isinstance(central_api_cfg.get("central"), dict) else {}
        return {
            **_central_runtime_defaults(),
            "api_version": "new_central",
            "cluster_url": str(central_cfg.get("url", "")).strip(),
            "client_id": str(central_cfg.get("client_id", "")).strip(),
            "client_secret": str(central_cfg.get("client_secret", "")),
            "customer_id": str(central_cfg.get("customer_id", "")).strip(),
        }

    classic_cfg = central_api_cfg.get("classic", {}) if isinstance(central_api_cfg.get("classic"), dict) else {}
    classic_has_explicit_values = any(str(classic_cfg.get(key, "")).strip() for key in ("url", "username")) or bool(classic_cfg.get("password"))
    if legacy.get("api_version") == "classic" and not classic_has_explicit_values and (legacy.get("access_token") or legacy.get("refresh_token")):
        return legacy

    return {
        **_central_runtime_defaults(),
        "api_version": "classic",
        "cluster_url": str(classic_cfg.get("url", "")).strip(),
    }


_persisted = _load_persisted_settings()
_persisted_central_api = _normalize_central_api_settings(_persisted.get("central_api", {}), _persisted.get("central_config", {}))
settings: dict[str, Any] = {
    "repo_branch": _persisted.get("repo_branch", REPO_BRANCH),
    "github_token": _persisted.get("github_token", ""),
    "central_api": _persisted_central_api,
    "central_config": _build_runtime_central_config(_persisted_central_api, _persisted.get("central_config", {})),
    # {wsite_value: central_site_name}
    "site_mappings": _persisted.get("site_mappings", {}),
    "spoke_monitored_items": _persisted.get("spoke_monitored_items", []),
    # [{type: "alert"|"insight", id: "...", name: "..."}]  — sim check monitors
    "monitored_checks": _persisted.get("monitored_checks", []),
    # [{id: "AP_DOWN", name: "AP Down", device_type: "ap"|"gateway"|"switch"}]
    "hardware_checks": _persisted.get("hardware_checks", []),
    # Notification settings
    "notifications": _persisted.get("notifications", {
        "email_enabled": False,
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from": "",
        "smtp_to": [],
        "teams_enabled": False,
        "teams_webhook_url": "",
    }),
    "repo_sync_interval": _persisted.get("repo_sync_interval", SYNC_INTERVAL),
    "relay_enabled": _normalize_relay_enabled(_persisted.get("relay_enabled", "off")),
    "relay_server_url": _persisted.get("relay_server_url", _persisted.get("relay_url", "")),
    "hub_tls_verify": _normalize_relay_enabled(_persisted.get("hub_tls_verify", "off")),
    "hub_managed": bool(_persisted.get("hub_managed", False)),
    "hub_isolation_timeout": int(_persisted.get("hub_isolation_timeout", 3600)),  # Persist the configurable hub isolation window in seconds so the safeguard survives restarts.
    "relay_spoke_name": _persisted.get("relay_spoke_name", ""),
    "relay_tenant_hint": _persisted.get("relay_tenant_hint", _persisted.get("relay_tenant_id", "")),
    "relay_api_key": _persisted.get("relay_api_key", _persisted.get("relay_token", "")),
    "relay_spoke_id": _candidate_relay_spoke_id(_persisted),
    "relay_tenant_id": _persisted.get("relay_tenant_id", _persisted.get("relay_tenant_hint", "")),
    "relay_poll_interval": _clamp_relay_interval(_persisted.get("relay_poll_interval", _persisted.get("relay_interval", RELAY_INTERVAL_DEFAULT))),
    "relay_onboarding_psk": _persisted.get("relay_onboarding_psk", ""),
    # --- LM hub relay (combined spoke) ---
    # When enabled, this spoke connects to the Lab Manager hub (lm/core) over
    # the LM websocket as module_type "Client-Sim" and relays its full state
    # there (replacing the legacy webui-hub relay destination). The standalone
    # spoke UI is unaffected. See lm_relay.py.
    "lm_hub_enabled": _normalize_relay_enabled(_persisted.get("lm_hub_enabled", "off")),
    "lm_hub_url": _persisted.get("lm_hub_url", os.getenv("HUB_URL", "")),
    "lm_hub_secret": _persisted.get("lm_hub_secret", os.getenv("HUB_SECRET", "")),
    "lm_spoke_id": _persisted.get("lm_spoke_id", os.getenv("SPOKE_ID", "")),
    "lm_spoke_secret": _persisted.get("lm_spoke_secret", os.getenv("SPOKE_SECRET", "")),
    "lm_hub_poll_interval": _persisted.get("lm_hub_poll_interval", _persisted.get("relay_poll_interval", RELAY_INTERVAL_DEFAULT)),
    "proxmox_approved_agents": _persisted.get("proxmox_approved_agents", {}),
    "proxmox_api_token": _persisted.get("proxmox_api_token", ""),
    "proxmox_tokens": _persisted.get("proxmox_tokens", {}),
    "proxmox_config": _persisted.get("proxmox_config", {}),
    "usb_vidpids": _persisted.get("usb_vidpids", "[]"),
    "usb_missing_timeout": str(_persisted.get("usb_missing_timeout", "60")),
    "vm_image_1_template_id": str(_persisted.get("vm_image_1_template_id", _persisted.get("usb_linux_template_id", _persisted.get("usb_template_id", "100")))),
    "vm_image_2_template_id": str(_persisted.get("vm_image_2_template_id", _persisted.get("usb_windows_template_id", "200"))),
    "vm_image_1_pct": str(_persisted.get("vm_image_1_pct", "50")),
    "usb_auto_provision": _normalize_relay_enabled(_persisted.get("usb_auto_provision", "off")),
    "use_all_dongles": bool(_persisted.get("use_all_dongles", False)),
    "usb_max_slots": str(_persisted.get("usb_max_slots", "24")),
    "cpu_provision_threshold": str(_persisted.get("cpu_provision_threshold", "80")),
    "cpu_delete_threshold": str(_persisted.get("cpu_delete_threshold", "90")),
    "mem_provision_threshold": str(_persisted.get("mem_provision_threshold", "80")),
    "mem_delete_threshold": str(_persisted.get("mem_delete_threshold", "90")),
    "vmid_start": int(_persisted.get("vmid_start", 0)),
    "usb_ignored_vidpids": _persisted.get("usb_ignored_vidpids", "[]"),
    "ignored_hostnames": _persisted.get("ignored_hostnames", '["sim-rpi-0000"]'),
    "vm_silent_timeout": str(_persisted.get("vm_silent_timeout", "24")),
    "reclone_schedule_enabled": _normalize_relay_enabled(_persisted.get("reclone_schedule_enabled", "off")),
    "reclone_schedule_cron": _persisted.get("reclone_schedule_cron", "sunday 02:00"),
    "reclone_concurrency": str(_persisted.get("reclone_concurrency", "1")),
    "protected_vmids": str(_persisted.get("protected_vmids", "")),
    "l1_vlan_start": str(_persisted.get("l1_vlan_start", "100")),
    "l1_vlan_end": str(_persisted.get("l1_vlan_end", "199")),
    "spoke_tls": _normalize_relay_enabled(_persisted.get("spoke_tls", os.getenv("SPOKE_TLS", "off"))),
    "guest_agent_watchdog_enabled": _normalize_relay_enabled(_persisted.get("guest_agent_watchdog_enabled", "on")),
    "guest_agent_grace_minutes": str(_persisted.get("guest_agent_grace_minutes", "20")),
    "guest_agent_check_interval_minutes": str(_persisted.get("guest_agent_check_interval_minutes", "10")),
    "guest_agent_reboot_after_minutes": str(_persisted.get("guest_agent_reboot_after_minutes", "10")),
    "guest_agent_reclone_after_minutes": str(_persisted.get("guest_agent_reclone_after_minutes", "30")),
    "watchdog_reboot_enabled": _normalize_relay_enabled(_persisted.get("watchdog_reboot_enabled", "on")),
    "client_api_key": _persisted.get("client_api_key", ""),
    "admin_ws_token": _persisted.get("admin_ws_token", ""),
    "admin_password": _persisted.get("admin_password", os.getenv("ADMIN_PASSWORD", "")),
    "local_users": _persisted.get("local_users", []),
    "session_timeout_minutes": int(_persisted.get("session_timeout_minutes", 30)),
    # Auth provider config
    "auth_provider": _persisted.get("auth_provider", "local"),
    "auth_ldap_url": _persisted.get("auth_ldap_url", ""),
    "auth_ldap_bind_dn": _persisted.get("auth_ldap_bind_dn", ""),
    "auth_ldap_bind_password": _persisted.get("auth_ldap_bind_password", ""),
    "auth_ldap_user_base": _persisted.get("auth_ldap_user_base", ""),
    "auth_ldap_user_filter": _persisted.get("auth_ldap_user_filter", "(&(objectClass=user)(sAMAccountName={username}))"),
    "auth_ldap_group_admin": _persisted.get("auth_ldap_group_admin", ""),
    "auth_ldap_group_viewer": _persisted.get("auth_ldap_group_viewer", ""),
    "auth_radius_host": _persisted.get("auth_radius_host", ""),
    "auth_radius_port": _persisted.get("auth_radius_port", 1812),
    "auth_radius_secret": _persisted.get("auth_radius_secret", ""),
    "auth_radius_role_attr": _persisted.get("auth_radius_role_attr", "Filter-Id"),
    "auth_radius_admin_val": _persisted.get("auth_radius_admin_val", "admin"),
    "auth_tacacs_host": _persisted.get("auth_tacacs_host", ""),
    "auth_tacacs_port": _persisted.get("auth_tacacs_port", 49),
    "auth_tacacs_secret": _persisted.get("auth_tacacs_secret", ""),
    "auth_tacacs_admin_priv": _persisted.get("auth_tacacs_admin_priv", 15),
}
settings["vm_image_1_template_spec"] = _resolved_template_spec(settings, 1)
settings["vm_image_2_template_spec"] = _resolved_template_spec(settings, 2)
settings["vm_image_1_template_id"] = _primary_template_id(settings["vm_image_1_template_spec"], _legacy_template_id(settings, 1))
settings["vm_image_2_template_id"] = _primary_template_id(settings["vm_image_2_template_spec"], _legacy_template_id(settings, 2))
_ensure_relay_spoke_id(_persisted)


def _ensure_secret_settings(*keys: str) -> None:
    changed = False
    for key in keys:
        value = str(settings.get(key, "") or "").strip()
        if value:
            settings[key] = value
            continue
        settings[key] = secrets.token_urlsafe(32)
        changed = True
    if changed:
        _save_settings()


_ensure_secret_settings("client_api_key", "admin_ws_token")

# Initialise in-memory token from persisted values so a restart
# doesn't require the user to re-enter credentials.
_stored_cfg = settings["central_config"]
_is_new_central = _stored_cfg.get("api_version") == "new_central"
if not _is_new_central and _stored_cfg.get("access_token"):
    # Classic: restore pasted token from disk
    central_token: dict[str, Any] = {
        "access_token": _stored_cfg["access_token"],
        "refresh_token": _stored_cfg.get("refresh_token"),
        "expires_at": time.time() + 7200,
    }
else:
    # New Central: token will be fetched automatically via client_credentials
    central_token: dict[str, Any] = {
        "access_token": None,
        "refresh_token": None,
        "expires_at": 0.0,
    }


ALLOWED_PLATFORMS = {"linux", "windows"}
SIMULATION_SECTION_KEYS = {
    "wsite",
    "ssid",
    "ssidpw",
    "dhcp_fail",
    "dns_fail",
    "assoc_fail",
    "port_flap",
    "ping_test",
    "download",
    "www_traffic",
    "iperf",
    "sim_phy",
}
GLOBAL_SECTION_KEYS = {
    "kill_switch",
    "rapid_update",
    "sim_load",
    "github_repo",
    "repo_location",
    "repo_branch",
    "site_based_ssid",
    "reboot_schedule",
    "allow_offline",
    "ssidpw_fail",
    "auth_fail",
    "iperf_bw",
    "syslog",
    "web_server",
}
SERVER_SECTION_KEYS = {"server_url"}
ADDRESS_SECTION_KEYS = {
    "smb_address",
    "ping_address",
    "dns_latency_1",
    "dns_latency_2",
    "dns_latency_3",
    "dns_bad_ip_1",
    "dns_bad_ip_2",
    "dns_bad_ip_3",
    "dns_bad_record_1",
    "dns_bad_record_2",
    "dns_bad_record_3",
    "iperf_server",
    "syslog_server",
}
ALLOWED_CONFIG_SECTIONS = {"simulation", "address", "server", *(f"s{i}" for i in range(10))}


# ── Aruba Central state ───────────────────────────────────────────────────────
# central_token is declared above, initialised from persisted settings.
# {wsite: {check_id: {status, count, ts, check_name, check_type}}}
state.central_status = {}
state.central_wireless_clients = {}   # wsite → client count from Central API
# wsite → list of (timestamp_float, client_count_int) samples (rolling 60 min)
_client_count_samples: dict[str, list[tuple[float, int]]] = {}
CLIENT_COUNT_WINDOW = 3600   # seconds of history to keep
CLIENT_COUNT_MIN_SAMPLES = 3  # minimum samples before flagging
CLIENT_COUNT_DROP_PCT = 25.0  # percent drop that triggers alert
state.central_history = []   # in-memory 24-h window
state.central_auth_error = None          # last auth/token failure message
history_lock = asyncio.Lock()
# Browse data for distributed mode — populated each poll cycle (new_central only)
state.central_browse_alerts = []
state.central_browse_insights = []
state.central_browse_devices_by_site = {}
state.central_browse_clients_by_site = {}
state.central_browse_clients = []  # individual client records
# Server-side cache for /api/central/browse — avoids hammering Central API on every tab open.
state._central_browse_response_cache = {}
state._central_browse_response_cached_at = 0.0
state._central_browse_fetching = False  # lock to prevent concurrent on-demand fetches
NC_BROWSE_SERVER_CACHE_TTL_S: int = 300  # 5 minutes — same as hub
# Serialise all git operations (fetch, reset, add, commit, push) on REPO_DIR.
# Running two git commands concurrently on the same repo creates .git/index.lock
# conflicts that cause the sync background task to hang indefinitely.
_git_lock = asyncio.Lock()

# Load persisted client count baseline so the UI has a reference point
# immediately after a restart instead of showing NO_DATA for an hour.
_client_count_baseline: dict[str, Any] = {}
try:
    _client_count_baseline = json.loads(CLIENT_COUNT_BASELINE_FILE.read_text(encoding="utf-8"))
    # Seed in-memory samples from the persisted baseline so _client_count_payload()
    # can surface the saved average immediately on startup — before enough live samples
    # have been collected to compute a fresh average.  We create CLIENT_COUNT_MIN_SAMPLES
    # synthetic entries spaced 60 s apart in the recent past.  They stay in the
    # CLIENT_COUNT_WINDOW (3600 s) for ~55 min, then age out naturally as real data
    # accumulates and replaces them.
    _seed_now = time.time()
    for _wsite, _saved in _client_count_baseline.items():
        _avg_val = round(_saved["hourly_avg"])
        _client_count_samples[_wsite] = [
            (_seed_now - (CLIENT_COUNT_MIN_SAMPLES - i) * 60, _avg_val)
            for i in range(CLIENT_COUNT_MIN_SAMPLES)
        ]
except Exception:
    pass

# 7-day hourly history: wsite → [(timestamp, hourly_avg), ...]
# Each hourly_baseline_saver() tick appends one entry; the 7-day avg of these
# is used as the alarm baseline so a prolonged drop doesn't suppress the alert.
_client_count_hourly_history: dict[str, list[tuple[float, float]]] = {}
try:
    _7d_raw = json.loads(CLIENT_COUNT_7DAY_FILE.read_text(encoding="utf-8"))
    _7d_cutoff = time.time() - CLIENT_COUNT_7DAY_WINDOW
    _client_count_hourly_history = {
        wsite: [(float(ts), float(v)) for ts, v in entries if float(ts) >= _7d_cutoff]
        for wsite, entries in _7d_raw.items()
    }
except Exception:
    pass

# Populated during each Central poll cycle from alert objects.
state.hardware_alert_devices = {}

# In hub-connected (centralized) mode the hub computes hardware_alerts and pushes the
# full pre-built list (id/name/device_type/total/sites) to the spoke.  We cache it
# here so _hw_alerts_payload() can return it when local settings["hardware_checks"] is
# empty (i.e. the spoke has no locally-configured checks).
state._hub_fed_hardware_alerts = []

# Previous check states for transition detection (green→red email/Teams trigger).
# {check_key: "OK"|"ERROR"}  where check_key = f"{check_id}:{wsite}" or just check_id for hw
_prev_check_states: dict[str, str] = {}

# Friendly-name map for known Central alert types
_HW_FRIENDLY: dict[str, str] = {
    "AP_DOWN": "AP Down",
    "AP_DISCONNECTED": "AP Disconnected",
    "AP_REBOOT": "AP Rebooted",
    "AP_FLAP": "AP Flapping",
    "GW_DOWN": "Gateway Down",
    "GW_DISCONNECTED": "Gateway Disconnected",
    "GW_FAILOVER": "Gateway Failover",
    "SWITCH_DOWN": "Switch Down",
    "SWITCH_DISCONNECTED": "Switch Disconnected",
    "SWITCH_PORT_DOWN": "Switch Port Down",
    "UPLINK_DOWN": "Uplink Down",
    "TUNNEL_DOWN": "Tunnel Down",
    "CONTROLLER_DOWN": "Controller Down",
}

# Device-type auto-detection from alert_type prefix
_ALERT_DEVICE_TYPE: dict[str, str] = {
    "AP_": "ap",
    "GW_": "gateway",
    "SWITCH_": "switch",
    "UPLINK_": "gateway",
    "TUNNEL_": "gateway",
    "CONTROLLER_": "gateway",
}


def _auto_device_type(alert_id: str) -> str:
    """Guess device type from alert_type prefix."""
    upper = alert_id.upper()
    for prefix, dtype in _ALERT_DEVICE_TYPE.items():
        if upper.startswith(prefix):
            return dtype
    return "ap"  # sensible default


# ── History file helpers ──────────────────────────────────────────────────────
# History file helpers moved to services/history.py
from services.history import _history_cutoff, _load_history, _append_and_trim_history  # noqa: E402


# ── Client history persistence ────────────────────────────────────────────────

def _client_history_cutoff() -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=CLIENT_HISTORY_DAYS)


def _load_client_history() -> dict[str, dict[str, Any]]:
    """Load persisted client records from disk, dropping entries older than 7 days."""
    if not CLIENT_HISTORY_FILE.exists():
        return {}
    try:
        raw = json.loads(CLIENT_HISTORY_FILE.read_text(encoding="utf-8"))
        cutoff = _client_history_cutoff()
        kept: dict[str, dict[str, Any]] = {}
        for hostname, record in raw.items():
            ls = record.get("last_seen")
            if ls:
                try:
                    dt = datetime.fromisoformat(ls)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        record = dict(record)
                        record["last_seen"] = dt  # ensure datetime type for serialize_client
                        kept[hostname] = record
                        continue
                except Exception:
                    pass
        logger.info("Loaded %d client record(s) from history (%d expired)",
                    len(kept), len(raw) - len(kept))
        return kept
    except Exception as exc:
        logger.warning("Could not load client history: %s", exc)
        return {}


def _save_client_history() -> None:
    """Serialise the in-memory clients dict to disk, pruning entries older than 7 days."""
    try:
        cutoff = _client_history_cutoff()
        snapshot: dict[str, Any] = {}
        for hostname, c in clients.items():
            ls = c.get("last_seen")
            if isinstance(ls, datetime):
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=timezone.utc)
                if ls < cutoff:
                    continue  # expired — do not persist
                entry = dict(c)
                entry["last_seen"] = ls.isoformat()
            else:
                entry = dict(c)
            snapshot[hostname] = entry
        CLIENT_HISTORY_FILE.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save client history: %s", exc)


async def client_history_saver() -> None:
    """Background task: flush clients to disk every CLIENT_SAVE_INTERVAL seconds."""
    await asyncio.sleep(CLIENT_SAVE_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(_save_client_history)
            _update_service_health("client_history_saver", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("client_history_saver", ok=False, error=str(exc))
            logger.exception("Client history saver error: %s", exc)
        await asyncio.sleep(CLIENT_SAVE_INTERVAL)


def _can_refresh() -> bool:
    """True when we can obtain a fresh token automatically."""
    cfg = _central_cfg()
    if not cfg.get("cluster_url") or not cfg.get("client_id") or not cfg.get("client_secret"):
        return False
    if _is_new_central_api():
        return True  # New Central uses client_credentials — no refresh token needed
    return bool(cfg.get("refresh_token") or central_token.get("refresh_token"))


# New Central GLP SSO token endpoint
_NEW_CENTRAL_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"


def _parse_bearer_token(authorization: str | None) -> str:
    value = str(authorization or "").strip()
    if not value:
        return ""
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _parse_upstream_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        logger.warning("Malformed JSON from upstream (status %s): %s", resp.status_code, exc)
        raise UpstreamJSONError(f"Malformed JSON from upstream (HTTP {resp.status_code})") from exc


def _valid_shared_client_key(provided_key: str) -> bool:
    expected_key = str(settings.get("client_api_key", "") or "").strip()
    candidate = str(provided_key or "").strip()
    return bool(expected_key and candidate) and secrets.compare_digest(candidate, expected_key)


def _require_shared_client_key(provided_key: str, context: str) -> None:
    if _valid_shared_client_key(provided_key):
        return
    logger.warning("Rejected %s with invalid shared client API key", context)
    raise HTTPException(status_code=403, detail="invalid client key")


# ── Aruba Central poll loop ───────────────────────────────────────────────────

async def _fetch_nc_browse_for_spoke(client: httpx.AsyncClient) -> None:
    """Fetch new_central browse data (alerts, insights, devices, clients) filtered to
    this spoke's assigned sites.  Results are stored in the module-level
    central_browse_* variables so they can be included in the telemetry sent to hub."""

    cfg = _central_cfg()
    base_url = cfg["cluster_url"].rstrip("/")
    headers = _central_headers()

    # Fetch ALL Central sites for browse data (not limited to site_mappings so
    # the browse tab can show Monitor buttons for sites not yet configured).
    central_sites: list[str] = []
    try:
        if _is_new_central_api():
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/sites-health",
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                central_sites = [
                    item.get("siteName") or item.get("site_name") or item.get("name", "")
                    for item in resp.json().get("items", [])
                    if (item.get("siteName") or item.get("site_name") or item.get("name"))
                ]
        else:
            for path, params in [
                ("/monitoring/v2/sites", {"limit": 1000, "offset": 0}),
                ("/monitoring/v1/sites", {"limit": 1000, "offset": 0}),
                ("/central/v2/sites", {"limit": 1000, "offset": 0}),
            ]:
                resp = await client.get(f"{base_url}{path}", headers=headers, params=params, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("sites") or data.get("items") or (data if isinstance(data, list) else [])
                    central_sites = [
                        (s if isinstance(s, str) else (s.get("site_name") or s.get("siteName") or s.get("name", "")))
                        for s in raw if s
                    ]
                    central_sites = [s for s in central_sites if s]
                    break
    except Exception as exc:
        logger.warning("NC browse: could not fetch all Central sites list: %s", exc)

    # Fall back to site_mappings if the API sites fetch failed
    if not central_sites:
        site_mappings: dict[str, str] = settings.get("site_mappings", {})
        central_sites = [s for s in site_mappings.values() if s]

    if not central_sites:
        return

    def _sev_map(s: str) -> str:
        s = (s or "").lower()
        return "error" if s == "critical" else "warning" if s in ("major", "minor") else "info"

    new_alerts: list[dict[str, Any]] = []
    new_insights: list[dict[str, Any]] = []
    new_devices_by_site: dict[str, list[dict[str, Any]]] = {}
    new_clients_by_site: dict[str, dict[str, Any]] = {}
    new_clients: list[dict[str, Any]] = []

    for central_site in central_sites:
        # ── Alerts for this site ──────────────────────────────────────────────
        try:
            filt = f"status eq 'Active' and siteName eq '{central_site}'"
            cursor: str | None = None
            seen: set[tuple[str, str]] = set()
            for _ in range(20):  # max 20 pages per site
                params: dict[str, Any] = {"limit": 100, "$filter": filt}
                if cursor:
                    params["next"] = cursor
                resp = await client.get(f"{base_url}/network-notifications/v1/alerts", headers=headers, params=params, timeout=30)
                if resp.status_code == 401 and _can_refresh():
                    ok, _ = await _refresh_central_token(client)
                    if ok:
                        headers = _central_headers()
                    resp = await client.get(f"{base_url}/network-notifications/v1/alerts", headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    break
                body = resp.json()
                for item in body.get("items", []):
                    name = (item.get("name") or item.get("alertType") or "").strip()
                    site = (item.get("siteName") or central_site).strip()
                    key = (name.lower(), site.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    new_alerts.append({
                        "name": name,
                        "site": site,
                        "severity": _sev_map(item.get("severity") or ""),
                        "category": item.get("category") or "",
                        "device_type": item.get("deviceType") or "",
                        "detail": item.get("summary") or "",
                        "ts": item.get("createdAt") or "",
                    })
                cursor = body.get("next")
                if not cursor:
                    break
        except Exception as exc:
            logger.warning("NC browse alerts fetch failed for site %s: %s", central_site, exc)

        # ── Insights for this site ────────────────────────────────────────────
        try:
            cursor = None
            for _ in range(10):
                params = {"limit": 100}
                if cursor:
                    params["next"] = cursor
                resp = await client.get(f"{base_url}/network-notifications/v1/insights", headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    break
                body = resp.json()
                for item in body.get("items", []):
                    for site_info in (item.get("impactedSites") or [{"siteName": central_site}]):
                        site = (site_info.get("siteName") or "").strip()
                        if site.lower() != central_site.lower():
                            continue
                        ts_raw = item.get("timestamp") or item.get("ts") or ""
                        try:
                            ts_val = datetime.utcfromtimestamp(int(ts_raw) / 1000).isoformat() if str(ts_raw).isdigit() else str(ts_raw)
                        except Exception:
                            ts_val = str(ts_raw)
                        new_insights.append({
                            "name": (item.get("name") or item.get("title") or "").strip(),
                            "category": item.get("category") or "",
                            "description": item.get("description") or "",
                            "site": site,
                            "device_count": site_info.get("impactedDeviceCount") or item.get("impactedDeviceCount") or 0,
                            "client_count": site_info.get("impactedClientCount") or item.get("impactedClientCount") or 0,
                            "ts": ts_val,
                        })
                cursor = body.get("next")
                if not cursor:
                    break
        except Exception as exc:
            logger.warning("NC browse insights fetch failed for site %s: %s", central_site, exc)

        # ── Devices for this site ─────────────────────────────────────────────
        try:
            cursor = None
            site_devs: list[dict[str, Any]] = []
            for _ in range(20):
                params = {"limit": 100, "$filter": f"siteName eq '{central_site}'"}
                if cursor:
                    params["next"] = cursor
                resp = await client.get(f"{base_url}/network-monitoring/v1/devices", headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    break
                body = resp.json()
                for dev in body.get("items", []):
                    site_devs.append({
                        "name": dev.get("name") or dev.get("hostname") or "",
                        "serial": dev.get("serialNumber") or "",
                        "type": dev.get("deviceType") or "",
                        "model": dev.get("model") or "",
                        "status": (dev.get("status") or "").upper(),
                        "ip": dev.get("ipAddress") or dev.get("ip") or "",
                        "firmware": dev.get("firmwareVersion") or "",
                        "site": central_site,
                    })
                cursor = body.get("next")
                if not cursor:
                    break
            if site_devs:
                new_devices_by_site[central_site] = site_devs
        except Exception as exc:
            logger.warning("NC browse devices fetch failed for site %s: %s", central_site, exc)

        # ── Clients for this site ─────────────────────────────────────────────
        try:
            filt = f"status eq 'Connected' and siteName eq '{central_site}'"
            cursor = None
            total = wired = wireless = 0
            for _ in range(50):
                params = {"limit": 100, "$filter": filt}
                if cursor:
                    params["next"] = cursor
                resp = await client.get(f"{base_url}/network-monitoring/v1/clients", headers=headers, params=params, timeout=30)
                if resp.status_code != 200:
                    break
                body = resp.json()
                for c in body.get("items", []):
                    total += 1
                    conn = (c.get("clientConnectionType") or "").lower()
                    if conn == "wired":
                        wired += 1
                    else:
                        wireless += 1
                    # Capture individual client record for the browse tab
                    mac = (c.get("macAddress") or c.get("mac") or "").upper()
                    new_clients.append({
                        "mac": mac,
                        "hostname": c.get("hostname") or c.get("name") or "—",
                        "username": c.get("username") or "",
                        "ip": c.get("ipAddress") or c.get("ip") or "",
                        "ap": c.get("apName") or c.get("ap") or "",
                        "ssid": c.get("ssid") or "",
                        "vlan": str(c.get("vlan") or ""),
                        "status": c.get("status") or "Connected",
                        "os": c.get("operatingSystem") or c.get("os") or "",
                        "site": central_site,
                        "connection_type": conn or "wireless",
                    })
                cursor = body.get("next")
                if not cursor:
                    break
            new_clients_by_site[central_site] = {"total": total, "wired": wired, "wireless": wireless}
        except Exception as exc:
            logger.warning("NC browse clients fetch failed for site %s: %s", central_site, exc)

    state.central_browse_alerts = new_alerts
    state.central_browse_insights = new_insights
    state.central_browse_devices_by_site = new_devices_by_site
    state.central_browse_clients_by_site = new_clients_by_site
    state.central_browse_clients = new_clients
    state._central_browse_response_cache = {}
    state._central_browse_response_cached_at = 0.0
    logger.info("NC browse fetch complete: %d alerts, %d insights, %d sites with devices, %d sites with clients (%d individual)",
                len(new_alerts), len(new_insights), len(new_devices_by_site), len(new_clients_by_site), len(new_clients))


def _save_client_count_baseline() -> None:
    """Persist per-site hourly averages to disk (restart recovery) and
    append a snapshot to the 7-day hourly history used as the alarm baseline."""
    snapshot: dict[str, Any] = {}
    now = time.time()
    cutoff_7day = now - CLIENT_COUNT_7DAY_WINDOW
    for wsite, samples in _client_count_samples.items():
        if len(samples) < CLIENT_COUNT_MIN_SAMPLES:
            continue
        avg = sum(s[1] for s in samples) / len(samples)
        snapshot[wsite] = {"hourly_avg": round(avg, 1), "recorded_at": now}
        # Append current hourly average to the 7-day rolling history.
        hist = _client_count_hourly_history.setdefault(wsite, [])
        hist.append((now, avg))
        _client_count_hourly_history[wsite] = [(ts, v) for ts, v in hist if ts >= cutoff_7day]
    if snapshot:
        try:
            CLIENT_COUNT_BASELINE_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            _client_count_baseline.update(snapshot)
        except Exception as exc:
            logger.warning("Could not save client count baseline: %s", exc)
    # Persist 7-day history independently so a restart still has the full window.
    if _client_count_hourly_history:
        try:
            CLIENT_COUNT_7DAY_FILE.write_text(
                json.dumps(
                    {wsite: list(hist) for wsite, hist in _client_count_hourly_history.items()},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save 7-day client count history: %s", exc)


async def hourly_baseline_saver() -> None:
    """Background task: recalculate and persist the client count baseline every hour.

    This replaces any stale baseline file with a fresh average computed from the
    last 60 minutes of live samples.  Running independently of the Central poll
    loop means the file is always at most ~1 hour old regardless of poll frequency.
    """
    await asyncio.sleep(3600)   # wait one full hour before first write
    while True:
        try:
            _save_client_count_baseline()
            logger.info("Client count baseline recalculated and persisted (hourly task).")
            _update_service_health("baseline_saver", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("baseline_saver", ok=False, error=str(exc))
            logger.exception("Hourly baseline saver error: %s", exc)
        await asyncio.sleep(3600)


SIM_CLIENT_SAMPLE_INTERVAL = 60  # seconds between sim-client count samples


async def sim_client_count_sampler() -> None:
    """Background task: sample sim client counts per wsite every minute.

    Provides client-count data for sites even when the Central API is not
    configured or returns no wireless-client information.  The Central API
    poll takes priority: if it has added a sample for a wsite within the
    last SIM_CLIENT_SAMPLE_INTERVAL seconds, we skip that wsite so we do
    not double-count.
    """
    while True:
        await asyncio.sleep(SIM_CLIENT_SAMPLE_INTERVAL)
        try:
            now = time.time()
            cutoff_cc = now - CLIENT_COUNT_WINDOW
            async with state_lock:
                active_snap = {h: dict(c) for h, c in clients.items()}

            counts = _sim_clients_per_wsite(active_snap)
            if not counts:
                continue

            updated = False
            for wsite_val, count in counts.items():
                existing = _client_count_samples.get(wsite_val, [])
                last_ts = existing[-1][0] if existing else 0.0
                # Skip if Central API already added a fresh sample this cycle
                if now - last_ts < SIM_CLIENT_SAMPLE_INTERVAL * 0.9:
                    continue
                _client_count_samples.setdefault(wsite_val, []).append((now, count))
                _client_count_samples[wsite_val] = [
                    s for s in _client_count_samples[wsite_val] if s[0] >= cutoff_cc
                ]
                updated = True

            if updated:
                _save_client_count_baseline()
                _save_state_cache()
                await broadcast({
                    "type": "central_update",
                    "client_count_status": _client_count_payload(),
                })
            _update_service_health("sim_client_sampler", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("sim_client_sampler", ok=False, error=str(exc))
            logger.warning("Sim client count sampler error: %s", exc)


def _client_count_payload() -> dict[str, Any]:
    """Per-site client count status using a 7-day rolling baseline.

    Alarm logic:
    - current_hourly = average of the last hour's samples (smoothed "current")
    - baseline = 7-day rolling average of hourly snapshots (stable reference)
    - drop_pct = (baseline - current_hourly) / baseline × 100
    - DEGRADED when drop_pct >= CLIENT_COUNT_DROP_PCT

    Because the baseline spans 7 days, a prolonged client drop does NOT
    suppress the alarm — it stays active until counts recover to near-normal.
    Falls back to 1-hour average when insufficient 7-day history exists
    (first day of operation or after data loss)."""
    site_mappings = settings.get("site_mappings", {})
    result: dict[str, Any] = {}
    for wsite, samples in _client_count_samples.items():
        if not samples:
            continue
        current = samples[-1][1]   # raw latest sample (display only)
        site_name = site_mappings.get(wsite, wsite)

        # 7-day baseline: average of all hourly snapshots recorded in the last 7 days.
        hourly_hist = _client_count_hourly_history.get(wsite, [])
        has_7day = len(hourly_hist) >= 2
        baseline_7day = (sum(v for _, v in hourly_hist) / len(hourly_hist)) if has_7day else None

        if len(samples) < CLIENT_COUNT_MIN_SAMPLES:
            # Not enough live samples yet — use persisted baseline data.
            saved = _client_count_baseline.get(wsite)
            if saved:
                hourly_avg = saved["hourly_avg"]
                baseline = baseline_7day if has_7day else hourly_avg
                drop_pct = max(0.0, (baseline - hourly_avg) / baseline * 100.0) if baseline >= 1 else 0.0
                status = "DEGRADED" if drop_pct >= CLIENT_COUNT_DROP_PCT else "OK"
                result[wsite] = {
                    "site_name": site_name,
                    "current": current,
                    "hourly_avg": hourly_avg,
                    "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                    "baseline_source": "7day" if has_7day else "hourly",
                    "drop_pct": drop_pct,
                    "status": status,
                    "ts": samples[-1][0],
                    "baseline_stale": True,
                    "baseline_recorded_at": saved["recorded_at"],
                }
            else:
                result[wsite] = {
                    "site_name": site_name,
                    "current": current,
                    "hourly_avg": current,
                    "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                    "baseline_source": "none",
                    "drop_pct": 0.0,
                    "status": "NO_DATA",
                    "ts": samples[-1][0],
                    "baseline_stale": False,
                }
            continue

        hourly_avg = sum(s[1] for s in samples) / len(samples)
        # Use 7-day baseline when available; fall back to hourly avg on first day.
        baseline = baseline_7day if has_7day else hourly_avg
        if baseline < 1:
            status = "OK"
            drop_pct = 0.0
        else:
            # Compare smoothed current (hourly avg) against the 7-day stable baseline.
            drop_pct = (baseline - hourly_avg) / baseline * 100.0
            status = "DEGRADED" if drop_pct >= CLIENT_COUNT_DROP_PCT else "OK"
        result[wsite] = {
            "site_name": site_name,
            "current": current,
            "hourly_avg": round(hourly_avg, 1),
            "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
            "baseline_source": "7day" if has_7day else "hourly",
            "drop_pct": drop_pct,
            "status": status,
            "ts": samples[-1][0],
            "baseline_stale": False,
        }
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    logger.info("=" * 60)
    logger.info("Client Simulator  v%s  starting up", INSTALLER_VERSION)
    logger.info("=" * 60)
    _debug_event("server_start", f"v{INSTALLER_VERSION} ui:v{APP_VERSION}")
    state.central_history = await asyncio.to_thread(_load_history)
    _load_state_cache()
    _load_commands()
    _load_reclone_state()
    _load_relay_state()
    _load_update_state()
    _load_vm_watchdog()
    _load_resource_cache()
    if not _autoprov_enabled():
        _clear_provision_halt_state()
    # Refresh cs-webui frontend files before accepting requests (non-blocking,
    # awaited once so the fix is in place before the first page load).
    await asyncio.wait_for(refresh_webui_frontend(), timeout=60)
    background_tasks["sync_repo"] = asyncio.create_task(sync_repo())
    background_tasks["heartbeat"] = asyncio.create_task(heartbeat_check())
    background_tasks["central_token"] = asyncio.create_task(central_token_manager())
    background_tasks["central_poller"] = asyncio.create_task(central_poller())
    background_tasks["update_checker"] = asyncio.create_task(check_for_update())
    background_tasks["webui_refresh"] = asyncio.create_task(periodic_webui_refresh())
    # Relay to exactly one hub. When lm_hub_enabled, the combined spoke connects
    # to the LM hub (lm/core) as module_type "Client-Sim" and relays its full
    # state there (replacing the legacy webui-hub relay destination). The
    # standalone spoke UI is unaffected either way.
    if _normalize_relay_enabled(settings.get("lm_hub_enabled", "off")) != "off":
        try:
            from lm_relay import build_lm_control_plane
            cp = build_lm_control_plane()
        except Exception:
            logger.exception("LM relay bridge failed to build; falling back to legacy relay")
            cp = None
        if cp is not None:
            background_tasks["lm_relay"] = asyncio.create_task(cp.run())
        else:
            background_tasks["relay"] = asyncio.create_task(relay_loop())
    else:
        background_tasks["relay"] = asyncio.create_task(relay_loop())
    background_tasks["hub_isolation_monitor"] = asyncio.create_task(hub_isolation_monitor())  # Watch the timeout in the background so the UI updates when isolation flips without waiting for another relay message.
    background_tasks["sim_conf_refresh"] = asyncio.create_task(_sim_conf_content_refresh_loop())
    background_tasks["user_overrides_conf_refresh"] = asyncio.create_task(_user_overrides_conf_content_refresh_loop())
    background_tasks["client_history_saver"] = asyncio.create_task(client_history_saver())
    background_tasks["command_expiry"] = asyncio.create_task(expire_commands())
    background_tasks["auto_recovery"] = asyncio.create_task(auto_recovery_check())
    background_tasks["vm_watchdog"] = asyncio.create_task(vm_watchdog_loop())
    background_tasks["schedule_check"] = asyncio.create_task(schedule_check())
    background_tasks["gkill_switch"] = asyncio.create_task(gkill_switch_poller())
    background_tasks["baseline_saver"] = asyncio.create_task(hourly_baseline_saver())
    background_tasks["sim_client_sampler"] = asyncio.create_task(sim_client_count_sampler())
    background_tasks["acme_renewal"] = asyncio.create_task(acme_renewal_loop())
    background_tasks["demo_expiry"] = asyncio.create_task(_demo_expiry_task())
    background_tasks["vm_sim_tag_sync"] = asyncio.create_task(_vm_sim_tag_sync_loop())
    background_tasks["loop_lag_monitor"] = asyncio.create_task(_event_loop_lag_monitor())
    yield
    # Flush client history to disk on shutdown
    await asyncio.to_thread(_save_client_history)
    for task in background_tasks.values():
        task.cancel()
    for task in background_tasks.values():
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ── Spoke session auth ─────────────────────────────────────────────────────────
# When spoke auth is enabled, all API routes (except /api/auth/*, /static/*,
# /ws, and GET /) require a valid session cookie.
_SPOKE_SESSION_COOKIE = "spoke_session"


@dataclass
class SpokeUser:
    username: str
    role: str
    auth_provider: str
    display_name: str = ""


_spoke_sessions: dict[str, tuple[SpokeUser, float]] = {}  # token → (user, expiry)


def _get_session_ttl() -> int:
    return max(5, min(1440, int(settings.get("session_timeout_minutes", 30)))) * 60


def _admin_password() -> str:
    return str(settings.get("admin_password", "") or os.getenv("ADMIN_PASSWORD", "") or "").strip()


def _normalize_spoke_auth_provider(value: Any) -> str:
    provider = str(value or "local").strip().lower()
    return provider if provider in {"local", "ldap", "radius", "tacacs"} else "local"


_LOCAL_USER_ROLES = {"admin", "viewer"}
_LOCAL_PASSWORD_SCHEME = "pbkdf2_sha256"
_LOCAL_PASSWORD_ITERATIONS = 200_000


def _normalize_local_role(value: Any) -> str:
    role = str(value or "viewer").strip().lower()
    return role if role in _LOCAL_USER_ROLES else "viewer"


def _normalize_local_users(value: Any) -> list[dict[str, str]]:
    users: list[dict[str, str]] = []
    if not isinstance(value, list):
        return users
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        username = str(entry.get("username", "") or "").strip()
        password_hash = str(entry.get("password_hash", "") or "")
        if not username or not password_hash:
            continue
        username_key = username.lower()
        if username_key == "admin" or username_key in seen:
            continue
        seen.add(username_key)
        users.append({
            "username": username,
            "password_hash": password_hash,
            "role": _normalize_local_role(entry.get("role", "viewer")),
        })
    return users


def _get_local_users() -> list[dict[str, str]]:
    users = _normalize_local_users(settings.get("local_users", []))
    if users != settings.get("local_users", []):
        settings["local_users"] = users
    return users


def _hash_local_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _LOCAL_PASSWORD_ITERATIONS)
    return f"{_LOCAL_PASSWORD_SCHEME}${_LOCAL_PASSWORD_ITERATIONS}${salt.hex()}${derived.hex()}"


def _verify_local_password(password: str, password_hash: str) -> bool:
    try:
        scheme, iterations_raw, salt_hex, digest_hex = str(password_hash or "").split("$", 3)
        if scheme != _LOCAL_PASSWORD_SCHEME:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_raw),
        )
        return secrets.compare_digest(derived.hex(), digest_hex)
    except Exception:
        return False


def _check_credentials(username: str, password: str) -> SpokeUser | None:
    candidate = str(username or "").strip()
    supplied_password = str(password or "")
    if not supplied_password:
        return None

    admin_password = _admin_password()
    if candidate.lower() in {"", "admin"} and admin_password and secrets.compare_digest(supplied_password.strip(), admin_password):
        return SpokeUser(username="admin", role="admin", auth_provider="local")

    if not candidate:
        return None
    candidate_key = candidate.lower()
    for entry in _get_local_users():
        stored_username = str(entry.get("username", "") or "").strip()
        if stored_username.lower() != candidate_key:
            continue
        if _verify_local_password(supplied_password, str(entry.get("password_hash", "") or "")):
            return SpokeUser(
                username=stored_username,
                role=_normalize_local_role(entry.get("role", "viewer")),
                auth_provider="local",
            )
        break
    return None


def _spoke_auth_required() -> bool:
    return bool(
        _admin_password()
        or _get_local_users()
        or _normalize_spoke_auth_provider(settings.get("auth_provider", "local")) != "local"
    )


async def require_auth(request: Request) -> SpokeUser:
    user = getattr(request.state, "spoke_user", None)
    if isinstance(user, SpokeUser):
        return user
    if not _spoke_auth_required():
        user = SpokeUser(username="admin", role="admin", auth_provider="local")
        request.state.spoke_user = user
        return user
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    user = _validate_spoke_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    request.state.spoke_user = user
    return user


def _create_spoke_session(user: SpokeUser) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _spoke_sessions[token] = (user, now + _get_session_ttl())
    expired = [stored_token for stored_token, (_, expiry) in list(_spoke_sessions.items()) if now >= expiry]
    for stored_token in expired:
        _spoke_sessions.pop(stored_token, None)
    return token


def _validate_spoke_session(token: str) -> SpokeUser | None:
    if not token:
        return None
    entry = _spoke_sessions.get(token)
    if entry is None:
        return None
    user, expiry = entry
    if time.time() >= expiry:
        _spoke_sessions.pop(token, None)
        return None
    return user


def _ldap_authenticate_sync(username: str, password: str) -> "SpokeUser | None":
    """Blocking LDAP auth — call via asyncio.to_thread."""
    try:
        from ldap3 import ALL, Connection, Server

        s = settings
        if not s.get("auth_ldap_url") or not s.get("auth_ldap_bind_dn"):
            return None

        srv = Server(s["auth_ldap_url"], get_info=ALL)

        with Connection(srv, user=s["auth_ldap_bind_dn"], password=s["auth_ldap_bind_password"], auto_bind=True) as conn:
            search_filter = str(s.get("auth_ldap_user_filter") or "(&(objectClass=user)(sAMAccountName={username}))").format(username=username)
            conn.search(
                search_base=s["auth_ldap_user_base"],
                search_filter=search_filter,
                attributes=["cn", "mail", "memberOf", "displayName"],
            )
            if not conn.entries:
                return None
            entry = conn.entries[0]
            user_dn = entry.entry_dn
            display_name = str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else username
            member_of = [str(group) for group in list(entry.memberOf)] if hasattr(entry, "memberOf") and entry.memberOf else []

        with Connection(srv, user=user_dn, password=password, auto_bind=True) as user_conn:
            if not user_conn.bound:
                return None

        admin_group = str(s.get("auth_ldap_group_admin", "") or "")
        viewer_group = str(s.get("auth_ldap_group_viewer", "") or "")
        role = "viewer"
        if admin_group and any(admin_group.lower() in group.lower() for group in member_of):
            role = "admin"
        elif not viewer_group:
            role = "admin"
        elif viewer_group and any(viewer_group.lower() in group.lower() for group in member_of):
            role = "viewer"
        else:
            return None

        return SpokeUser(username=username, role=role, auth_provider="ldap", display_name=display_name)
    except ImportError:
        logger.warning("ldap3 not installed — LDAP auth unavailable")
        return None
    except Exception as exc:
        logger.warning(f"LDAP auth error for {username}: {exc}")
        return None


async def _ldap_authenticate(username: str, password: str) -> "SpokeUser | None":
    """Authenticate against LDAP/AD. Returns SpokeUser or None."""
    return await asyncio.to_thread(_ldap_authenticate_sync, username, password)


def _radius_authenticate_sync(username: str, password: str) -> "SpokeUser | None":
    """Blocking RADIUS auth — call via asyncio.to_thread."""
    try:
        import io

        import pyrad.client
        import pyrad.dictionary
        import pyrad.packet

        s = settings
        if not s.get("auth_radius_host") or not s.get("auth_radius_secret"):
            return None

        dict_src = """
ATTRIBUTE User-Name      1  string
ATTRIBUTE User-Password  2  string
ATTRIBUTE Filter-Id      11 string
ATTRIBUTE Class          25 string
"""
        dictionary = pyrad.dictionary.Dictionary(io.StringIO(dict_src))
        client = pyrad.client.Client(
            server=s["auth_radius_host"],
            authport=int(s.get("auth_radius_port", 1812)),
            secret=str(s["auth_radius_secret"]).encode(),
            dict=dictionary,
        )
        client.timeout = 10

        req = client.CreateAuthPacket(code=pyrad.packet.AccessRequest, User_Name=username)
        req["User-Password"] = req.PwCrypt(password)
        reply = client.SendPacket(req)

        if reply.code != pyrad.packet.AccessAccept:
            return None

        role_attr = str(s.get("auth_radius_role_attr", "Filter-Id") or "Filter-Id")
        admin_val = str(s.get("auth_radius_admin_val", "admin") or "admin").lower()
        role = "admin"
        display_name = ""

        if role_attr in reply:
            raw_value = reply[role_attr][0] if reply[role_attr] else b""
            if isinstance(raw_value, bytes):
                attr_val = raw_value.decode(errors="ignore")
            else:
                attr_val = str(raw_value)
            role = "admin" if admin_val in attr_val.lower() else "viewer"
            display_name = attr_val

        return SpokeUser(username=username, role=role, auth_provider="radius", display_name=display_name)
    except ImportError:
        logger.warning("pyrad not installed — RADIUS auth unavailable")
        return None
    except Exception as exc:
        logger.warning(f"RADIUS auth error for {username}: {exc}")
        return None


async def _radius_authenticate(username: str, password: str) -> "SpokeUser | None":
    return await asyncio.to_thread(_radius_authenticate_sync, username, password)


def _tacacs_authenticate_sync(username: str, password: str) -> "SpokeUser | None":
    """Blocking TACACS+ auth — call via asyncio.to_thread."""
    try:
        import tacacs_plus.client as tacacs

        s = settings
        if not s.get("auth_tacacs_host") or not s.get("auth_tacacs_secret"):
            return None

        client = tacacs.TACACSClient(
            host=s["auth_tacacs_host"],
            port=int(s.get("auth_tacacs_port", 49)),
            secret=str(s["auth_tacacs_secret"]).encode(),
            timeout=10,
        )

        authen = client.authenticate(username, password)
        if not getattr(authen, "valid", False):
            return None

        admin_priv = int(s.get("auth_tacacs_admin_priv", 15))
        author = client.authorize(username, arguments=[b"service=shell", b"cmd="])
        priv_level = 1
        for arg in (getattr(author, "arguments", None) or []):
            if b"priv-lvl=" in arg:
                try:
                    priv_level = int(arg.split(b"=", 1)[1])
                except Exception:
                    pass

        role = "admin" if priv_level >= admin_priv else "viewer"
        return SpokeUser(username=username, role=role, auth_provider="tacacs")
    except ImportError:
        logger.warning("tacacs-plus not installed — TACACS+ auth unavailable")
        return None
    except Exception as exc:
        logger.warning(f"TACACS+ auth error for {username}: {exc}")
        return None


async def _tacacs_authenticate(username: str, password: str) -> "SpokeUser | None":
    return await asyncio.to_thread(_tacacs_authenticate_sync, username, password)


app = FastAPI(title="Client Simulator", lifespan=lifespan)

# Prevent browser caching on all responses
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

class SpokeAuthMiddleware(BaseHTTPMiddleware):
    _PUBLIC_PREFIXES = ("/static/", "/api/auth/")
    # Paths accessible by simulation client devices (no browser session required)
    _PUBLIC_PATHS = ("/ws", "/ws/client", "/ws/proxmox", "/api/health", "/api/status", "/api/client/key")
    # Proxmox agent registration endpoints (no key yet at this stage)
    _PROXMOX_PUBLIC_PATHS = ("/api/proxmox/register", "/api/proxmox/key")

    async def dispatch(self, request: Request, call_next):
        if not _spoke_auth_required():
            return await call_next(request)
        path = request.url.path
        if (path == "/" or path.startswith(self._PUBLIC_PREFIXES) or path in self._PUBLIC_PATHS or request.method == "OPTIONS"):
            return await call_next(request)
        # Allow Proxmox agent registration before it has a key
        if path in self._PROXMOX_PUBLIC_PATHS:
            return await call_next(request)
        # Allow requests carrying a valid Proxmox agent API key
        api_key = request.headers.get("X-API-Key", "")
        if api_key and api_key in approved_proxmox_agents.values():
            return await call_next(request)
        token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
        user = _validate_spoke_session(token)
        if not user:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        ttl = _get_session_ttl()
        _spoke_sessions[token] = (user, time.time() + ttl)
        request.state.spoke_user = user
        if (
            user.role == "viewer"
            and path.startswith("/api/")
            and not path.startswith("/api/auth/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            response = JSONResponse({"detail": "Viewer role cannot modify data"}, status_code=403)
            response.set_cookie(_SPOKE_SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=ttl)
            return response
        response = await call_next(request)
        response.set_cookie(_SPOKE_SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=ttl)
        return response

app.add_middleware(SpokeAuthMiddleware)
app.add_middleware(NoCacheMiddleware)
clients: dict[str, dict[str, Any]] = _load_client_history()

# ── Command inbox ──────────────────────────────────────────────────────────────
commands: list[dict[str, Any]] = []
COMMAND_MAX = 100                 # keep last N commands in memory
COMMAND_EXPIRE_SECS = 900         # pending/delivered commands expire after 15 minutes
COMMAND_RESULT_RETENTION_SECS = 86400  # keep completed/expired results for 24 hours

ws_connections: list[WebSocket] = []
client_ws_connections: dict[str, WebSocket] = {}
state.proxmox_ws_connection = None
state.proxmox_ws_hostname = None
state.proxmox_ws_disconnect_task = None
PROXMOX_WS_GRACE_SECS = 30
_acme_challenges: dict[str, str] = {}
_acme_status: dict[str, Any] = {"running": False, "last_result": None, "last_error": None}
state_lock = asyncio.Lock()
repo_state = {"synced": False, "error": None, "last_sync": None}
gkill_switch_state: dict[str, Any] = {"value": "off", "last_fetched": None, "error": None}
# ── Command trace ring buffer ───────────────────────────────────────────────────
# Captures key events in the hub→spoke→agent relay pipeline so they can be
# fetched via /api/debug/command-trace for live debugging without SSH access.
_COMMAND_TRACE_MAX = 300
_command_trace: list[dict[str, Any]] = []

def _trace(event: str, **kwargs: Any) -> None:
    """Append a timestamped event to the command trace ring buffer."""
    entry = {"t": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    _command_trace.append(entry)
    if len(_command_trace) > _COMMAND_TRACE_MAX:
        del _command_trace[: len(_command_trace) - _COMMAND_TRACE_MAX]
# Queues for proxmox token provision responses relayed from agent → spoke → hub
_proxmox_token_provision_queues: dict[str, asyncio.Queue] = {}
GKILL_SWITCH_URL = "https://raw.githubusercontent.com/lbockenstedt/cs/main/configs/kill_switch.txt"
relay_state: dict[str, Any] = {
    "enabled": settings.get("relay_enabled") == "on" and bool(settings.get("relay_server_url")),
    "connected": False,
    "last_sync": None,
    "error": None,
    "registration_status": _relay_registration_status_from_settings(),
    "api_key_configured": bool(settings.get("relay_api_key")),
    # Diagnostic counters — surfaced in telemetry so the hub can detect instability.
    "ws_reconnect_count": 0,   # incremented on each successful WS connection (>0 means it reconnected)
    "ws_last_error": None,      # last exception string that caused a WS disconnect
    "ws_last_reconnect_at": None,  # ISO UTC timestamp of last successful WS (re)connect
    "telemetry_build_ms": None, # how long the last _build_relay_telemetry_payload call took
    "hub_loop_lag_ms": None,    # hub event-loop lag reported in the last telemetry_ack
}
state.relay_registration_refresh_needed = bool(relay_state["enabled"])
# Capped registration diagnostic log — last 50 attempts
_RELAY_DIAG_MAX = 50
relay_diag_log: list[dict[str, Any]] = []
state._relay_ws_send_json = None
state._relay_ws_spoke_id = None
_shell_sessions: dict[str, dict[str, Any]] = {}
state._repo_ver = None
state._proxmox_reseed_in_progress = False

# Hub-synced monitored items — fetched each relay cycle, cached here
state._hub_monitored_items = {"items": [], "has_sites": False, "assigned_sites": []}


def _relay_diag_append(event: str, **kwargs: Any) -> None:
    """Append one entry to relay_diag_log, keeping the list capped."""
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kwargs}
    relay_diag_log.append(entry)
    if len(relay_diag_log) > _RELAY_DIAG_MAX:
        del relay_diag_log[:-_RELAY_DIAG_MAX]


def _default_provision_run_state() -> dict[str, Any]:
    return {
        "running": False,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "total": 0,
        "completed": 0,
        "failed": 0,
        "items": [],
    }


proxmox_state: dict[str, Any] = {
    "connected": False,
    "last_seen": None,
    "node": {},
    "vms": None,  # None = never received telemetry; [] = received but empty (all deleted)
    "unknown_usb": [],
    "usb_state": [],
    "present_usb": [],
    "blacklisted_drivers": [],
    "missing_timeout_mins": 60,
    "agent_version": None,
    "pve_version": None,
    "template_lock": "",
    "vm_set_override": 0,
    "effective_vm_set": 1,
    "prov_summary": None,   # {"action": "provisioned"|"deleted", "count": N, "at": <unix ts>}
    "prov_run": _default_provision_run_state(),
}
# Previous usb_state vmid→prov_status snapshot for transition detection
state._prev_usb_by_vmid = {}
# VMIDs for which a delete command has been queued but not yet confirmed by telemetry.
# Kept as a set so the UI can show "deleting…" immediately instead of the row vanishing.
_pending_delete_vmids: set[int] = set()
# Cooldown: earliest time a new auto-delete may be queued (updated after each
# confirmed deletion so the fleet has time to stabilise before the next one).
state._delete_gate_cooldown_until = 0.0
DELETE_GATE_COOLDOWN_S: int = 300  # 5 minutes between consecutive auto-deletes
# VMID gap audit: detect and repair out-of-order VMID assignments.
# Runs at most once per host per interval (even if multiple telemetry cycles occur).
_vmid_gap_audit_last_run: dict[str, float] = {}
VMID_AUDIT_INTERVAL_S: int = 300  # 5 minutes between gap audit checks per host
# Throttle auto-provision gate debug logging to at most once per 120s per reason key.
_autoprov_gate_log_ts: dict[str, float] = {}
_AUTOPROV_GATE_LOG_INTERVAL = 120.0


def _autoprov_gate_log(reason_key: str, msg: str, *args: object) -> None:
    """Log an auto-provision gate decision at most once per _AUTOPROV_GATE_LOG_INTERVAL seconds."""
    now = time.time()
    if now - _autoprov_gate_log_ts.get(reason_key, 0.0) >= _AUTOPROV_GATE_LOG_INTERVAL:
        _autoprov_gate_log_ts[reason_key] = now
        logger.info("Auto-prov gate [%s]: " + msg, reason_key, *args)
# Maps vmid → approved hostname of the agent that last reported that VM.
# Used to route delete_vm / reclone_vm commands to the correct node in multi-agent setups.
_proxmox_agent_vm_map: dict[int, str] = {}
# Rolling resource samples for 1-hour average CPU/memory threshold checks.
# Each entry is (unix_timestamp, value_percent).  Pruned to the last hour on each update.
state._cpu_samples = []
state._mem_samples = []
state._resource_samples_started = 0.0  # epoch when first sample was recorded
_RESOURCE_SAMPLE_WINDOW = 3600  # seconds (1 hour)

# ── Proxmox VM simulation tags ────────────────────────────────────────────────
# Tracks which sim tags we last applied per (agent_hostname, vmid) to avoid
# redundant API calls.  Keyed this way because VMIDs can collide across nodes.
state._vm_applied_sim_tags = {}
_SIM_TAG_PREFIX = "sim-"


def _merge_sim_tags(current_tags_str: str, desired_sim_tags: list[str]) -> str:
    """Replace only sim-prefixed tags while preserving any manual Proxmox tags."""
    existing = [t.strip() for t in current_tags_str.split(';') if t.strip()]
    non_sim = [t for t in existing if not t.lower().startswith(_SIM_TAG_PREFIX)]
    merged = non_sim + sorted(set(t for t in desired_sim_tags if t))
    return ';'.join(merged)


def _sanitize_vm_set_override(value: Any) -> int:
    try:
        bucket = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return bucket if 1 <= bucket <= 99 else 0


def _hostname_vm_set_number(hostname: Any) -> int:
    match = re.search(r"(\d+)$", _normalize_proxmox_hostname(hostname))
    if not match:
        return 1
    try:
        bucket = int(match.group(1))
    except (TypeError, ValueError):
        return 1
    return max(1, bucket)


async def _apply_sim_tags_for_vm(vmid: int, agent_hostname: str, desired_sim_tags: list[str], current_tags_str: str = "") -> None:
    """Call Proxmox REST API to update simulation tags on a single VM."""
    desired_set = frozenset(t for t in desired_sim_tags if t)
    cache_key = (agent_hostname, vmid)
    if state._vm_applied_sim_tags.get(cache_key) == desired_set:
        return  # no change since last apply
    api_token = _get_proxmox_token_for_host(agent_hostname)
    if not api_token or not agent_hostname:
        return
    node_data = proxmox_states.get(agent_hostname, {}).get("node") or {}
    node = str(node_data.get("hostname") or agent_hostname).split(".")[0]
    url = f"https://{agent_hostname}:8006/api2/json/nodes/{node}/qemu/{vmid}/config"
    headers = {"Authorization": f"PVEAPIToken={api_token}"}
    merged = _merge_sim_tags(current_tags_str, list(desired_set))
    try:
        async with httpx.AsyncClient(verify=False, timeout=8) as hc:
            resp = await hc.put(url, headers=headers, data={"tags": merged})
        if resp.status_code == 200:
            state._vm_applied_sim_tags[cache_key] = desired_set
            logger.debug("VM %s tags updated: %s", vmid, merged or "(cleared)")
        else:
            logger.debug("VM %s tag update failed: HTTP %s", vmid, resp.status_code)
    except Exception as exc:
        logger.debug("VM %s tag update error: %s", vmid, exc)


async def _sync_sim_tags_for_client(client_hostname: str) -> None:
    """Immediately sync simulation tags for the VM matching this client hostname."""
    if not _has_any_proxmox_token():
        return
    norm = client_hostname.strip().lower()
    async with state_lock:
        client_data = clients.get(client_hostname) or clients.get(norm)
        if not client_data:
            return
        online = compute_online(client_data.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
        sim_tags = [_sanitize_proxmox_tag(s) for s in client_data.get("active_simulations", []) if str(s).strip()] if online else []
        found_vmid: int | None = None
        found_agent: str | None = None
        current_tags = ""
        for agent_hn, st in proxmox_states.items():
            for vm in (st.get("vms") or []):
                if str(vm.get("name") or "").strip().lower() == norm and not vm.get("is_template") and vm.get("type") != "lxc":
                    found_vmid = int(vm["vmid"])
                    found_agent = agent_hn
                    current_tags = str(vm.get("tags") or "")
                    break
            if found_vmid:
                break
    if found_vmid and found_agent:
        await _apply_sim_tags_for_vm(found_vmid, found_agent, sim_tags, current_tags)


async def _sync_all_vm_sim_tags() -> None:
    """Sweep all known VMs and reconcile their simulation tags against live client data."""
    if not _has_any_proxmox_token():
        return
    async with state_lock:
        now_dt = utcnow()
        client_sim_map: dict[str, tuple[list[str], str]] = {}  # norm_hostname → (sim_tags, current_tags)
        for hn, c in clients.items():
            online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
            tags = [_sanitize_proxmox_tag(s) for s in c.get("active_simulations", []) if str(s).strip()] if online else []
            client_sim_map[hn.strip().lower()] = tags
        agent_vms: list[tuple[str, dict]] = [
            (hn, vm)
            for hn, st in proxmox_states.items()
            for vm in (st.get("vms") or [])
        ]
    for agent_hn, vm in agent_vms:
        vmid = vm.get("vmid")
        vm_name = str(vm.get("name") or "").strip().lower()
        if not vmid or not vm_name or vm.get("is_template") or vm.get("type") == "lxc":
            continue
        desired_sim_tags = client_sim_map.get(vm_name)
        if desired_sim_tags is None:
            continue  # not a managed client VM — don't touch tags
        current_tags = str(vm.get("tags") or "")
        await _apply_sim_tags_for_vm(int(vmid), agent_hn, desired_sim_tags, current_tags)


async def _vm_sim_tag_sync_loop() -> None:
    """Background loop: sync simulation tags every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        try:
            await _sync_all_vm_sim_tags()
        except Exception as exc:
            logger.warning("VM sim tag sync loop error: %s", exc)


# ── Server backpressure ───────────────────────────────────────────────────────
# Tracks current API/WebSocket load and signals sim clients to slow down when
# the event loop is saturated so that the spoke stays responsive.
# Throttle levels and their client reporting intervals (seconds):
_BP_LEVELS = [
    # (event_loop_lag_threshold_s, throttle_interval_s, level_name)
    (0.0,   15, "normal"),
    (0.30,  30, "medium"),
    (1.00,  60, "high"),
]
# How long (s) to stay at a level before stepping back down one level.
_BP_HOLD_SECONDS = {"high": 60, "medium": 30, "normal": 0}

_server_pressure: dict[str, Any] = {
    "active": False,
    "level": "normal",
    "throttle_interval": 15,
    "reason": "",
    "since": 0.0,
    "held_since": 0.0,   # when we last transitioned to the current level
}
# Rolling event-loop lag samples (epoch, lag_seconds)
_loop_lag_samples: list[float] = []
_LOOP_LAG_WINDOW = 10  # number of samples to keep


async def _broadcast_server_pressure() -> None:
    """Push current pressure state to browser WS clients and throttle to sim clients."""
    msg = {
        "type": "server_pressure",
        "active": _server_pressure["active"],
        "level": _server_pressure["level"],
        "throttle_interval": _server_pressure["throttle_interval"],
        "reason": _server_pressure["reason"],
    }
    # Broadcast to browser clients
    await broadcast(msg)
    # Push throttle directive to all connected sim-client WS sessions concurrently
    throttle_msg = json.dumps({"type": "throttle", "interval": _server_pressure["throttle_interval"]})
    async def _send_one(ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.send_text(throttle_msg), timeout=3.0)
        except Exception:
            pass
    if client_ws_connections:
        await asyncio.gather(*(_send_one(ws) for ws in list(client_ws_connections.values())))


async def _event_loop_lag_monitor() -> None:
    """Background task: measures asyncio event-loop lag and manages backpressure."""
    import random
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - t0 - 1.0  # positive means loop was blocked

        _loop_lag_samples.append(lag)
        if len(_loop_lag_samples) > _LOOP_LAG_WINDOW:
            _loop_lag_samples.pop(0)

        if len(_loop_lag_samples) < 3:
            continue  # not enough data yet

        avg_lag = sum(_loop_lag_samples[-5:]) / min(5, len(_loop_lag_samples))
        now = time.monotonic()

        # Determine target level from current average lag
        target_level = "normal"
        for threshold, interval, level in reversed(_BP_LEVELS):
            if avg_lag >= threshold:
                target_level = level
                break

        current_level = _server_pressure["level"]

        # Step up immediately if things get worse
        level_order = ["normal", "medium", "high"]
        curr_idx = level_order.index(current_level)
        tgt_idx = level_order.index(target_level)

        if tgt_idx > curr_idx:
            # Escalate immediately
            new_level = target_level
        elif tgt_idx < curr_idx:
            # Only step down one level at a time after hold period
            hold = _BP_HOLD_SECONDS.get(current_level, 30)
            if now - _server_pressure["held_since"] >= hold:
                new_level = level_order[curr_idx - 1]
            else:
                new_level = current_level
        else:
            new_level = current_level

        if new_level != current_level:
            interval = next(i for _, i, l in _BP_LEVELS if l == new_level)
            _server_pressure.update({
                "active": new_level != "normal",
                "level": new_level,
                "throttle_interval": interval,
                "reason": f"Event loop lag: {avg_lag * 1000:.0f}ms avg",
                "since": time.time() if new_level != "normal" else 0.0,
                "held_since": now,
            })
            logger.info("Server pressure: %s → %s (lag=%.0fms, interval=%ds)",
                        current_level, new_level, avg_lag * 1000, interval)
            await _broadcast_server_pressure()


def _resource_1h_average(samples: list[tuple[float, float]]) -> float | None:
    """Return the rolling mean of all samples within the last hour.

    Returns the average of whatever samples exist as soon as the first one
    arrives — no warm-up delay.  Returns None only when no samples have been
    recorded yet (i.e. no telemetry received since startup).
    Older samples outside the 1-hour window are already pruned by
    _record_resource_samples(), so this always reflects recent history.
    """
    if not samples:
        return None
    cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
    recent = [v for ts, v in samples if ts >= cutoff]
    return sum(recent) / len(recent) if recent else None


def _resource_estimated_average(samples: list[tuple[float, float]]) -> float | None:
    """Return the mean of all available samples regardless of warmup status.

    Used to show a live estimate in the UI while the 1-hour window fills.
    Returns None only when no samples have been recorded yet.
    """
    vals = [v for _, v in samples]
    return sum(vals) / len(vals) if vals else None


def _record_resource_samples(node: dict[str, Any], now: float) -> None:
    """Append a CPU and memory sample from the latest node telemetry."""
    cpu_pct = node.get("cpu_percent")
    mem_used = node.get("mem_used_kb")
    mem_total = node.get("mem_total_kb")
    cutoff = now - _RESOURCE_SAMPLE_WINDOW
    if cpu_pct is not None:
        if not state._resource_samples_started:
            state._resource_samples_started = now
        state._cpu_samples.append((now, float(cpu_pct)))
        state._cpu_samples[:] = [(ts, v) for ts, v in state._cpu_samples if ts >= cutoff]
    try:
        if mem_used is not None and mem_total:
            mem_total_f = float(mem_total)
            if mem_total_f > 0:
                if not state._resource_samples_started:
                    state._resource_samples_started = now
                mem_pct = (float(mem_used) / mem_total_f) * 100.0
                state._mem_samples.append((now, mem_pct))
                state._mem_samples[:] = [(ts, v) for ts, v in state._mem_samples if ts >= cutoff]
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    _save_resource_cache()
_RESOURCE_CACHE_SAVE_INTERVAL = 60.0  # persist at most once per minute
state._resource_cache_last_saved = 0.0


def _load_resource_cache() -> None:
    """Restore resource samples from disk so restarts don't reset the 1-hour window."""
    try:
        if not RESOURCE_CACHE_FILE.exists():
            return
        data = json.loads(RESOURCE_CACHE_FILE.read_text())
        cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
        loaded_cpu = [(float(ts), float(v)) for ts, v in (data.get("cpu_samples") or []) if float(ts) >= cutoff]
        loaded_mem = [(float(ts), float(v)) for ts, v in (data.get("mem_samples") or []) if float(ts) >= cutoff]
        started = float(data.get("started") or 0)
        state._cpu_samples = loaded_cpu
        state._mem_samples = loaded_mem
        state._resource_samples_started = started if started > 0 else 0.0
        # Restore agent/pve version so hub Details shows versions immediately after restart
        if data.get("agent_version"):
            proxmox_state["agent_version"] = data["agent_version"]
        if data.get("pve_version"):
            proxmox_state["pve_version"] = data["pve_version"]
        # Restore key proxmox_state fields so the hub sees last-known data immediately
        # after a spoke server restart (before the agent posts fresh telemetry).
        for field in ("vm_count", "usb_state", "present_usb", "provision_halt", "prov_run"):
            cache_key = f"px_{field}"
            if cache_key in data:
                proxmox_state[field] = data.get(cache_key)
        logger.info(
            "Loaded resource cache: %d CPU samples, %d mem samples (started %.0fs ago)",
            len(state._cpu_samples), len(state._mem_samples),
            time.time() - state._resource_samples_started if state._resource_samples_started else 0,
        )
    except Exception:
        logger.debug("Could not load resource cache from %s", RESOURCE_CACHE_FILE, exc_info=True)


def _save_resource_cache(force: bool = False) -> None:
    """Persist resource samples so the 1-hour window survives service restarts."""
    now = time.time()
    if not force and (now - state._resource_cache_last_saved) < _RESOURCE_CACHE_SAVE_INTERVAL:
        return
    state._resource_cache_last_saved = now
    try:
        _atomic_write_json(RESOURCE_CACHE_FILE, {
            "cpu_samples": state._cpu_samples,
            "mem_samples": state._mem_samples,
            "started": state._resource_samples_started,
            "agent_version": proxmox_state.get("agent_version"),
            "pve_version": proxmox_state.get("pve_version"),
            # Persist key proxmox_state fields so spoke server restarts don't blank
            # the hub's last-known view before the agent posts fresh telemetry.
            "px_vm_count": proxmox_state.get("vm_count"),
            "px_usb_state": proxmox_state.get("usb_state"),
            "px_present_usb": proxmox_state.get("present_usb"),
            "px_provision_halt": _current_provision_halt(),
            "px_prov_run": proxmox_state.get("prov_run"),
        })
    except Exception:
        logger.debug("Could not save resource cache to %s", RESOURCE_CACHE_FILE, exc_info=True)


# Ring buffer: last 500 agent log lines
proxmox_log_buffer: list[str] = []
PROXMOX_LOG_MAX = 500
proxmox_watchdog_log: list[dict[str, Any]] = []
PROXMOX_WATCHDOG_LOG_MAX = 100
# Server-side debug event ring buffer — captures connectivity and state events
_debug_log: list[dict[str, Any]] = []
_DEBUG_LOG_MAX = 100
_server_start_time: float = time.time()


def _debug_event(event: str, detail: str = "", **extra: Any) -> None:
    """Append a timestamped debug event to the ring buffer."""
    entry: dict[str, Any] = {"ts": time.time(), "event": event, "detail": detail, **extra}
    _debug_log.append(entry)
    if len(_debug_log) > _DEBUG_LOG_MAX:
        del _debug_log[:len(_debug_log) - _DEBUG_LOG_MAX]
# Pending/approved Proxmox agent registry
pending_proxmox_agents: dict[str, dict[str, Any]] = {}
approved_proxmox_agents: dict[str, str] = dict(settings.get("proxmox_approved_agents", {}))
reclone_state: dict[str, Any] = {
    "status": "idle",
    "type": None,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current_vm": None,
    "log": [],
    "auto_recovery_log": [],
    "last_run": None,
    "started_at": None,
}
vm_watchdog: dict[str, dict[str, Any]] = {}
state.update_all_state = {
    "running": False,
    "phase": "idle",
    "total_agents": 0,
    "completed_agents": 0,
    "failed_agents": 0,
    "agent_cmds": [],
    "started_at": None,
    "error": None,
}
relay_sites: dict[str, dict[str, Any]] = {}
background_tasks: dict[str, asyncio.Task[Any]] = {}
service_health: dict[str, dict[str, Any]] = {}
reclone_run_lock = asyncio.Lock()
state.last_schedule_trigger = None
state._hub_repo_sync_task = None  # dedup guard for fire-and-forget repo_sync

class ClientStatus(BaseModel):
    hostname: str
    simulation_id: str
    platform: str
    hw_type: str | None = None
    iteration: int
    connected_ssid: str | None = None
    gateway_reachable: bool
    active_simulations: list[str] = Field(default_factory=list)
    config: dict[str, str] = Field(default_factory=dict)
    # errors: list of human-readable error strings that occurred since the last
    # status report. The client accumulates them between reports and sends the
    # whole batch here. WHY: we want errors visible in the dashboard, not buried
    # in client-side log files that no operator can easily read remotely.
    errors: list[str] = Field(default_factory=list)


class ClientControlResponse(BaseModel):
    hostname: str
    overrides: dict[str, str]
    client: dict[str, Any]


class SettingsUpdate(BaseModel):
    proxmox_config: dict[str, Any] | None = None
    repo_branch: str | None = None
    github_token: str | None = None
    central_api: dict[str, Any] | None = None
    central_config: dict[str, str] | None = None
    site_mappings: dict[str, str] | None = None
    monitored_checks: list[dict[str, str]] | None = None
    hardware_checks: list[dict[str, str]] | None = None
    notifications: dict[str, Any] | None = None
    repo_sync_interval: int | None = None
    relay_enabled: str | None = None
    relay_server_url: str | None = None
    hub_tls_verify: str | None = None
    relay_spoke_name: str | None = None
    relay_tenant_hint: str | None = None
    relay_onboarding_psk: str | None = None
    relay_api_key: str | None = None
    relay_spoke_id: str | None = None
    relay_tenant_id: str | None = None
    relay_poll_interval: int | None = None
    hub_isolation_timeout: int | None = None  # Accept a caller-supplied isolation timeout so the setup UI can tune when stale hub contact pauses config pushes.
    admin_password: str | None = None
    session_timeout_minutes: int | None = None
    auth_provider: str | None = None
    auth_ldap_url: str | None = None
    auth_ldap_bind_dn: str | None = None
    auth_ldap_bind_password: str | None = None
    auth_ldap_user_base: str | None = None
    auth_ldap_user_filter: str | None = None
    auth_ldap_group_admin: str | None = None
    auth_ldap_group_viewer: str | None = None
    auth_radius_host: str | None = None
    auth_radius_port: int | None = None
    auth_radius_secret: str | None = None
    auth_radius_role_attr: str | None = None
    auth_radius_admin_val: str | None = None
    auth_tacacs_host: str | None = None
    auth_tacacs_port: int | None = None
    auth_tacacs_secret: str | None = None
    auth_tacacs_admin_priv: int | None = None
    usb_vidpids: str | None = None
    usb_missing_timeout: str | None = None
    usb_template_id: str | None = None
    vm_image_1_template_id: str | None = None
    vm_image_1_template_spec: str | None = None
    vm_image_2_template_id: str | None = None
    vm_image_2_template_spec: str | None = None
    vm_image_1_pct: str | None = None
    usb_auto_provision: str | None = None
    use_all_dongles: bool | None = None
    usb_max_slots: str | None = None
    cpu_provision_threshold: str | None = None
    cpu_delete_threshold: str | None = None
    mem_provision_threshold: str | None = None
    mem_delete_threshold: str | None = None
    vmid_start: int | None = None
    usb_ignored_vidpids: str | None = None
    ignored_hostnames: str | None = None
    vm_silent_timeout: str | None = None
    reclone_schedule_enabled: str | None = None
    reclone_schedule_cron: str | None = None
    reclone_concurrency: str | None = None
    protected_vmids: str | None = None
    l1_vlan_start: str | None = None
    l1_vlan_end: str | None = None
    spoke_tls: str | None = None
    guest_agent_watchdog_enabled: str | None = None
    guest_agent_grace_minutes: str | None = None
    guest_agent_check_interval_minutes: str | None = None
    guest_agent_reboot_after_minutes: str | None = None
    guest_agent_reclone_after_minutes: str | None = None
    watchdog_reboot_enabled: str | None = None
    proxmox_api_token: str | None = None


class SimulationConfigUpdate(BaseModel):
    section: str
    updates: dict[str, str] = Field(default_factory=dict)


class OverridesSaveRequest(BaseModel):
    username: str
    flags: dict[str, str] = Field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utcnow() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def compute_online(last_seen: datetime) -> bool:
    return (utcnow() - last_seen).total_seconds() <= OFFLINE_TIMEOUT


def _hostname_has_usb(hostname: str) -> bool:
    """Return True if the VM for this hostname has a USB dongle assigned in Proxmox config or usb_state."""
    vms: list[dict[str, Any]] = proxmox_state.get("vms") or []
    usb_state: list[dict[str, Any]] = proxmox_state.get("usb_state") or []
    norm = hostname.strip().lower()
    vmid: str | None = None
    for vm in vms:
        if str(vm.get("name") or "").strip().lower() == norm:
            raw = vm.get("vmid")
            if raw is not None:
                vmid = str(raw).strip()
            # has_usb_config is True if the Proxmox VM config has any USB passthrough line
            if vm.get("has_usb_config") or vm.get("reclone_bus_path"):
                return True
            break
    if not usb_state:
        return False
    usb_vmids = {
        str(d.get("vmid")).strip()
        for d in usb_state
        if isinstance(d, dict) and d.get("vmid") is not None and str(d.get("vmid")).strip()
    }
    if vmid and vmid in usb_vmids:
        return True
    # Fallback: match by hostname field on USB entry
    usb_hostnames = {
        str(d.get("hostname") or d.get("vm_name") or "").strip().lower()
        for d in usb_state
        if isinstance(d, dict)
    }
    usb_hostnames.discard("")
    return norm in usb_hostnames


def serialize_client(hostname: str, client: dict[str, Any]) -> dict[str, Any]:
    config = {key: str(value) for key, value in client.get("config", {}).items()}
    overrides = {key: str(value) for key, value in client.get("overrides", {}).items()}
    effective_config = {**config, **overrides}
    last_seen = client["last_seen"]
    online = compute_online(last_seen)

    return {
        "hostname": hostname,
        "has_usb": _hostname_has_usb(hostname),
        "simulation_id": client.get("simulation_id", ""),
        "platform": client.get("platform", ""),
        "hw_type": client.get("hw_type") or "",
        "iteration": client.get("iteration", 0),
        "connected_ssid": client.get("connected_ssid") or "",
        "gateway_reachable": bool(client.get("gateway_reachable", False)),
        "active_simulations": list(client.get("active_simulations", [])),
        "config": config,
        "effective_config": effective_config,
        "overrides": overrides,
        "last_seen": last_seen.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "online": online,
        # recent_errors: circular buffer of the last MAX_CLIENT_ERRORS entries.
        # Each entry has a timestamp and message so operators know when errors occurred.
        "recent_errors": list(client.get("recent_errors", [])),
        "error_count": int(client.get("error_count", 0)),
    }


async def current_clients() -> list[dict[str, Any]]:
    async with state_lock:
        return [serialize_client(hostname, clients[hostname]) for hostname in sorted(clients)]


def _normalize_toggle(value: Any) -> str:
    if isinstance(value, str):
        return "on" if value.lower() == "on" else "off"
    return "on" if value else "off"


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_json_list(value: str, field_name: str) -> str:
    try:
        parsed = json.loads(value or "[]")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail=f"{field_name} must be a JSON array")
    return json.dumps(parsed)


def _setting_int(key: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(str(settings.get(key, default)).strip())
    except (TypeError, ValueError, AttributeError):
        value = default
    return max(minimum, value)


def _setting_bool(key: str, default: bool = False) -> bool:
    value = settings.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _client_last_seen_for_hostname(hostname: Any, client_seen: dict[str, Any] | None = None) -> datetime | None:
    normalized = str(hostname or "").strip().lower()
    if not normalized:
        return None
    seen_map = client_seen or {name: info.get("last_seen") for name, info in clients.items()}
    for candidate, last_seen in seen_map.items():
        if str(candidate or "").strip().lower() == normalized and isinstance(last_seen, datetime):
            return last_seen
    return None


def _vm_has_checked_in(hostname: Any, clone_completed_at: float | None, client_seen: dict[str, Any] | None = None) -> bool:
    if clone_completed_at is None:
        return False
    last_seen = _client_last_seen_for_hostname(hostname, client_seen)
    return bool(last_seen and last_seen.timestamp() > float(clone_completed_at))


def _vm_pending_checkin(vm: dict[str, Any], client_seen: dict[str, Any] | None = None) -> bool:
    vmid_key = _vm_watchdog_key(vm.get("vmid"))
    if vmid_key is None:
        return False
    entry = vm_watchdog.get(vmid_key)
    if not entry:
        return False
    clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
    hostname = str(entry.get("hostname") or vm.get("name") or "").strip()
    return not _vm_has_checked_in(hostname, clone_completed_at, client_seen)


# Per-agent state tracking — hostname → state snapshot updated on each telemetry push.
# This is separate from the single proxmox_state which maintains backward compatibility.
proxmox_states: dict[str, dict[str, Any]] = {}


def _autoprov_enabled() -> bool:
    return _normalize_toggle(settings.get("usb_auto_provision", "off")) == "on"


def _current_provision_halt(state: dict[str, Any] | None = None) -> Any:
    if not _autoprov_enabled():
        return None
    source = proxmox_state if state is None else state
    return source.get("provision_halt")


def _clear_provision_halt_state() -> None:
    proxmox_state["provision_halt"] = None
    for state in proxmox_states.values():
        state["provision_halt"] = None
    _save_state_cache(force=True)
    _save_resource_cache(force=True)


def _client_os_counts() -> dict[str, int]:
    """Count connected clients by platform (linux/windows)."""
    counts: dict[str, int] = {"linux": 0, "windows": 0}
    for c in clients.values():
        platform = str(c.get("platform", "")).lower()
        if platform in counts:
            counts[platform] += 1
    return counts


def _read_local_kill_switch() -> str:
    """Read kill_switch from the repo's configs/simulation.conf without full parse."""
    try:
        conf = (REPO_DIR / "configs" / "simulation.conf").read_text(encoding="utf-8")
        for line in conf.splitlines():
            if line.strip().startswith("kill_switch"):
                val = line.split("=", 1)[-1].strip()
                return val if val in ("on", "off") else "off"
    except Exception:
        pass
    return "off"


def _prepare_delete_vm_args(args: dict[str, Any] | None, strict: bool = True) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")
    try:
        vmid = int(args.get("vmid"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="A valid vmid is required") from None

    vm = _find_proxmox_vm(vmid)
    if vm is None:
        # Allow re-delete of a VM that's already in pending-delete state (idempotent)
        if vmid in _pending_delete_vmids:
            return {"vmid": vmid, "vm_type": "qemu", "status": "deleting"}
        if strict:
            # 503 only when the inventory has never been received (None), not when it's empty
            # after a batch delete (which would be an empty list []).
            if proxmox_state.get("vms") is None:
                raise HTTPException(status_code=503, detail="No Proxmox VM inventory is available yet")
            raise HTTPException(status_code=404, detail=f"VM {vmid} was not found in Proxmox inventory")
        else:
            # Relay mode: inventory may be stale or not yet loaded — pass through with a
            # safe default vm_type so the proxmox agent can handle it (or report the error).
            logger.warning(
                "Hub relay delete_vm: VM %s not found in local inventory%s — forwarding anyway",
                vmid,
                " (inventory not loaded)" if proxmox_state.get("vms") is None else "",
            )
            return {"vmid": vmid, "vm_type": str(args.get("vm_type") or "qemu")}
    if vm.get("is_template"):
        raise HTTPException(status_code=400, detail="Templates cannot be deleted from the VM list")
    if _is_protected_vmid(vmid):
        raise HTTPException(status_code=403, detail=f"VM {vmid} is protected and cannot be managed from this UI")

    prepared = dict(args)
    prepared["vmid"] = vmid
    vm_type = str(vm.get("type") or "qemu").strip().lower()
    prepared["vm_type"] = vm_type if vm_type in {"qemu", "lxc"} else "qemu"
    if vm.get("name"):
        prepared["vm_name"] = str(vm.get("name"))
    return prepared


def _auto_recovery_pending_vmids() -> list[int]:
    """Return VMIDs that have a pending/delivered auto-recovery reclone command."""
    return [
        int(cmd.get("args", {}).get("vmid", -1))
        for cmd in commands
        if cmd.get("action") == "reclone_vm"
        and cmd.get("type") == "auto-recovery"
        and cmd.get("status") in {"pending", "delivered"}
        and cmd.get("args", {}).get("vmid") is not None
    ]


def _derive_provision_run_item_status(
    usb_entry: dict[str, Any],
    vm_by_vmid: dict[str, dict[str, Any]],
) -> str:
    if str(usb_entry.get("prov_status") or "").strip().lower() != "provisioning":
        return "done"
    vm = vm_by_vmid.get(str(usb_entry.get("vmid"))) or {}
    vm_status = str(vm.get("status") or "").strip().lower()
    return "configuring" if vm_status == "running" else "cloning"


def _unlock_template_result(cmd: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "queued": True,
        "task_type": "unlock_template",
        "detail": "Template unlock queued",
        "command_id": cmd.get("id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def auto_recovery_check() -> None:
    await asyncio.sleep(1800)
    while True:
        try:
            timeout_hours = _setting_int("vm_silent_timeout", 24, 1)
            now = time.time()
            triggered: list[int] = []
            for vm in list(proxmox_state.get("vms") or []):
                vmid = vm.get("vmid")
                if vmid is None:
                    continue
                if int(vmid) <= 9000 or vm.get("is_template"):
                    continue
                # Skip VMs that are being intentionally deleted
                if int(vmid) in _pending_delete_vmids:
                    continue
                last_seen = _parse_ts(vm.get("last_seen"))
                if last_seen is None or (now - last_seen) <= timeout_hours * 3600:
                    continue
                vmid_int = int(vmid)
                if _has_pending_reclone(vmid_int):
                    continue
                await _queue_proxmox_command("reclone_vm", {"vmid": vmid_int}, command_type="auto-recovery")
                triggered.append(vmid_int)
            if triggered:
                vmid_list = ", ".join(str(v) for v in triggered)
                for vmid_int in triggered:
                    name = next(
                        (vm.get("name") or f"VM {vmid_int}" for vm in proxmox_state.get("vms") or [] if int(vm.get("vmid", -1)) == vmid_int),
                        f"VM {vmid_int}",
                    )
                    reclone_state["auto_recovery_log"].append({
                        "vmid": vmid_int,
                        "name": name,
                        "status": "queued",
                        "timestamp": iso_utcnow(),
                    })
                reclone_state["auto_recovery_log"] = reclone_state["auto_recovery_log"][-50:]
                await _async_save_reclone_state()
                await broadcast({
                    "type": "notification",
                    "level": "warning",
                    "message": f"Auto-recovery: queued reclone for {len(triggered)} silent VM(s) — {vmid_list}",
                })
                await _broadcast_proxmox_state()
            _update_service_health("auto_recovery", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("auto_recovery", ok=False, error=str(exc))
            logger.exception("Auto recovery check error: %s", exc)
        await asyncio.sleep(1800)


async def schedule_check() -> None:
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    await asyncio.sleep(60)
    while True:
        try:
            if _normalize_toggle(settings.get("reclone_schedule_enabled", "off")) == "on" and reclone_state.get("status") != "running":
                parsed = _parse_reclone_schedule(settings.get("reclone_schedule_cron", "sunday 02:00"))
                if parsed:
                    day, hour, minute = parsed
                    now = datetime.now()
                    if day_names[now.weekday()] == day and now.hour == hour and now.minute == minute:
                        trigger_key = now.strftime("%Y-%m-%d %H:%M")
                        if state.last_schedule_trigger != trigger_key:
                            state.last_schedule_trigger = trigger_key
                            asyncio.create_task(_run_rolling_reclone("scheduled"))
            _update_service_health("schedule_check", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("schedule_check", ok=False, error=str(exc))
            logger.exception("Schedule check error: %s", exc)
        await asyncio.sleep(60)




async def gkill_switch_poller() -> None:
    """Fetch the global kill switch from solutions-hpe/main every 5 minutes.
    Serves as the authoritative value for /api/kill-switch — never relies on
    a local file so a forked repo cannot override it."""
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resp = await client.get(GKILL_SWITCH_URL)
                value = resp.text.strip().lower()
                if value not in ("on", "off"):
                    value = "off"
                prev = gkill_switch_state["value"]
                gkill_switch_state["value"] = value
                gkill_switch_state["last_fetched"] = time.time()
                gkill_switch_state["error"] = None
                if value != prev:
                    logger.warning("Global kill switch changed: %s → %s", prev, value)
                    await broadcast({"type": "gkill_switch_update", "value": value})
                _update_service_health("gkill_switch", ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                gkill_switch_state["error"] = str(exc)
                _update_service_health("gkill_switch", ok=False, error=str(exc))
                logger.warning("gkill_switch fetch failed: %s", exc)
            await asyncio.sleep(300)




async def broadcast(message: dict[str, Any]) -> None:
    if not ws_connections:
        return

    payload = json.dumps(message, default=str)
    stale: list[WebSocket] = []
    for websocket in list(ws_connections):
        try:
            await websocket.send_text(payload)
        except Exception:
            stale.append(websocket)

    for websocket in stale:
        with contextlib.suppress(ValueError):
            ws_connections.remove(websocket)


async def broadcast_full_state() -> None:
    await broadcast({"type": "full_state", "clients": await current_clients()})


def _relay_status_payload() -> dict[str, Any]:  # Build the relay status payload once so REST and websocket updates expose identical isolation data.
    return {  # Merge relay, spoke, and isolation fields so the browser can render complete hub status from one payload.
        **dict(relay_state),  # Preserve the existing relay status fields so current UI behavior keeps working.
        "spoke_id": settings.get("relay_spoke_id", ""),  # Include the spoke identifier so live relay broadcasts keep the setup status grid populated.
        "api_key_configured": bool(settings.get("relay_api_key")),  # Include API key state so relay status broadcasts do not clear the approval indicator.
        "spoke_name": settings.get("relay_spoke_name", ""),  # Include the display name so relay consumers always have the active spoke label.
        "hub_isolated": _hub_isolated(),  # Publish the current isolation flag so the browser can pause hub-push messaging in the UI immediately.
        "hub_last_checkin": relay_state.get("last_sync"),  # Publish the last successful check-in timestamp so the UI and telemetry share one source of truth.
        "hub_isolation_timeout": int(settings.get("hub_isolation_timeout", 3600)),  # Publish the configured timeout so clients can explain why isolation was triggered.
    }  # Return one enriched relay payload so every broadcast and endpoint exposes isolation details consistently.


async def _broadcast_relay_state() -> None:
    _save_relay_state()
    await broadcast({"type": "relay_status", **_relay_status_payload()})


def _build_registration_config() -> dict[str, Any]:
    """Build the seed config payload sent to hub on first registration."""
    return {
        "repo_branch": settings.get("repo_branch") or REPO_BRANCH,
        "repo_url": REPO_URL,
        "site_mappings": settings.get("site_mappings", {}),
        "monitored_checks": settings.get("monitored_checks", []),
        "hardware_checks": settings.get("hardware_checks", []),
        "reclone_schedule_enabled": settings.get("reclone_schedule_enabled", "off"),
        "reclone_schedule_cron": settings.get("reclone_schedule_cron", "sunday 02:00"),
        "reclone_concurrency": settings.get("reclone_concurrency", "1"),
        "protected_vmids": settings.get("protected_vmids", ""),
        "vm_image_1_template_id": settings.get("vm_image_1_template_id", "100"),
        "vm_image_1_template_spec": settings.get("vm_image_1_template_spec", _resolved_template_spec(settings, 1)),
        "vm_image_2_template_id": settings.get("vm_image_2_template_id", "200"),
        "vm_image_2_template_spec": settings.get("vm_image_2_template_spec", _resolved_template_spec(settings, 2)),
        "vm_image_1_pct": settings.get("vm_image_1_pct", "50"),
        "usb_auto_provision": settings.get("usb_auto_provision", "off"),
        "usb_max_slots": settings.get("usb_max_slots", "24"),
        "cpu_provision_threshold": settings.get("cpu_provision_threshold", "80"),
        "cpu_delete_threshold": settings.get("cpu_delete_threshold", "90"),
        "mem_provision_threshold": settings.get("mem_provision_threshold", "80"),
        "mem_delete_threshold": settings.get("mem_delete_threshold", "90"),
        "usb_missing_timeout": settings.get("usb_missing_timeout", "60"),
        "vm_silent_timeout": settings.get("vm_silent_timeout", "24"),
        "ignored_hostnames": settings.get("ignored_hostnames", '["sim-rpi-0000"]'),
        "l1_vlan_start": settings.get("l1_vlan_start", "100"),
        "l1_vlan_end": settings.get("l1_vlan_end", "199"),
        "hub_tls_verify": settings.get("hub_tls_verify", "off"),
        "guest_agent_watchdog_enabled": settings.get("guest_agent_watchdog_enabled", "on"),
        "guest_agent_grace_minutes": settings.get("guest_agent_grace_minutes", "20"),
        "guest_agent_check_interval_minutes": settings.get("guest_agent_check_interval_minutes", "10"),
        "guest_agent_reboot_after_minutes": settings.get("guest_agent_reboot_after_minutes", "10"),
        "guest_agent_reclone_after_minutes": settings.get("guest_agent_reclone_after_minutes", "30"),
        "watchdog_reboot_enabled": settings.get("watchdog_reboot_enabled", "on"),
    }


def _relay_ws_url(server_url: str, tenant_id: str, spoke_id: str, api_key: str) -> str:
    hub_base = _relay_hub_base_url(server_url, tenant_id)
    if hub_base.startswith("https://"):
        hub_base = "wss://" + hub_base[len("https://"):]
    elif hub_base.startswith("http://"):
        hub_base = "ws://" + hub_base[len("http://"):]
    return f"{hub_base}/api/{tenant_id}/spokes/{spoke_id}/ws?api_key={api_key}"


async def _build_relay_telemetry_payload(spoke_id: str) -> dict[str, Any]:
    async with state_lock:
        proxmox_vms = list(proxmox_state.get("vms") or [])
        usb_state = list(proxmox_state.get("usb_state", []))
        clients_snapshot = [serialize_client(hostname, clients[hostname]) for hostname in sorted(clients)]
    present_usb = list(proxmox_state.get("present_usb", []))
    unknown_usb = list(proxmox_state.get("unknown_usb", []))

    # Enrich the VM list with prov_status from usb_state so the hub can show
    # provisioning status without a separate lookup. Also synthesize entries for
    # VMs that are tracked in usb_state as "provisioning" but not yet present in
    # the Proxmox VM list (mid-clone race window).
    usb_by_vmid: dict[str, dict[str, Any]] = {
        str(e.get("vmid")): e for e in usb_state if e.get("vmid") is not None
    }
    enriched_vms: list[dict[str, Any]] = []
    existing_vmids: set[str] = set()
    for vm in proxmox_vms:
        enriched = dict(vm)
        vmid_key = str(vm.get("vmid", ""))
        usb_entry = usb_by_vmid.get(vmid_key)
        enriched["prov_status"] = (usb_entry.get("prov_status") or "active") if usb_entry else "active"
        existing_vmids.add(vmid_key)
        enriched_vms.append(enriched)
    # Add synthetic VM entries for usb_state slots still in "provisioning" that
    # Proxmox hasn't surfaced yet (clone not complete).
    for vmid_key, entry in usb_by_vmid.items():
        if vmid_key not in existing_vmids and str(entry.get("prov_status", "")).lower() == "provisioning":
            enriched_vms.append({
                "vmid": entry.get("vmid"),
                "name": entry.get("hostname") or f"VM {entry.get('vmid')}",
                "status": "provisioning",
                "type": "qemu",
                "prov_status": "provisioning",
            })

    # Read raw simulation.conf so the hub can populate the conf editor when
    # there is no GitHub API key and no saved hub override yet.
    # Content is kept fresh by _sim_conf_content_refresh_loop (runs every 30s in a
    # thread-pool worker) so this dict-read never blocks the event loop.
    sim_conf_content = _sim_conf_content_cache["content"]
    user_overrides_conf_content = _user_overrides_conf_content_cache["content"]

    return {
        "spoke_id": spoke_id,
        "spoke_name": settings.get("relay_spoke_name", "").strip() or socket.gethostname(),
        "hostname": socket.gethostname(),
        "clients": clients_snapshot,
        "timestamp": time.time(),
        "sim_conf_content": sim_conf_content,
        "user_overrides_conf_content": user_overrides_conf_content,
        "hub_isolated": _hub_isolated(),  # Export the current isolation state so the hub can see when this spoke has paused config pushes.
        "hub_last_checkin": relay_state.get("last_sync"),  # Export the last successful check-in timestamp so the hub can reason about isolation timing.
        "hub_rtt_ms": relay_state.get("hub_rtt_ms"),  # Round-trip time from last telemetry send to ack receipt.
        "hub_processing_ms": relay_state.get("hub_processing_ms"),  # Hub-reported time to process and save telemetry.
        "hub_loop_lag_ms": relay_state.get("hub_loop_lag_ms"),  # Hub event-loop lag reported in last ack — high values indicate hub blocking.
        "telemetry_build_ms": relay_state.get("telemetry_build_ms"),  # How long the last payload build took; high values indicate spoke event-loop blocking.
        "ws_reconnect_count": relay_state.get("ws_reconnect_count", 0),  # WS reconnect counter; non-zero means the spoke has had connection drops.
        "ws_last_reconnect_at": relay_state.get("ws_last_reconnect_at"),  # ISO UTC timestamp of last successful WS (re)connect.
        "ws_last_error": relay_state.get("ws_last_error"),  # Last WS disconnect reason.
        "sim_conf_read_error": _sim_conf_content_cache.get("error"),  # Non-None if the background sim_conf reader is failing (e.g. FS stall).
        "reseed_in_progress": bool(state._proxmox_reseed_in_progress),
        "proxmox": {
            "connected": bool(proxmox_state.get("connected", False)),
            "last_seen": proxmox_state.get("last_seen"),
            "node": dict(proxmox_state.get("node") or {}),
            "vm_count": sum(1 for vm in enriched_vms if not vm.get("is_template")),
            "running_count": sum(1 for vm in enriched_vms if vm.get("status") == "running" and not vm.get("is_template")),
            "vms": [
                {
                    "vmid": vm.get("vmid"),
                    "name": vm.get("name", ""),
                    "status": vm.get("status", ""),
                    "type": vm.get("type", ""),
                    "cpu": vm.get("cpu"),
                    "mem": vm.get("mem"),
                    "maxmem": vm.get("maxmem"),
                    "is_template": vm.get("is_template", False),
                    "has_usb_config": vm.get("has_usb_config", False),
                    "reclone_bus_path": vm.get("reclone_bus_path"),
                    "pci_passthrough_addrs": vm.get("pci_passthrough_addrs") or [],
                    "prov_status": vm.get("prov_status", "active"),
                }
                for vm in enriched_vms
            ],
            "usb_state": usb_state,
            "present_usb": present_usb,
            "unknown_usb": unknown_usb,
            "usb_count": len(present_usb) if present_usb else len(usb_state),
            "agent_version": proxmox_state.get("agent_version"),
            "pve_version": proxmox_state.get("pve_version"),
            "cpu_1h_avg": _resource_1h_average(state._cpu_samples),
            "mem_1h_avg": _resource_1h_average(state._mem_samples),
            "provision_halt": _current_provision_halt(),
            "prov_run": dict(proxmox_state.get("prov_run") or {}),
            "cpu_est_avg": _resource_estimated_average(state._cpu_samples),
            "mem_est_avg": _resource_estimated_average(state._mem_samples),
            "resource_samples_started": state._resource_samples_started or None,
            "resource_sample_count": len(state._cpu_samples),
            "template_lock": str(proxmox_state.get("template_lock") or ""),
            "reseed_in_progress": bool(state._proxmox_reseed_in_progress),
            "hw_faults": proxmox_state.get("hw_faults") or {},
            "hw_last_reset": proxmox_state.get("hw_last_reset"),
            # T3 IoT PCI devices found on this Proxmox node — list of {id, vidpid, name} dicts.
            # Used by the hub to classify this spoke as a T3 host and render per-node counts.
            "t3_pci_devices": list(proxmox_state.get("t3_pci_devices") or []),
            "t3_pci_count": len(proxmox_state.get("t3_pci_devices") or []),
            "blacklisted_drivers": list(proxmox_state.get("blacklisted_drivers") or []),
            "usb_quarantine": list(proxmox_state.get("usb_quarantine") or []),
            "orphan_vms": list(proxmox_state.get("orphan_vms") or []),
        },
            "proxmox_vms": proxmox_vms,
            "usb_devices": usb_state,
            "api_server": {
                "health": {
                    "status": "ok",
                    "version": APP_VERSION,
                    "clients": len(clients_snapshot),
                    "repo_synced": repo_state["synced"],
                    "repo_error": repo_state["error"],
                    "installer_version": INSTALLER_VERSION,
                },
                "services": {name: dict(info) for name, info in service_health.items()},
                "task_names": list(background_tasks.keys()),
            },
            "central": {
                "status": _central_status_payload(),
                "wireless_clients": dict(state.central_wireless_clients),
                "hardware_alerts": _hw_alerts_payload(),
                "client_count_status": _client_count_payload(),
                "token_valid": bool(central_token.get("access_token") and time.time() < central_token.get("expires_at", 0)),
                "token_state": _central_token_state(),
                "site_mappings": dict(settings.get("site_mappings", {})),
                "monitored_checks": list(settings.get("monitored_checks", [])),
                "hardware_checks": list(settings.get("hardware_checks", [])),
                # Filter browse data to only the sites this spoke is responsible for.
                # We now fetch ALL Central sites locally (for the browse tab), but the
                # hub should only see data for sites assigned to this spoke — otherwise
                # the hub's distributed aggregation gets duplicate entries for sites
                # shared between spokes or orphan alerts for unassigned sites.
                "central_alerts": _telemetry_filtered_browse_list(state.central_browse_alerts, "site"),
                "central_insights": _telemetry_filtered_browse_list(state.central_browse_insights, "site"),
                "central_devices_by_site": _telemetry_filtered_browse_dict(state.central_browse_devices_by_site),
                "central_clients_by_site": _telemetry_filtered_browse_dict(state.central_browse_clients_by_site),
                # Individual client records (filtered to assigned sites) for hub distributed aggregation
                "central_clients": _telemetry_filtered_browse_list(state.central_browse_clients, "site"),
            },
            "reclone_state": {
                k: v for k, v in reclone_state.items() if k != "auto_recovery_log"
            },
        }


# ── VNC relay ─────────────────────────────────────────────────────────────────

_vnc_sessions: dict[str, asyncio.Queue] = {}
_direct_console_sessions: dict[str, dict[str, Any]] = {}
_DIRECT_CONSOLE_TTL = 60  # seconds until session token expires

async def _handle_log_fetch(message: dict[str, Any]) -> None:
    """Fetch log lines from journal/agent/watchdog/install and send back to hub."""
    request_id = str(message.get("request_id") or "").strip()
    source = str(message.get("source") or "journal").strip().lower()
    lines = min(max(int(message.get("lines") or 200), 10), 2000)

    if not request_id:
        return

    async def _send_response(log_lines: list[str], error: str | None = None) -> None:
        if state._relay_ws_send_json is None:
            return
        out: dict[str, Any] = {
            "type": "log_fetch_response",
            "request_id": request_id,
            "source": source,
        }
        if error:
            out["error"] = error
        else:
            out["lines"] = log_lines
        if state._relay_ws_spoke_id:
            out["spoke_id"] = state._relay_ws_spoke_id
        await state._relay_ws_send_json(out)

    try:
        if source == "agent":
            log_lines = [str(l) for l in proxmox_log_buffer[-lines:]]
            if not log_lines:
                log_lines = ["[INFO] No Proxmox agent logs yet."]
            await _send_response(log_lines)
            return

        if source == "watchdog":
            log_path = Path("/var/log/proxmox-watchdog.log")
        elif source == "install":
            log_path = Path("/var/log/client-sim-dashboard-install.log")
        else:
            log_path = None

        if log_path is not None:
            if not log_path.exists():
                await _send_response([f"[INFO] {log_path} does not exist yet."])
                return
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(log_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            text = stdout.decode("utf-8", errors="replace").strip()
            await _send_response(text.splitlines() if text else [f"[INFO] {log_path} is empty."])
            return

        # journal
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "client-sim-dashboard", "--no-pager", "-n", str(lines),
            "--output=short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            err = stderr.decode("utf-8", errors="replace").strip()
            await _send_response([f"[WARN] Journal unavailable: {err or 'no output'}"])
            return
        await _send_response(text.splitlines())
        return

    except Exception as exc:
        await _send_response([], error=str(exc))


async def _relay_shell_message(message: dict[str, Any]) -> None:
    if state._relay_ws_send_json is None:
        raise RuntimeError("Hub relay is not connected")
    await state._relay_ws_send_json(message)


def _resize_shell_fd(fd: int, cols: int, rows: int) -> None:
    safe_cols = max(int(cols or 80), 1)
    safe_rows = max(int(rows or 24), 1)
    winsize = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


async def _terminate_shell_process(proc: subprocess.Popen[Any]) -> int | None:
    if proc.poll() is not None:
        return proc.returncode
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGHUP)
    try:
        return await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            return await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                return await asyncio.to_thread(proc.wait)
    return proc.returncode


async def _cleanup_shell_session(session_id: str, *, exit_code: int | None = None, notify_exit: bool = True) -> None:
    session = _shell_sessions.pop(session_id, None)
    if not session:
        return

    current_task = asyncio.current_task()
    reader_task = session.get("reader_task")
    if reader_task is not None and reader_task is not current_task and not reader_task.done():
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task

    proc = session.get("process")
    if proc is not None:
        if exit_code is None:
            exit_code = await _terminate_shell_process(proc)
        else:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proc.wait)

    master_fd = session.get("pty_fd")
    if isinstance(master_fd, int):
        with contextlib.suppress(OSError):
            os.close(master_fd)

    if notify_exit and state._relay_ws_send_json is not None:
        with contextlib.suppress(Exception):
            await _relay_shell_message({
                "type": "shell_exit",
                "session_id": session_id,
                "exit_code": int(exit_code if exit_code is not None else -1),
            })


async def _close_all_shell_sessions(*, notify_exit: bool = False) -> None:
    for session_id in list(_shell_sessions):
        await _cleanup_shell_session(session_id, notify_exit=notify_exit)


async def _shell_reader_loop(session_id: str) -> None:
    session = _shell_sessions.get(session_id)
    if not session:
        return

    proc = session["process"]
    pty_fd = session["pty_fd"]
    notify_exit = True
    exit_code: int | None = None
    try:
        while True:
            await _wait_for_fd_readable(pty_fd)
            try:
                data = os.read(pty_fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                raise
            if not data:
                break
            try:
                await _relay_shell_message({
                    "type": "shell_data",
                    "session_id": session_id,
                    "data": data.decode("utf-8", errors="replace"),
                })
            except Exception as exc:
                notify_exit = False
                logger.warning("Shell relay send failed for session %s: %s", session_id, exc)
                break
        exit_code = proc.poll()
        if exit_code is None:
            exit_code = await asyncio.to_thread(proc.wait)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Shell reader failed for session %s: %s", session_id, exc)
        exit_code = proc.poll()
    finally:
        await _cleanup_shell_session(session_id, exit_code=exit_code, notify_exit=notify_exit)


async def _start_shell_session(command: dict[str, Any]) -> str:
    requested_session_id = str(command.get("session_id") or "").strip()
    session_id = requested_session_id or str(uuid.uuid4())
    await _cleanup_shell_session(session_id, notify_exit=False)

    shell_path = shutil.which("bash") or "/bin/bash"
    if not Path(shell_path).exists():
        raise RuntimeError("bash is not installed on the spoke host")

    pty_fd, child_fd = pty.openpty()
    try:
        os.set_blocking(pty_fd, False)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        proc = subprocess.Popen(
            [shell_path, "-i"],
            stdin=child_fd,
            stdout=child_fd,
            stderr=child_fd,
            cwd=str(Path.home()),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        with contextlib.suppress(OSError):
            os.close(pty_fd)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(child_fd)

    cols = int(command.get("cols") or 80)
    rows = int(command.get("rows") or 24)
    _resize_shell_fd(pty_fd, cols, rows)
    _shell_sessions[session_id] = {
        "pty_fd": pty_fd,
        "process": proc,
        "reader_task": None,
    }
    reader_task = asyncio.create_task(_shell_reader_loop(session_id))
    _shell_sessions[session_id]["reader_task"] = reader_task
    await _relay_shell_message({"type": "shell_started", "session_id": session_id})
    return session_id


async def _handle_shell_relay_message(message: dict[str, Any]) -> None:
    msg_type = str(message.get("type") or "").strip().lower()
    session_id = str(message.get("session_id") or "").strip()

    if msg_type == "shell_start":
        try:
            await _start_shell_session(message)
        except Exception as exc:
            logger.warning("Failed to start shell session %s: %s", session_id or "<new>", exc)
            await _relay_shell_message({
                "type": "shell_exit",
                "session_id": session_id or str(uuid.uuid4()),
                "exit_code": -1,
                "error": str(exc),
            })
        return

    session = _shell_sessions.get(session_id)
    if not session:
        if msg_type == "shell_exit":
            return
        raise RuntimeError(f"Unknown shell session: {session_id}")

    pty_fd = session["pty_fd"]
    if msg_type == "shell_input":
        data = message.get("data")
        if data is not None:
            os.write(pty_fd, str(data).encode())
        return
    if msg_type == "shell_resize":
        _resize_shell_fd(pty_fd, int(message.get("cols") or 80), int(message.get("rows") or 24))
        return
    if msg_type == "shell_exit":
        await _cleanup_shell_session(session_id)
        return
    raise RuntimeError(f"Unsupported shell relay message: {msg_type}")


async def _apply_relay_command_batch(remote_cmds: list[dict[str, Any]], ack_fn) -> None:
    commands_changed = False
    serialized_commands: list[dict[str, Any]] | None = None
    queued_targets: list[str] = []
    for rc in remote_cmds:
        cmd_id = rc.get("id", "")
        cmd_type = rc.get("type", "")
        payload_data = rc.get("payload", {}) if isinstance(rc.get("payload"), dict) else {}
        target = rc.get("target", "")
        action = rc.get("action", "") or payload_data.get("action", "")
        normalized_action = _normalize_command_action(action)
        args = rc.get("args", {}) or payload_data.get("args", {})
        _trace("hub_relay_recv", cmd_type=cmd_type, target=target, action=normalized_action,
               args={k: v for k, v in (args or {}).items() if k in {"vmid", "vm_type", "vm_name"}},
               cmd_id=cmd_id)

        if cmd_type in {"backup", "reseed"}:
            try:
                result = await _forward_hub_passthrough_to_proxmox(cmd_type, payload_data)
            except Exception as exc:
                logger.warning("Failed to forward %s command to proxmox agent: %s", cmd_type, exc)
                result = {
                    "success": False,
                    "task_type": cmd_type,
                    "detail": f"Failed to forward {cmd_type} command to proxmox agent: {exc}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if _hub_command_blocked_by_reseed(cmd_type, target, normalized_action):
            logger.warning("Rejecting hub %s command while reseed is in progress", cmd_type or normalized_action or target)
            if cmd_id:
                await ack_fn(cmd_id, "executed", _hub_reseed_block_result())
            continue

        if cmd_type in {"config_update", "config_clear"} and _hub_isolated():  # Skip new hub config pushes during isolation so the spoke keeps its current config until hub contact recovers.
            result = _hub_config_isolation_result(cmd_type)  # Reuse one explicit skip payload so the hub can see the safeguard blocked the command intentionally.
            if cmd_id:  # Only ack when the hub supplied a command ID so skipped pushes do not linger in the inbox.
                await ack_fn(cmd_id, "executed", result)  # Ack the skipped command so the hub inbox does not keep replaying a push we refuse while isolated.
            continue  # Stop before any config mutation because isolation means the current config must keep running unchanged.

        if cmd_type == "config_update":
            result = await _apply_hub_config(payload_data)
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "config_clear":
            settings["hub_managed"] = False
            _save_settings()
            await broadcast({"type": "settings_update", "settings": await api_settings_get()})
            await _broadcast_relay_state()  # Broadcast the cleared hub-managed state so any isolation banner disappears as soon as config control returns locally.
            logger.info("Hub config cleared — spoke is now self-managed")
            if cmd_id:
                await ack_fn(cmd_id, "executed", {
                    "success": True,
                    "task_type": "config_clear",
                    "detail": "Hub config cleared — spoke is now self-managed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            continue

        if cmd_type == "gkill_switch":
            new_val = (payload_data.get("value") or action or "").strip()
            if new_val in ("on", "off"):
                gkill_switch_state["value"] = new_val
                await broadcast({"type": "gkill_switch", "value": new_val})
                logger.info("gkill_switch set to %s by hub", new_val)
            if cmd_id:
                await ack_fn(cmd_id, "executed", {
                    "success": True,
                    "task_type": "gkill_switch",
                    "detail": f"gkill set to {gkill_switch_state['value']}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            continue

        if cmd_type == "repo_sync":
            already_running = state._hub_repo_sync_task is not None and not state._hub_repo_sync_task.done()
            if cmd_id:
                await ack_fn(cmd_id, "executed", {
                    "success": True,
                    "task_type": "repo_sync",
                    "detail": "Repo sync already in progress" if already_running else "Repo sync started",
                    "started": not already_running,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            if not already_running:
                state._hub_repo_sync_task = asyncio.create_task(_run_hub_repo_sync())
            continue

        if cmd_type == "self_update":
            if cmd_id:
                await ack_fn(cmd_id, "executed", {"task_type": "self_update", "detail": "Self-update triggered"})
            asyncio.create_task(_run_self_update())
            continue

        if cmd_type == "refresh_webui":
            asyncio.create_task(refresh_webui_frontend())
            if cmd_id:
                await ack_fn(cmd_id, "executed", {"task_type": "refresh_webui", "detail": "WebUI refresh triggered"})
            continue

        if cmd_type == "proxmox_agent_update":
            try:
                result = await _queue_proxmox_agent_update()
            except Exception as exc:
                result = {"success": False, "task_type": "proxmox_agent_update", "detail": str(exc)}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "proxmox_approve_agent":
            hostname = str(payload_data.get("hostname") or "").strip()
            try:
                pending_payload: list[dict[str, Any]] | None = None
                async with state_lock:
                    pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
                    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
                    if approved_hostname is None:
                        resolved_hostname = pending_hostname or _normalize_proxmox_hostname(hostname)
                        if resolved_hostname:
                            key = str(uuid.uuid4())
                            approved_proxmox_agents[resolved_hostname] = key
                            pending_proxmox_agents.pop(pending_hostname or resolved_hostname, None)
                            settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
                            _save_settings()
                            pending_payload = _pending_proxmox_payload()
                    else:
                        if pending_hostname:
                            pending_proxmox_agents.pop(pending_hostname, None)
                            pending_payload = _pending_proxmox_payload()
                if pending_payload is not None:
                    await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
                await _broadcast_proxmox_state()
                result = {"success": True, "task_type": "proxmox_approve_agent", "hostname": hostname}
            except Exception as exc:
                result = {"success": False, "task_type": "proxmox_approve_agent", "detail": str(exc)}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "proxmox_revoke_agent":
            hostname = str(payload_data.get("hostname") or "").strip()
            try:
                async with state_lock:
                    resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
                    approved_proxmox_agents.pop(resolved_hostname, None)
                    settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
                    _save_settings()
                await _broadcast_proxmox_state()
                result = {"success": True, "task_type": "proxmox_revoke_agent", "hostname": hostname}
            except Exception as exc:
                result = {"success": False, "task_type": "proxmox_revoke_agent", "detail": str(exc)}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "proxmox_agent_command":
            import uuid as _uuid_mod
            _action = (payload_data or {}).get("action", "")
            _args = (payload_data or {}).get("args", {})
            try:
                result = await _forward_hub_passthrough_to_proxmox("command", {
                    "id": str(_uuid_mod.uuid4()),
                    "action": _action,
                    "args": _args,
                })
            except RuntimeError:
                # Proxmox agent not connected via WebSocket (inbox polling mode) — enqueue
                # via the spoke's local command queue so the agent picks it up on next poll.
                # process_inbox handles multiple delete_vm commands in parallel, which avoids
                # the sequential-WS timeout for bulk teardown operations.
                try:
                    await _queue_proxmox_command(_action, _args if isinstance(_args, dict) else {})
                    result = {
                        "success": True,
                        "task_type": "proxmox_agent_command",
                        "detail": f"Queued {_action} via spoke inbox (WS unavailable)",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                except Exception as exc2:
                    result = {"success": False, "task_type": "proxmox_agent_command", "detail": str(exc2)}
            except Exception as exc:
                result = {"success": False, "task_type": "proxmox_agent_command", "detail": str(exc)}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "unlock_template":
            try:
                cmd = await _queue_unlock_template_command("unlock_template")
                result = _unlock_template_result(cmd)
            except Exception as exc:
                result = {"success": False, "task_type": "unlock_template", "detail": str(exc)}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "clear_reclone_state":
            _status = reclone_state.get("status", "idle")
            if _status != "running":
                reclone_state.update({
                    "status": "idle", "type": None, "total": 0,
                    "completed": 0, "failed": 0, "current_vm": None,
                    "log": [], "started_at": None,
                    "last_run": None, "auto_recovery_log": [],
                })
                await _broadcast_reclone_state()
            result = {"cleared": _status != "running", "previous_status": _status}
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if cmd_type == "proxmox_reclone_all":
            try:
                concurrency = int(payload_data.get("concurrency", 0) or 0)
                if concurrency > 0:
                    settings["reclone_concurrency"] = str(concurrency)
                if reclone_state.get("status") == "running":
                    result = {
                        "success": True,
                        "started": False,
                        "task_type": "proxmox_reclone_all",
                        "detail": "A reclone run is already in progress",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    eligible = _reclone_targets_for_run()
                    unassigned_dongles = _proxmox_unassigned_present_usb()
                    if not eligible and not unassigned_dongles:
                        result = {
                            "success": False,
                            "started": False,
                            "task_type": "proxmox_reclone_all",
                            "detail": (
                                "No reclone-capable guests or unassigned certified USB devices were found. "
                                "Guests without a USB mapping or LXC template source are skipped."
                            ),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    else:
                        asyncio.create_task(_run_rolling_reclone("fleet"))
                        result = {
                            "success": True,
                            "started": True,
                            "task_type": "proxmox_reclone_all",
                            "detail": "Fleet reclone started",
                            "vm_count": len(eligible),
                            "unassigned_dongles": len(unassigned_dongles),
                            "concurrency": max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1")),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
            except Exception as exc:
                logger.exception("Hub proxmox_reclone_all failed")
                result = {
                    "success": False,
                    "started": False,
                    "task_type": "proxmox_reclone_all",
                    "detail": f"Fleet reclone failed: {exc}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            if cmd_id:
                await ack_fn(cmd_id, "executed", result)
            continue

        if not target or not action:
            _trace("hub_relay_skip", reason="empty target or action", target=target, action=action)
            continue
        try:
            async with state_lock:
                if target == "all":
                    target_hostnames = list(clients.keys())
                    queued_targets.extend(target_hostnames)
                    for hostname in target_hostnames:
                        _enqueue_command_locked(hostname, action, args, command_type=cmd_type, relay=True)
                else:
                    queued_targets.append(target)
                    _enqueue_command_locked(target, action, args, command_type=cmd_type, relay=True)
                serialized_commands = _serialize_commands()
            agent_connected = state.proxmox_ws_connection is not None if target == "proxmox" else bool(client_ws_connections.get(target))
            _trace("hub_relay_enqueued", target=target, action=action, cmd_id=cmd_id,
                   agent_connected=agent_connected)
            commands_changed = True
            if cmd_id:
                await ack_fn(cmd_id, "queued", None)
        except Exception as exc:
            logger.warning("Hub relay: failed to enqueue %s/%s %s: %s", target, action, args, exc)
            _trace("hub_relay_enqueue_err", target=target, action=action, error=str(exc))
            if cmd_id:
                await ack_fn(cmd_id, "executed", {
                    "success": False,
                    "task_type": cmd_type or action,
                    "detail": str(exc),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    if commands_changed and serialized_commands is not None:
        await broadcast({"type": "commands_update", "commands": serialized_commands})
        await _push_pending_commands_for_targets(queued_targets)


async def _apply_hub_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply a config_update command payload pushed from hub.
    Returns an ack result dict."""
    raw_config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    config_payload = raw_config if isinstance(raw_config, dict) else {}
    config_version = int(payload.get("config_version") or payload.get("__config_version") or 0)
    if _hub_isolated():  # Refuse fresh hub config while isolated so the spoke keeps running its last known-good config during hub outages.
        return _hub_config_isolation_result("config_update")  # Return an explicit skip result so callers can ack the safeguard instead of pretending the config applied.
    changed: list[str] = []
    settings["hub_managed"] = True

    central_changed = False
    central_api_payload = config_payload.get("central_api") if "central_api" in config_payload else ...
    if central_api_payload is not ...:
        if central_api_payload is None:
            settings["central_api"] = _default_central_api_settings()
            settings["hub_aruba_polling_mode"] = "centralized"
        elif isinstance(central_api_payload, dict):
            merged_api = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
            mode = str(central_api_payload.get("mode", merged_api.get("mode", "classic"))).strip().lower()
            merged_api["mode"] = mode if mode in {"classic", "central"} else "classic"
            classic_update = central_api_payload.get("classic")
            if isinstance(classic_update, dict):
                for key in ("url", "username"):
                    if key in classic_update:
                        merged_api["classic"][key] = "" if classic_update.get(key) is None else str(classic_update.get(key, "")).strip()
                if "password" in classic_update:
                    merged_api["classic"]["password"] = "" if classic_update.get("password") is None else str(classic_update.get("password", ""))
            central_update = central_api_payload.get("central")
            if isinstance(central_update, dict):
                for key in ("url", "client_id", "customer_id"):
                    if key in central_update:
                        merged_api["central"][key] = "" if central_update.get(key) is None else str(central_update.get(key, "")).strip()
                if "client_secret" in central_update:
                    merged_api["central"]["client_secret"] = "" if central_update.get("client_secret") is None else str(central_update.get("client_secret", ""))
            settings["central_api"] = merged_api
            settings["hub_aruba_polling_mode"] = "distributed"
        changed.append("central_api")
        central_changed = True

    central_config_payload = config_payload.get("central_config") if "central_config" in config_payload else ...
    if central_config_payload is not ...:
        if central_config_payload is None:
            settings["central_config"] = {
                **_central_runtime_defaults(),
                "api_version": "classic",
                "cluster_url": "",
                "client_id": "",
                "client_secret": "",
                "customer_id": "",
                "access_token": "",
                "refresh_token": "",
            }
        elif isinstance(central_config_payload, dict):
            merged = dict(settings.get("central_config", {}))
            for key in ("cluster_url", "client_id", "customer_id", "api_version"):
                if key in central_config_payload:
                    value = central_config_payload.get(key)
                    merged[key] = "" if value is None else str(value).strip()
            for secret_key in ("client_secret", "access_token", "refresh_token"):
                if secret_key in central_config_payload:
                    value = central_config_payload.get(secret_key)
                    merged[secret_key] = "" if value is None else str(value).strip()
            settings["central_config"] = merged
        changed.append("central_config")
        central_changed = True

    if central_changed:
        merged_api = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
        merged_cfg = dict(settings.get("central_config", {}))
        if merged_cfg.get("api_version") == "new_central":
            merged_api["mode"] = "central"
            merged_api["central"].update({
                "url": str(merged_cfg.get("cluster_url", "")).strip(),
                "client_id": str(merged_cfg.get("client_id", "")).strip(),
                "client_secret": str(merged_cfg.get("client_secret", "")),
                "customer_id": str(merged_cfg.get("customer_id", "")).strip(),
            })
            central_token["access_token"] = None
            central_token["refresh_token"] = None
            central_token["expires_at"] = 0.0
        else:
            merged_api["mode"] = "classic"
            merged_api["classic"].update({
                "url": str(merged_cfg.get("cluster_url", "")).strip(),
            })
            central_token["access_token"] = merged_cfg.get("access_token") or None
            central_token["refresh_token"] = merged_cfg.get("refresh_token") or None
            central_token["expires_at"] = time.time() + 7200 if merged_cfg.get("access_token") else 0.0
        settings["central_api"] = merged_api
        settings["central_config"] = merged_cfg

    notifications = copy.deepcopy(settings.get("notifications", {}))
    notification_changed = False
    if isinstance(config_payload.get("notifications"), dict):
        for key, value in config_payload["notifications"].items():
            if key not in HUB_NOTIFICATION_KEY_MAP:
                continue
            notifications[key] = [] if key == "smtp_to" and value is None else ("" if value is None else value)
            changed.append(key)
            notification_changed = True
    for key in HUB_NOTIFICATION_KEY_MAP:
        if key in config_payload:
            value = config_payload.get(key)
            notifications[HUB_NOTIFICATION_KEY_MAP[key]] = [] if key == "smtp_to" and value is None else ("" if value is None else value)
            changed.append(key)
            notification_changed = True
    if notification_changed:
        settings["notifications"] = notifications

    # ── github_config (Wave 2 Setup → GitHub) ────────────────────────────────
    github_payload = config_payload.get("github_config") if "github_config" in config_payload else ...
    if github_payload is not ...:
        if github_payload is None:
            settings["github_config"] = {"repo_url": "", "repo_branch": "", "github_token": ""}
        elif isinstance(github_payload, dict):
            merged = dict(settings.get("github_config", {}))
            for gk in ("repo_url", "repo_branch"):
                if gk in github_payload:
                    v = github_payload.get(gk)
                    merged[gk] = "" if v is None else str(v).strip()
            if "github_token" in github_payload:
                v = github_payload.get("github_token")
                merged["github_token"] = "" if v is None else str(v)
            settings["github_config"] = merged
        changed.append("github_config")

    # ── security_config (Wave 2 Setup → Security: spoke-local dashboard auth) ─
    security_payload = config_payload.get("security_config") if "security_config" in config_payload else ...
    if security_payload is not ...:
        if security_payload is None:
            settings["security_config"] = {"session_timeout_minutes": "", "auth_provider": ""}
        elif isinstance(security_payload, dict):
            merged = dict(settings.get("security_config", {}))
            for sk in ("session_timeout_minutes", "auth_provider"):
                if sk in security_payload:
                    v = security_payload.get(sk)
                    merged[sk] = "" if v is None else str(v).strip()
            settings["security_config"] = merged
        changed.append("security_config")

    # ── central_sites_config (Wave 2 Setup → Central API / Wave 3 Central) ──
    csc_payload = config_payload.get("central_sites_config") if "central_sites_config" in config_payload else ...
    if csc_payload is not ...:
        if csc_payload is None:
            settings["central_sites_config"] = {}
        elif isinstance(csc_payload, dict):
            settings["central_sites_config"] = dict(csc_payload)
            # Hub-managed: propagate the owned sub-fields into the runtime
            # settings the spoke's central monitoring actually reads, so the
            # hub-owned sites/checks editor takes effect (same guard as the
            # usb_vidpids allowlist). Sentinel per-field: only overwrite when
            # the hub included that key (absence leaves the spoke value intact).
            if settings.get("hub_managed"):
                if "site_mappings" in csc_payload:
                    sm = csc_payload.get("site_mappings")
                    settings["site_mappings"] = dict(sm) if isinstance(sm, dict) else {}
                    changed.append("site_mappings")
                if "monitored_checks" in csc_payload:
                    mc = csc_payload.get("monitored_checks")
                    settings["monitored_checks"] = list(mc) if isinstance(mc, list) else []
                    changed.append("monitored_checks")
                if "hardware_checks" in csc_payload:
                    hc = csc_payload.get("hardware_checks")
                    settings["hardware_checks"] = list(hc) if isinstance(hc, list) else []
                    changed.append("hardware_checks")
        changed.append("central_sites_config")

    for key, value in config_payload.items():
        if key in HUB_RELAY_KEYS or key in {"command", "config", "config_version", "__config_version", "central_api", "central_config", "central_sites_config", "notifications", "github_config", "security_config", "sim_conf_override", "user_conf_override", *HUB_NOTIFICATION_KEY_MAP.keys()}:
            continue
        # USB allowlist control: when this spoke is under hub management, the hub owns
        # usb_vidpids and usb_ignored_vidpids — apply whatever the hub sends (even empty,
        # so the hub can clear the list).  When the spoke is running locally (not hub_managed),
        # these two keys are locally owned and we skip any hub-pushed value so that a stale
        # registration snapshot stored in spoke.config doesn't overwrite the locally configured VIDs.
        if key in {"usb_vidpids", "usb_ignored_vidpids"}:
            if not settings.get("hub_managed"):
                # Spoke is not hub-managed — leave the local allowlist untouched.
                continue
            # Hub-managed: fall through and let the hub value replace the local one.
        if key in {"usb_auto_provision", "reclone_schedule_enabled", "spoke_tls"}:
            settings[key] = _normalize_relay_enabled(value)
        elif value is None:
            settings[key] = ""
        else:
            settings[key] = value
        changed.append(key)

    # ── Remove hub-owned keys that were absent from this config push ──────────
    # The hub always sends its full config snapshot; if an owned key is absent,
    # the operator has cleared it and the spoke should reset it to a blank value.
    # Only applies when the spoke is hub-managed to avoid clobbering local config.
    if settings.get("hub_managed"):
        for key in HUB_CONFIG_OWNED_KEYS:
            if key in config_payload:
                continue  # was present — already handled above
            # Skip special keys handled by other paths
            if key in {"usb_vidpids", "usb_ignored_vidpids"}:
                continue  # handled above with explicit hub_managed guard
            if key in settings and settings[key] not in (None, "", [], {}):
                settings[key] = ""
                changed.append(f"{key}:cleared-by-absence")
                logger.debug("Hub config: cleared spoke setting '%s' (absent from hub payload)", key)
    # The hub pushes optional INI text for simulation.conf and user-overrides.conf.
    # None = no override (spoke uses its GitHub-pulled files as-is).
    # Non-None string = write to hub-*-overrides.conf so the spoke merges it on top.
    for override_key, override_filename in (
        ("sim_conf_override",  "hub-sim-overrides.conf"),
        ("user_conf_override", "hub-user-overrides.conf"),
    ):
        if override_key not in config_payload:
            continue
        override_text = config_payload[override_key]
        override_path = REPO_DIR / "configs" / override_filename
        try:
            if override_text is None:
                # Hub cleared the override — remove local file so GitHub file applies.
                if override_path.exists():
                    override_path.unlink()
                    changed.append(f"{override_key}:cleared")
            else:
                override_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = override_path.with_suffix(".tmp")
                tmp.write_text(str(override_text), encoding="utf-8")
                tmp.replace(override_path)
                changed.append(f"{override_key}:updated")
        except Exception as exc:
            logger.warning("Could not write %s: %s", override_path, exc)

    # Invalidate simulation.conf INI cache so the merged result is recomputed.
    _sim_conf_cache["sim_mtime"] = -1.0

    _save_settings()
    await broadcast({"type": "settings_update", "settings": await api_settings_get()})
    logger.info("Applied hub config_update v%s: %s", config_version or "?", changed)
    return {
        "success": True,
        "task_type": "config_update",
        "detail": f"Applied config version {config_version}: {', '.join(changed) if changed else 'no changes'}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def relay_sync_once() -> None:
    """One relay cycle: register if needed, check approval if pending,
    then post telemetry, fetch inbox, process commands, and ack each."""
    if settings.get("relay_enabled") != "on" or not settings.get("relay_server_url"):
        relay_state["enabled"] = False
        _save_relay_state()
        return

    if not _HTTPX_AVAILABLE or httpx is None:
        relay_state.update({"enabled": True, "connected": False, "error": "httpx not installed"})
        await _broadcast_relay_state()
        return

    relay_state["enabled"] = True
    server_url = settings["relay_server_url"].rstrip("/")
    spoke_id = _ensure_relay_spoke_id()
    api_key = settings.get("relay_api_key", "")
    tenant_id = settings.get("relay_tenant_id", "")

    # ── Phase 1: Register if no spoke_id yet ──────────────────────────────────
    if not spoke_id:
        await _hub_self_register(server_url)
        await _broadcast_relay_state()
        return

    if state.relay_registration_refresh_needed:
        await _hub_check_approval(server_url, spoke_id)
        spoke_id = settings.get("relay_spoke_id", spoke_id)
        api_key = settings.get("relay_api_key", "")
        tenant_id = settings.get("relay_tenant_id", "")

    # ── Phase 2: Pending approval — poll hub for approval ──────────────────────
    if not api_key or not tenant_id:
        relay_state["registration_status"] = relay_state.get("registration_status", "pending")
        await _hub_check_approval(server_url, spoke_id)
        spoke_id = settings.get("relay_spoke_id", spoke_id)
        api_key = settings.get("relay_api_key", "")
        tenant_id = settings.get("relay_tenant_id", "")
        if not api_key or not tenant_id:
            await _broadcast_relay_state()
            return

    # ── Phase 3: Approved — full relay cycle ───────────────────────────────────
    relay_state["registration_status"] = "approved"
    headers = {"X-API-Key": api_key}
    hub_base = _relay_hub_base_url(server_url, tenant_id)
    base = f"{hub_base}/api/{tenant_id}/spokes/{spoke_id}"

    try:
        telemetry = await _build_relay_telemetry_payload(spoke_id)

        async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc:
            telemetry_resp = await hc.post(f"{base}/telemetry", json=telemetry, headers=headers)
            telemetry_resp.raise_for_status()
            relay_state.update({"connected": True, "last_sync": time.time(), "error": None})  # Count the successful telemetry POST immediately so isolation clears before we evaluate any newly fetched hub commands.
            resp = await hc.get(f"{base}/inbox", headers=headers)
            # In centralized mode, get Central data from hub instead of polling directly
            if settings.get("hub_aruba_polling_mode") == "centralized":
                try:
                    feed_resp = await hc.get(f"{base}/central-feed", headers=headers, timeout=15)
                    if feed_resp.status_code == 200:
                        await _apply_central_feed(_parse_upstream_json(feed_resp))
                except UpstreamJSONError:
                    pass
                except Exception as _feed_exc:
                    logger.debug("Central feed fetch failed: %s", _feed_exc)
            # Fetch hub-managed monitored items filtered to this spoke's assigned sites
            try:
                mon_resp = await hc.get(f"{base}/monitored-items", headers=headers, timeout=10)
                if mon_resp.status_code == 200:
                    state._hub_monitored_items = mon_resp.json()
            except Exception as _mon_exc:
                logger.debug("Monitored items fetch failed: %s", _mon_exc)
            resp.raise_for_status()
            remote_cmds = _parse_upstream_json(resp)

        if not isinstance(remote_cmds, list):
            remote_cmds = []

        commands_changed = False
        serialized_commands: list[dict[str, Any]] | None = None
        queued_targets: list[str] = []
        for rc in remote_cmds:
            cmd_id = rc.get("id", "")
            cmd_type = rc.get("type", "")
            payload_data = rc.get("payload", {}) if isinstance(rc.get("payload"), dict) else {}
            target = rc.get("target", "")
            action = rc.get("action", "") or payload_data.get("action", "")
            normalized_action = _normalize_command_action(action)
            args = rc.get("args", {}) or payload_data.get("args", {})

            if cmd_type in {"backup", "reseed"}:
                try:
                    result = await _forward_hub_passthrough_to_proxmox(cmd_type, payload_data)
                except Exception as exc:
                    logger.warning("Failed to forward %s command to proxmox agent: %s", cmd_type, exc)
                    result = {
                        "success": False,
                        "task_type": cmd_type,
                        "detail": f"Failed to forward {cmd_type} command to proxmox agent: {exc}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if _hub_command_blocked_by_reseed(cmd_type, target, normalized_action):
                logger.warning("Rejecting hub %s command while reseed is in progress", cmd_type or normalized_action or target)
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": _hub_reseed_block_result(),
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type in {"config_update", "config_clear"} and _hub_isolated():  # Skip new hub config pushes during isolation so the spoke freezes hub-driven changes until contact is healthy again.
                result = _hub_config_isolation_result(cmd_type)  # Build one consistent safeguard ack so the hub can see the command was intentionally paused.
                if cmd_id:  # Only send an ack when the hub gave us an ID so skipped commands are retired cleanly.
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:  # Open a short-lived ack client so the skipped command is recorded immediately by the hub.
                        ack_resp = await hc_ack.post(f"{base}/ack", json={  # Post the skip result so the hub knows isolation paused this config push on purpose.
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue  # Stop before any config mutation because isolated spokes must keep their current config unchanged.

            # ── config_update: apply hub-pushed config and ack ─────────────
            if cmd_type == "config_update":
                result = await _apply_hub_config(payload_data)
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "config_clear":
                settings["hub_managed"] = False
                _save_settings()
                await broadcast({"type": "settings_update", "settings": await api_settings_get()})
                await _broadcast_relay_state()  # Broadcast the cleared hub-managed state so isolation status resets immediately after the hub releases control.
                logger.info("Hub config cleared — spoke is now self-managed")
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": {
                                "success": True,
                                "task_type": "config_clear",
                                "detail": "Hub config cleared — spoke is now self-managed",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            # ── gkill_switch: update local gkill state ─────────────────────
            if cmd_type == "gkill_switch":
                new_val = (payload_data.get("value") or action or "").strip()
                if new_val in ("on", "off"):
                    gkill_switch_state["value"] = new_val
                    await broadcast({"type": "gkill_switch", "value": new_val})
                    logger.info("gkill_switch set to %s by hub", new_val)
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": {
                                "success": True,
                                "task_type": "gkill_switch",
                                "detail": f"gkill set to {gkill_switch_state['value']}",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "repo_sync":
                try:
                    result = await _run_hub_repo_sync()
                except Exception as exc:
                    logger.exception("Hub repo_sync failed")
                    result = {
                        "success": False,
                        "task_type": "repo_sync",
                        "detail": f"Repo Sync failed: {exc}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "self_update":
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": {"task_type": "self_update", "detail": "Self-update triggered"},
                        }, headers=headers)
                        ack_resp.raise_for_status()
                asyncio.create_task(_run_self_update())
                continue

            if cmd_type == "refresh_webui":
                asyncio.create_task(refresh_webui_frontend())
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": {"task_type": "refresh_webui", "detail": "WebUI refresh triggered"},
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "proxmox_agent_update":
                try:
                    result = await _queue_proxmox_agent_update()
                except Exception as exc:
                    result = {"success": False, "task_type": "proxmox_agent_update", "detail": str(exc)}
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "proxmox_agent_command":
                import uuid as _uuid_mod
                _action = (payload_data or {}).get("action", "")
                _args = (payload_data or {}).get("args", {})
                try:
                    result = await _forward_hub_passthrough_to_proxmox("command", {
                        "id": str(_uuid_mod.uuid4()),
                        "action": _action,
                        "args": _args,
                    })
                except RuntimeError:
                    # Proxmox agent not connected via WebSocket — fall back to spoke inbox queue.
                    try:
                        await _queue_proxmox_command(_action, _args if isinstance(_args, dict) else {})
                        result = {
                            "success": True,
                            "task_type": "proxmox_agent_command",
                            "detail": f"Queued {_action} via spoke inbox (WS unavailable)",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    except Exception as exc2:
                        result = {"success": False, "task_type": "proxmox_agent_command", "detail": str(exc2)}
                except Exception as exc:
                    result = {"success": False, "task_type": "proxmox_agent_command", "detail": str(exc)}
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "unlock_template":
                try:
                    cmd = await _queue_unlock_template_command("unlock_template")
                    result = _unlock_template_result(cmd)
                except Exception as exc:
                    result = {"success": False, "task_type": "unlock_template", "detail": str(exc)}
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "clear_reclone_state":
                _status = reclone_state.get("status", "idle")
                if _status != "running":
                    reclone_state.update({
                        "status": "idle", "type": None, "total": 0,
                        "completed": 0, "failed": 0, "current_vm": None,
                        "log": [], "started_at": None,
                        "last_run": None, "auto_recovery_log": [],
                    })
                    await _broadcast_reclone_state()
                result = {"cleared": _status != "running", "previous_status": _status}
                if cmd_id:
                    async with httpx.AsyncClient(verify=False, timeout=10) as cli:
                        ack_resp = await cli.post(f"{hub_base}/api/{hub_tenant_id}/spokes/{hub_spoke_id}/ack",
                            json={"command_id": cmd_id, "status": "executed", "result": result},
                            headers=headers)
                        ack_resp.raise_for_status()
                continue

            if cmd_type == "proxmox_reclone_all":
                try:
                    concurrency = int(payload_data.get("concurrency", 0) or 0)
                    if concurrency > 0:
                        settings["reclone_concurrency"] = str(concurrency)
                    if reclone_state.get("status") == "running":
                        result = {
                            "success": True,
                            "started": False,
                            "task_type": "proxmox_reclone_all",
                            "detail": "A reclone run is already in progress",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    else:
                        eligible = _reclone_targets_for_run()
                        unassigned_dongles = _proxmox_unassigned_present_usb()
                        if not eligible and not unassigned_dongles:
                            result = {
                                "success": False,
                                "started": False,
                                "task_type": "proxmox_reclone_all",
                                "detail": (
                                    "No reclone-capable guests or unassigned certified USB devices were found. "
                                    "Guests without a USB mapping or LXC template source are skipped."
                                ),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                        else:
                            asyncio.create_task(_run_rolling_reclone("fleet"))
                            result = {
                                "success": True,
                                "started": True,
                                "task_type": "proxmox_reclone_all",
                                "detail": "Fleet reclone started",
                                "vm_count": len(eligible),
                                "unassigned_dongles": len(unassigned_dongles),
                                "concurrency": max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1")),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                except Exception as exc:
                    logger.exception("Hub proxmox_reclone_all failed")
                    result = {
                        "success": False,
                        "started": False,
                        "task_type": "proxmox_reclone_all",
                        "detail": f"Fleet reclone failed: {exc}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": result,
                        }, headers=headers)
                        ack_resp.raise_for_status()
                continue

            # ── regular client/proxmox commands ────────────────────────────
            if not target or not action:
                continue
            try:
                async with state_lock:
                    if target == "all":
                        target_hostnames = list(clients.keys())
                        queued_targets.extend(target_hostnames)
                        for hostname in target_hostnames:
                            _enqueue_command_locked(hostname, action, args, command_type=cmd_type, relay=True)
                    else:
                        queued_targets.append(target)
                        _enqueue_command_locked(target, action, args, command_type=cmd_type, relay=True)
                    serialized_commands = _serialize_commands()
                commands_changed = True

                # Ack each queued command
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "queued",
                        }, headers=headers)
                        ack_resp.raise_for_status()
            except Exception as exc:
                logger.warning("Hub relay: failed to enqueue %s/%s %s: %s", target, action, args, exc)
                if cmd_id:
                    async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc_ack:
                        ack_resp = await hc_ack.post(f"{base}/ack", json={
                            "command_id": cmd_id,
                            "status": "executed",
                            "result": {
                                "success": False,
                                "task_type": cmd_type or action,
                                "detail": str(exc),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        }, headers=headers)
                        ack_resp.raise_for_status()

        if commands_changed and serialized_commands is not None:
            await broadcast({"type": "commands_update", "commands": serialized_commands})
            await _push_pending_commands_for_targets(queued_targets)

        relay_state.update({"connected": True, "error": None})  # Keep the relay marked healthy after processing commands because last_sync was already set at the successful telemetry POST.
        _debug_event("relay_sync_ok", f"proxmox_connected={proxmox_state.get('connected')} clients={len(telemetry.get('clients', []))}")
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code in (401, 403, 404):
            state.relay_registration_refresh_needed = True
            settings["relay_api_key"] = ""
            settings["relay_tenant_id"] = ""
            relay_state.update({
                "connected": False,
                "registration_status": "pending",
                "error": f"Hub returned {status_code}; refreshing registration",
            })
            _relay_diag_append(
                "relay_reauth_required",
                status_code=status_code,
                method=exc.request.method if exc.request else "",
                url=str(exc.request.url) if exc.request else "",
            )
            _revert_hub_managed_if_auth_failure(status_code, f"relay sync HTTP {status_code}")
            _save_settings()
            await _hub_check_approval(server_url, spoke_id)
        else:
            relay_state.update({"connected": False, "error": str(exc)})
        logger.warning("Relay sync failed: %s", exc)
        _debug_event("relay_sync_fail", str(exc)[:200])
    except Exception as exc:
        relay_state.update({"connected": False, "error": str(exc)})
        logger.warning("Relay sync failed: %s", exc)
        _debug_event("relay_sync_fail", str(exc)[:200])

    await _broadcast_relay_state()


async def relay_ws_loop() -> None:
    if not _WEBSOCKETS_AVAILABLE or websockets is None:
        raise RuntimeError("websockets not installed")

    backoff = 1
    while True:
        interval = int(settings.get("relay_poll_interval", RELAY_INTERVAL_DEFAULT))
        try:
            if settings.get("relay_enabled") != "on" or not settings.get("relay_server_url"):
                relay_state["enabled"] = False
                await _broadcast_relay_state()
                return

            relay_state["enabled"] = True
            server_url = settings["relay_server_url"].rstrip("/")
            spoke_id = _ensure_relay_spoke_id()
            api_key = settings.get("relay_api_key", "")
            tenant_id = settings.get("relay_tenant_id", "")

            if not spoke_id:
                await _hub_self_register(server_url)
                await _broadcast_relay_state()
                await asyncio.sleep(interval)
                continue

            if state.relay_registration_refresh_needed:
                await _hub_check_approval(server_url, spoke_id)
                spoke_id = settings.get("relay_spoke_id", spoke_id)
                api_key = settings.get("relay_api_key", "")
                tenant_id = settings.get("relay_tenant_id", "")

            if not api_key or not tenant_id:
                relay_state["registration_status"] = relay_state.get("registration_status", "pending")
                await _hub_check_approval(server_url, spoke_id)
                spoke_id = settings.get("relay_spoke_id", spoke_id)
                api_key = settings.get("relay_api_key", "")
                tenant_id = settings.get("relay_tenant_id", "")
                if not api_key or not tenant_id:
                    await _broadcast_relay_state()
                    await asyncio.sleep(interval)
                    continue

            relay_state["registration_status"] = "approved"
            ws_url = _relay_ws_url(server_url, tenant_id, spoke_id, api_key)
            send_lock = asyncio.Lock()

            # Build SSL context: disable cert verification when hub_tls_verify is off
            ws_ssl: bool | ssl.SSLContext = True
            if not _hub_tls_verify():
                ws_ssl_ctx = ssl.create_default_context()
                ws_ssl_ctx.check_hostname = False
                ws_ssl_ctx.verify_mode = ssl.CERT_NONE
                ws_ssl = ws_ssl_ctx

            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10,
                                          ssl=ws_ssl if ws_url.startswith("wss://") else None) as websocket:
                backoff = 1
                relay_state["ws_reconnect_count"] = relay_state.get("ws_reconnect_count", 0) + 1
                relay_state["ws_last_reconnect_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

                async def send_json(payload: dict[str, Any]) -> None:
                    async with send_lock:
                        await websocket.send(json.dumps(payload, default=str))

                async def ack_fn(command_id: str, status: str, result: dict[str, Any] | None) -> None:
                    payload: dict[str, Any] = {"type": "ack", "payload": {"command_id": command_id, "status": status}}
                    if result is not None:
                        payload["payload"]["result"] = result
                    await send_json(payload)

                async def telemetry_loop() -> None:
                    while True:
                        try:
                            _t0 = time.monotonic()
                            telemetry = await _build_relay_telemetry_payload(spoke_id)
                            build_ms = round((time.monotonic() - _t0) * 1000)
                            relay_state["telemetry_build_ms"] = build_ms
                            relay_state["_last_telemetry_sent_at"] = time.time()
                            await send_json({"type": "telemetry", "payload": telemetry})
                            # Do NOT set connected=True here — wait for telemetry_ack from the hub.
                            # Setting connected on send would mask auth failures where the hub
                            # accepts the TCP/WS handshake but rejects the spoke with a close frame.
                            _debug_event("relay_sync_ok", f"proxmox_connected={proxmox_state.get('connected')} clients={len(telemetry.get('clients', []))}")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # Log and continue — a transient error (e.g. WS send on a half-open
                            # connection, serialisation blip) must not silently kill the loop.
                            # If the WS is truly dead, the outer receive loop will detect it and
                            # trigger reconnection; until then we keep trying so telemetry resumes
                            # as soon as the connection recovers.
                            logger.warning("relay telemetry_loop error (will retry in %ss): %s", interval, exc)
                        await asyncio.sleep(interval)

                state._relay_ws_send_json = send_json
                state._relay_ws_spoke_id = spoke_id
                sender = asyncio.create_task(telemetry_loop())
                try:
                    # Clear all active demo scenarios on hub reconnect so that a hub
                    # reboot automatically reverts any in-flight demo overrides.
                    if _demo_active:
                        logger.info("Hub reconnected — clearing %d active demo override(s)", len(_demo_active))
                        _clear_all_demo_scenarios_sync()
                        await broadcast_full_state()
                    await send_json({"type": "sync"})
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        msg_type = str(message.get("type") or "").strip().lower()
                        if msg_type == "commands":
                            commands = message.get("commands") if isinstance(message.get("commands"), list) else []
                            await _apply_relay_command_batch(commands, ack_fn)
                        elif msg_type == "command":
                            await _apply_relay_command_batch([message], ack_fn)
                        elif msg_type == "central_feed":
                            payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                            await _apply_central_feed(payload)
                        elif msg_type == "telemetry_ack":
                            sent_at = relay_state.pop("_last_telemetry_sent_at", None)
                            rtt_ms = round((time.time() - sent_at) * 1000) if sent_at else None
                            hub_processing_ms = message.get("processing_ms")
                            hub_loop_lag_ms = message.get("loop_lag_ms")
                            relay_state.update({"connected": True, "last_sync": time.time(), "error": None})
                            if rtt_ms is not None:
                                relay_state["hub_rtt_ms"] = rtt_ms
                            if hub_processing_ms is not None:
                                relay_state["hub_processing_ms"] = hub_processing_ms
                            if hub_loop_lag_ms is not None:
                                relay_state["hub_loop_lag_ms"] = hub_loop_lag_ms
                            await _broadcast_relay_state()
                        elif msg_type == "pong":
                            relay_state.update({"connected": True, "error": None})
                        elif msg_type.startswith("shell_"):
                            await _handle_shell_relay_message(message)
                        elif msg_type == "vnc_proxy_request":
                            asyncio.create_task(_handle_vnc_proxy_request(message))
                        elif msg_type == "provision_proxmox_token":
                            asyncio.create_task(_handle_provision_proxmox_token(message))
                        elif msg_type == "vnc_frame_to_proxmox":
                            req_id = str(message.get("request_id") or "").strip()
                            q = _vnc_sessions.get(req_id)
                            if q is not None:
                                q.put_nowait(message)
                        elif msg_type == "vnc_disconnect":
                            req_id = str(message.get("request_id") or "").strip()
                            q = _vnc_sessions.get(req_id)
                            if q is not None:
                                q.put_nowait(None)
                        elif msg_type == "log_fetch":
                            asyncio.create_task(_handle_log_fetch(message))
                        elif msg_type == "command_trace_request":
                            asyncio.create_task(_handle_command_trace_request(message))
                        elif msg_type == "demo_scenario":
                            _hostname = str(message.get("hostname") or "").strip()
                            _scenario = str(message.get("scenario") or "").strip()
                            if _hostname and _scenario:
                                asyncio.create_task(_apply_demo_scenario(_hostname, _scenario, triggered_by="hub"))
                        elif msg_type == "demo_clear":
                            _hostname = str(message.get("hostname") or "").strip()
                            if _hostname:
                                asyncio.create_task(_clear_demo_scenario(_hostname))
                            else:
                                _clear_all_demo_scenarios_sync()
                                asyncio.create_task(broadcast_full_state())
                        elif msg_type == "purge_clients":
                            async def _do_purge_clients_relay() -> None:
                                async with state_lock:
                                    clients.clear()
                                await asyncio.to_thread(_save_client_history)
                                await broadcast({"type": "clients_purged"})
                                logger.info("Client history purged by hub relay request")
                            asyncio.create_task(_do_purge_clients_relay())
                finally:
                    await _close_all_shell_sessions(notify_exit=False)
                    if state._relay_ws_send_json is send_json:
                        state._relay_ws_send_json = None
                        state._relay_ws_spoke_id = None
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
        except asyncio.CancelledError:
            raise
        except WebSocketInvalidStatus as exc:
            status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            relay_state.update({"connected": False, "error": f"websocket handshake failed: {exc}", "ws_last_error": f"HTTP {status_code}: {exc}"})
            await _broadcast_relay_state()
            if status_code in (401, 403):
                state.relay_registration_refresh_needed = True
                settings["relay_api_key"] = ""
                settings["relay_tenant_id"] = ""
                _revert_hub_managed_if_auth_failure(status_code, f"websocket handshake HTTP {status_code}")
                _save_settings()
            if status_code == 404:
                _revert_hub_managed_if_auth_failure(status_code, "websocket handshake HTTP 404 (tenant not found)")
                await relay_sync_once()
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
        except Exception as exc:
            relay_state.update({"connected": False, "error": str(exc), "ws_last_error": str(exc)})
            _debug_event("relay_sync_fail", str(exc)[:200])
            await _broadcast_relay_state()
            # Detect WebSocket close codes 4401/4403 sent by the hub when auth fails.
            # The hub accepts the HTTP upgrade then closes with an application-level code,
            # so these never reach WebSocketInvalidStatus — they arrive here as generic exceptions.
            exc_str = str(exc).lower()
            ws_close_code = getattr(exc, "code", None) or getattr(exc, "rcvd", None)
            ws_close_int = int(ws_close_code.code) if hasattr(ws_close_code, "code") else (int(ws_close_code) if isinstance(ws_close_code, int) else None)
            if ws_close_int in (4401, 4403) or "4401" in exc_str or "4403" in exc_str:
                state.relay_registration_refresh_needed = True
                settings["relay_api_key"] = ""
                settings["relay_tenant_id"] = ""
                _revert_hub_managed_if_auth_failure(401, f"websocket closed with code {ws_close_int or 'auth'}: hub rejected credentials")
                _save_settings()
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
            elif any(token in exc_str for token in ["connection refused", "404", "not found"]):
                await relay_sync_once()
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)


async def relay_loop() -> None:
    while True:
        interval = int(settings.get("relay_poll_interval", RELAY_INTERVAL_DEFAULT))
        jitter = random.randint(0, 15)
        try:
            if _WEBSOCKETS_AVAILABLE and websockets is not None:
                await relay_ws_loop()
            else:
                await relay_sync_once()
            _update_service_health("relay", ok=True)
        except asyncio.CancelledError:
            raise
        except UpstreamJSONError as exc:
            _update_service_health("relay", ok=False, error=str(exc))
            logger.warning("Relay loop upstream JSON error: %s", exc)
            await asyncio.sleep(interval + jitter)
            continue
        except Exception as exc:
            _update_service_health("relay", ok=False, error=str(exc))
            logger.exception("Relay loop error: %s", exc)
            await asyncio.sleep(interval + jitter)
            continue
        await asyncio.sleep(interval + jitter)




def ensure_repo_ready() -> None:
    if not repo_state["synced"]:
        detail = "Repository has not been synced yet."
        if repo_state["error"]:
            detail = f"Repository not ready: {repo_state['error']}"
        raise HTTPException(status_code=503, detail=detail)


def repo_path(*parts: str) -> Path:
    ensure_repo_ready()
    path = REPO_DIR.joinpath(*parts)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Repo file not found: {'/'.join(parts)}")
    return path


def validate_platform(platform: str) -> str:
    if platform not in ALLOWED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    return platform


def override_section_name(key: str, simulation_id: str | None) -> str | None:
    if key in SIMULATION_SECTION_KEYS:
        return simulation_id
    if key in GLOBAL_SECTION_KEYS:
        return "simulation"
    if key in SERVER_SECTION_KEYS:
        return "server"
    if key in ADDRESS_SECTION_KEYS:
        return "address"
    return simulation_id


def apply_overrides(config_text: str, client: dict[str, Any]) -> str:
    overrides = {key: str(value) for key, value in client.get("overrides", {}).items()}
    if not overrides:
        return config_text

    pattern = re.compile(r"^\s*\[(?P<section>[^\]]+)\]\s*$")
    key_pattern = re.compile(r"^\s*(?P<key>[^=\s#;]+)\s*=")
    section_keys: dict[str, set[str]] = {}
    current_section: str | None = None

    for line in config_text.splitlines():
        match = pattern.match(line)
        if match:
            current_section = match.group("section")
            section_keys.setdefault(current_section, set())
            continue
        if current_section:
            key_match = key_pattern.match(line)
            if key_match:
                section_keys.setdefault(current_section, set()).add(key_match.group("key"))

    hostname = str(client.get("hostname", ""))
    user_candidates = [hostname, hostname.split("-")[0] if hostname else ""]
    user_section = next((candidate for candidate in user_candidates if candidate and candidate in section_keys), None)

    simulation_id = client.get("simulation_id")
    replacements: dict[str, dict[str, str]] = {}
    for key, value in overrides.items():
        section = None
        if user_section and key in section_keys.get(user_section, set()):
            section = user_section
        if not section:
            section = override_section_name(key, simulation_id)
        if not section:
            continue
        replacements.setdefault(section, {})[key] = value

    current_section = None
    updated_lines: list[str] = []
    for line in config_text.splitlines():
        match = pattern.match(line)
        if match:
            current_section = match.group("section")
            updated_lines.append(line)
            continue

        if current_section and current_section in replacements:
            for key, value in replacements[current_section].items():
                if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                    line = re.sub(rf"^(\s*{re.escape(key)}\s*=).*$", rf"\1{value}", line)
                    break

        updated_lines.append(line)

    if config_text.endswith("\n"):
        return "\n".join(updated_lines) + "\n"
    return "\n".join(updated_lines)


def _git(*args: str, cwd: Path | None = None, timeout: int = 120, env: dict[str, str] | None = None) -> str:
    """Run a git command, raise RuntimeError on failure.

    timeout (default 120 s) prevents git clone/fetch from hanging indefinitely
    when the network is slow or GitHub is temporarily unresponsive.
    GIT_TERMINAL_PROMPT=0 ensures git never blocks waiting for credentials —
    public repos work without a token; private repos fail fast with a clear error.
    """
    git_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        **(env or {}),
    }
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or REPO_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def sync_repo_once() -> None:
    branch = str(settings.get("repo_branch") or REPO_BRANCH).strip()
    REPO_DIR.parent.mkdir(parents=True, exist_ok=True)

    if not REPO_DIR.exists() or not any(REPO_DIR.iterdir()):
        logger.info("Cloning %s (%s) into %s", REPO_URL, branch, REPO_DIR)
        _git("clone", "--branch", branch, "--single-branch", REPO_URL, str(REPO_DIR),
             cwd=REPO_DIR.parent, timeout=300)
        return

    if not (REPO_DIR / ".git").exists():
        raise RuntimeError(f"{REPO_DIR} exists but is not a git repository")

    logger.info("Pulling latest repo state from %s branch %s", REPO_URL, branch)
    _git("fetch", "--prune", "origin")
    # -B creates the local branch if missing, or resets it if it exists
    _git("checkout", "-B", branch, f"origin/{branch}")


async def _sync_repo_now() -> str | None:
    try:
        async with _git_lock:
            await asyncio.to_thread(sync_repo_once)
        repo_state["synced"] = True
        repo_state["error"] = None
        repo_state["last_sync"] = time.time()
        repo_version = await asyncio.to_thread(_get_repo_version)
        _update_service_health("sync_repo", ok=True)
        await broadcast({"type": "repo_status", "synced": True, "error": None, "last_sync": repo_state["last_sync"], "repo_version": repo_version})
        return repo_version
    except Exception as exc:
        repo_state["error"] = str(exc)
        _update_service_health("sync_repo", ok=False, error=str(exc))
        await broadcast({"type": "repo_status", "synced": repo_state["synced"], "error": str(exc), "last_sync": repo_state["last_sync"]})
        raise


async def sync_repo() -> None:
    while True:
        try:
            await _sync_repo_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Repository sync failed")
        await asyncio.sleep(settings.get("repo_sync_interval", SYNC_INTERVAL))




def _get_repo_version() -> str | None:
    """Read VERSION= from the synced install-lxc.sh in the repo."""
    try:
        text = _INSTALLER_PATH.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("VERSION="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _cs_webui_branch() -> str:
    branch = str(settings.get("repo_branch", REPO_BRANCH) or REPO_BRANCH).strip()
    return branch or REPO_BRANCH


def _webui_fetch_bytes(url: str, timeout: int) -> bytes:
    """Blocking HTTP GET — must be called via asyncio.to_thread."""
    import urllib.request as _urllib_req
    with _urllib_req.urlopen(url, timeout=timeout) as resp:
        return resp.read()


async def refresh_webui_frontend() -> None:
    """On startup: compare the deployed cs-webui VERSION to the repo and
    download fresh frontend files (app.js, style.css, index.html) if stale.

    This self-heals spoke installs that were created before a cs-webui fix
    was committed — e.g. a broken app.js with a SyntaxError would cause all
    JavaScript to fail, breaking the entire UI.  Running the full installer
    is not required; we only need to swap the three static files.

    All network I/O is offloaded to a thread pool via asyncio.to_thread so
    the event loop is never blocked — a slow/unresponsive GitHub would
    otherwise stall all HTTP request processing on the spoke."""
    branch = _cs_webui_branch()
    raw_base = f"{CS_WEBUI_REPO_RAW}/{branch}"

    # Read deployed version
    local_ver: str | None = None
    try:
        local_ver = (STATIC_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass

    # Fetch remote version (lightweight — just a few bytes); offload to thread
    remote_ver: str | None = None
    try:
        raw = await asyncio.to_thread(_webui_fetch_bytes, f"{raw_base}/VERSION", 10)
        remote_ver = raw.decode(errors="replace").strip()
    except Exception as exc:
        logger.warning("webui refresh: could not fetch remote VERSION: %s", exc)
        return

    if remote_ver == local_ver:
        logger.info("webui refresh: deployed cs-webui %s is current — no update needed", local_ver)
        update_state["cswebui_current"] = local_ver or APP_VERSION
        update_state["cswebui_available"] = remote_ver
        return

    logger.info("webui refresh: deployed=%s  remote=%s — downloading updated files", local_ver, remote_ver)

    # All frontend files to download (rel path from repo root)
    # Includes legacy app.js for backward compat and the new ES module tree
    frontend_files = [
        "static/app.js",
        "static/style.css",
        "static/js/main.js",
        "static/js/state.js",
        "static/js/utils.js",
        "static/js/websocket.js",
        "static/js/nav.js",
        "static/js/agent-log.js",
        "static/js/hub/dashboard.js",
        "static/js/hub/admin.js",
        "static/js/hub/central.js",
        "static/js/spoke/dashboard.js",
        "static/js/spoke/central.js",
    ]
    for rel_path in frontend_files:
        url = f"{raw_base}/{rel_path}"
        # Preserve subdirectory structure under STATIC_DIR
        # rel_path is like "static/js/hub/dashboard.js" → dest is STATIC_DIR/js/hub/dashboard.js
        rel_to_static = Path(rel_path).relative_to("static")
        dest = STATIC_DIR / rel_to_static
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = await asyncio.to_thread(_webui_fetch_bytes, url, 30)
            await asyncio.to_thread(dest.write_bytes, data)
            logger.info("webui refresh: updated %s", rel_to_static)
        except Exception as exc:
            logger.error("webui refresh: failed to download %s: %s", rel_path, exc)
            return  # abort — partial update is worse than stale

    # Download index.html template and inject WEBUI_MODE=spoke
    try:
        raw_html = await asyncio.to_thread(_webui_fetch_bytes, f"{raw_base}/templates/index.html", 30)
        html = raw_html.decode(errors="replace").replace("{{WEBUI_MODE}}", "spoke")
        await asyncio.to_thread((STATIC_DIR / "index.html").write_text, html, "utf-8")
        logger.info("webui refresh: updated index.html (WEBUI_MODE=spoke injected)")
    except Exception as exc:
        logger.error("webui refresh: failed to download index.html: %s", exc)
        return

    # Write updated VERSION so next restart is a no-op
    try:
        await asyncio.to_thread((STATIC_DIR / "VERSION").write_text, remote_ver + "\n", "utf-8")
    except Exception:
        pass

    logger.info("webui refresh: cs-webui updated %s → %s — browser reload required", local_ver, remote_ver)
    update_state["cswebui_current"] = remote_ver
    update_state["cswebui_available"] = remote_ver


async def periodic_webui_refresh() -> None:
    """Background task: re-run refresh_webui_frontend() every 30 minutes so
    frontend fixes are picked up automatically without a service restart."""
    INTERVAL = 1800  # 30 minutes
    await asyncio.sleep(INTERVAL)  # skip first run — startup already did it
    while True:
        try:
            await asyncio.wait_for(refresh_webui_frontend(), timeout=60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("periodic webui refresh error: %s", exc)
        await asyncio.sleep(INTERVAL)




async def acme_renewal_loop() -> None:
    while True:
        try:
            renewed = await spoke_acme.renew_if_needed(BASE_DIR)
            if renewed:
                cert_info = spoke_acme.get_cert_info()
                _acme_status["last_result"] = {"success": True, "expires": cert_info.get("expires"), "domain": cert_info.get("domain"), "renewed": True}
                _acme_status["last_error"] = None
                await broadcast({"type": "cert_renewed", "expires": cert_info.get("expires")})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ACME renewal loop error: %s", exc)
            _acme_status["last_error"] = str(exc)
        await asyncio.sleep(86400)


async def heartbeat_check() -> None:
    await asyncio.sleep(HEARTBEAT_INTERVAL)
    while True:
        try:
            changed = False
            async with state_lock:
                for client in clients.values():
                    online = compute_online(client["last_seen"])
                    if client.get("online") != online:
                        client["online"] = online
                        changed = True
            if changed:
                await broadcast_full_state()

            # Mark proxmox agent offline if it hasn't reported within OFFLINE_TIMEOUT.
            # DO NOT clear proxmox_state["vms"] — the last-known VM list is still
            # accurate for USB/T1-T2 classification and gets refreshed when the agent
            # reconnects.  Clearing it causes all clients to flip to T1 on every agent
            # hiccup, which produces spurious classification noise.
            if proxmox_state.get("connected") and proxmox_state.get("last_seen"):
                age = time.time() - float(proxmox_state["last_seen"])
                if age > OFFLINE_TIMEOUT:
                    proxmox_state["connected"] = False
                    await _broadcast_proxmox_state()
            # Also mark per-agent states stale so the multi-server list stays accurate.
            for _hn, _st in proxmox_states.items():
                if _st.get("connected") and _st.get("last_seen"):
                    if time.time() - float(_st["last_seen"]) > OFFLINE_TIMEOUT:
                        _st["connected"] = False

            # Auto-reset reclone state after 8 hours on successful completion
            if reclone_state.get("status") == "completed":
                last_run = reclone_state.get("last_run") or {}
                ts = _parse_ts(last_run.get("timestamp"))
                if ts and (time.time() - ts) >= 8 * 3600:
                    reclone_state.update({
                        "status": "idle", "type": None, "total": 0,
                        "completed": 0, "failed": 0, "current_vm": None,
                        "log": [], "started_at": None, "last_run": None,
                        "auto_recovery_log": [],
                    })
                    await _broadcast_reclone_state()

            _update_service_health("heartbeat", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("heartbeat", ok=False, error=str(exc))
            logger.exception("Heartbeat check error: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ── Auth endpoints ─────────────────────────────────────────────────────────────

class _SpokeLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class ChangePasswordPayload(BaseModel):
    current_password: str = ""
    new_password: str = ""


class LocalUserCreatePayload(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "admin"


@app.websocket("/ws/console/{session_id}")
async def ws_console_direct(websocket: WebSocket, session_id: str) -> None:
    """Relay raw VNC bytes between the browser (noVNC) and Proxmox vncwebsocket."""
    session = _direct_console_sessions.pop(session_id, None)
    if not session or session.get("expires", 0) < time.time():
        await websocket.close(code=4404, reason="Invalid or expired console session")
        return

    await websocket.accept()
    proxmox_host = session["proxmox_host"]
    node = session["node"]
    vmid = session["vmid"]
    vmtype = session["vmtype"]
    ticket = session["ticket"]
    port = session["port"]
    api_token = str(session.get("api_token") or _get_proxmox_token_for_host(proxmox_host)).strip()
    if not api_token:
        await websocket.close(code=1011, reason="Proxmox API token not configured on spoke")
        return

    import urllib.parse as _urlparse_console
    params = _urlparse_console.urlencode({"port": port, "vncticket": ticket})
    ws_url = f"wss://{proxmox_host}:8006/api2/json/nodes/{node}/{vmtype}/{vmid}/vncwebsocket?{params}"
    auth_header = {"Authorization": f"PVEAPIToken={api_token}"}

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    if websockets is None:
        await websocket.close(code=1011, reason="websockets library not installed")
        return

    try:
        import inspect as _inspect_console
        connect_kwargs: dict[str, Any] = {"ssl": ssl_ctx, "open_timeout": 20, "max_size": None}
        hdr_key = (
            "additional_headers"
            if "additional_headers" in _inspect_console.signature(websockets.connect).parameters
            else "extra_headers"
        )
        connect_kwargs[hdr_key] = auth_header

        async with websockets.connect(ws_url, **connect_kwargs) as px_ws:
            async def _browser_to_proxmox() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect(code=int(msg.get("code") or 1000))
                    raw = msg.get("bytes") or (msg.get("text") or "").encode()
                    if raw:
                        await px_ws.send(raw)

            async def _proxmox_to_browser() -> None:
                async for raw in px_ws:
                    data = raw if isinstance(raw, bytes) else raw.encode()
                    await websocket.send_bytes(data)

            t1 = asyncio.create_task(_browser_to_proxmox())
            t2 = asyncio.create_task(_proxmox_to_browser())
            _, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.warning("Direct VNC relay error for session %s: %s", session_id, exc)


# Cache: (wsite, central_site) → (timestamp, [client_name, ...])
_central_client_cache: dict[str, tuple[float, list[str]]] = {}
_CENTRAL_CLIENT_CACHE_TTL = 60  # seconds



async def _api_health_payload() -> dict[str, Any]:
    async with state_lock:
        client_count = len(clients)
    return {
        "status": "ok",
        "version": APP_VERSION,
        "clients": client_count,
        "repo_synced": repo_state["synced"],
        "repo_error": repo_state["error"],
        "installer_version": INSTALLER_VERSION,
    }


async def _run_acme_request() -> None:
    _acme_status["running"] = True
    _acme_status["last_result"] = None
    _acme_status["last_error"] = None
    await broadcast({"type": "acme_status", **_acme_status})
    try:
        cfg = spoke_acme.load_acme_config()
        result = await spoke_acme.request_certificate(cfg, BASE_DIR)
        _acme_status["last_result"] = result
        _acme_status["last_error"] = None if result.get("success") else result.get("error")
        if result.get("success"):
            settings["spoke_tls"] = "on"
            _save_settings()
            logger.info("TLS certificate ready. Restart the spoke service with SPOKE_TLS=on to enable HTTPS.")
            await broadcast({"type": "cert_renewed", "expires": result.get("expires")})
    except Exception as exc:
        logger.exception("ACME certificate request failed: %s", exc)
        _acme_status["last_error"] = str(exc)
        _acme_status["last_result"] = None
    finally:
        _acme_status["running"] = False
        await broadcast({"type": "acme_status", **_acme_status})


class ConfOverrideBody(BaseModel):
    content: str  # Raw INI text, same format as the target .conf file


async def _apply_client_status(status: ClientStatus) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    # Ignore the Proxmox template VM — it uses the default hostname before
    # being cloned and renamed.  Registering it would pollute the dashboard.
    _IGNORED_HOSTNAMES = set(_parse_json_list(settings.get("ignored_hostnames", '["sim-rpi-0000"]')))
    if status.hostname in _IGNORED_HOSTNAMES:
        return {}, False, {"status": "ignored", "reason": "template hostname"}

    now = utcnow()
    watchdog_changed = False
    async with state_lock:
        existing = clients.get(status.hostname, {})

        # Build timestamped error entries from whatever the client reported this cycle.
        # WHY: clients accumulate errors between reports (e.g. "SSID not found") and
        # flush them here. We stamp them server-side so timestamps are in server time,
        # which is consistent with other log timestamps in the dashboard.
        incoming_errors = [
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "msg": e}
            for e in status.errors
        ]
        existing_errors: list[dict[str, str]] = existing.get("recent_errors", [])
        # Keep a rolling window; oldest entries fall off the front.
        recent_errors = (existing_errors + incoming_errors)[-MAX_CLIENT_ERRORS:]
        total_errors = int(existing.get("error_count", 0)) + len(incoming_errors)

        if incoming_errors:
            logger.warning(
                "Client %s reported %d error(s): %s",
                status.hostname,
                len(incoming_errors),
                "; ".join(e["msg"] for e in incoming_errors),
            )

        clients[status.hostname] = {
            **existing,
            "hostname": status.hostname,
            "simulation_id": status.simulation_id,
            "platform": status.platform,
            "hw_type": status.hw_type or existing.get("hw_type", ""),
            "iteration": status.iteration,
            "connected_ssid": status.connected_ssid,
            "gateway_reachable": status.gateway_reachable,
            "active_simulations": list(status.active_simulations),
            "config": {key: str(value) for key, value in status.config.items()},
            "overrides": existing.get("overrides", {}),
            "last_seen": now,
            "online": True,
            "recent_errors": recent_errors,
            "error_count": total_errors,
        }
        normalized_hostname = str(status.hostname or "").strip().lower()
        for vmid_key, entry in list(vm_watchdog.items()):
            if str(entry.get("hostname") or "").strip().lower() != normalized_hostname:
                continue
            clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
            if clone_completed_at is None or now.timestamp() <= clone_completed_at:
                continue
            vm_watchdog.pop(vmid_key, None)
            watchdog_changed = True
        if watchdog_changed:
            await _async_save_vm_watchdog()
        payload = serialize_client(status.hostname, clients[status.hostname])

    return payload, watchdog_changed, None


# ── Demo Scenario System ──────────────────────────────────────────────────────
# Demo users can trigger named failure scenarios on individual clients without
# needing to understand simulation.conf.  Overrides are:
#   - In-memory only (cleared on hub or spoke reboot automatically)
#   - Auto-expired after DEMO_TTL_SECONDS (120 minutes)
#   - Exclusive: each scenario sets all failure flags explicitly so there is
#     never ambiguity about which failure is active

_DEMO_TTL_SECONDS = 120 * 60  # 120 minutes

_FAILURE_FLAGS = ("dns_fail", "dhcp_fail", "assoc_fail", "auth_fail", "ssidpw_fail", "port_flap")


def _build_demo_scenarios() -> dict[str, dict[str, str]]:
    scenarios: dict[str, dict[str, str]] = {
        "normal": {f: "off" for f in _FAILURE_FLAGS},
    }
    for flag in _FAILURE_FLAGS:
        scenarios[flag] = {f: ("on" if f == flag else "off") for f in _FAILURE_FLAGS}
    return scenarios


DEMO_SCENARIOS: dict[str, dict[str, str]] = _build_demo_scenarios()

# hostname → {scenario, flags, expires_at, triggered_by}
_demo_active: dict[str, dict[str, Any]] = {}


async def _apply_demo_scenario(hostname: str, scenario: str, triggered_by: str) -> dict[str, Any]:
    """Apply a named demo scenario to a client and record its expiry.

    Returns the serialized client payload.  Raises HTTPException if the
    scenario name is unknown or the client is not found.
    """
    flags = DEMO_SCENARIOS.get(scenario)
    if flags is None:
        raise HTTPException(status_code=422, detail=f"Unknown scenario '{scenario}'. Valid: {sorted(DEMO_SCENARIOS)}")

    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail=f"Client '{hostname}' not found")
        clients[hostname].setdefault("overrides", {}).update(flags)
        payload = serialize_client(hostname, clients[hostname])

    if scenario == "normal":
        _demo_active.pop(hostname, None)
    else:
        _demo_active[hostname] = {
            "scenario": scenario,
            "flags": flags,
            "expires_at": time.time() + _DEMO_TTL_SECONDS,
            "triggered_by": triggered_by,
        }

    await broadcast({"type": "overrides_update", "client": payload})
    return payload


async def _clear_demo_scenario(hostname: str) -> dict[str, Any] | None:
    """Clear demo override for one client; returns serialized client or None if not found."""
    _demo_active.pop(hostname, None)
    async with state_lock:
        if hostname not in clients:
            return None
        clients[hostname]["overrides"] = {}
        payload = serialize_client(hostname, clients[hostname])
    await broadcast({"type": "overrides_cleared", "client": payload})
    return payload


def _clear_all_demo_scenarios_sync() -> None:
    """Synchronous best-effort clear of all demo overrides (used on hub reconnect)."""
    _demo_active.clear()
    for client in clients.values():
        client["overrides"] = {}


def _demo_active_summary() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "hostname": h,
            "scenario": v["scenario"],
            "triggered_by": v.get("triggered_by", ""),
            "expires_at": v["expires_at"],
            "minutes_remaining": max(0, round((v["expires_at"] - now) / 60, 1)),
        }
        for h, v in list(_demo_active.items())
    ]


async def _demo_expiry_task() -> None:
    """Background task: check every 30 s and clear any expired demo overrides."""
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            expired = [h for h, v in list(_demo_active.items()) if v["expires_at"] <= now]
            for hostname in expired:
                logger.info("Demo override expired for %s — reverting to normal", hostname)
                await _clear_demo_scenario(hostname)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("demo_expiry_task error: %s", exc)




JOURNAL_UNIT = "client-sim-dashboard"
INSTALL_LOG_PATH = Path("/var/log/client-sim-dashboard-install.log")
WATCHDOG_LOG_PATH = Path("/var/log/proxmox-watchdog.log")
LOG_STREAM_KEEPALIVE_SECS = 15
LOG_STREAM_POLL_SECS = 1


def _normalize_log_source(source: str) -> str:
    normalized = (source or "journal").strip().lower()
    if normalized not in {"journal", "install", "agent", "watchdog"}:
        raise HTTPException(status_code=400, detail=f"Unsupported log source: {source}")
    return normalized


def _log_source_hint(source: str, detail: str | None = None) -> str:
    detail_text = f": {detail}" if detail else ""
    if source == "agent":
        return "[INFO] No Proxmox agent logs yet — logs arrive on the next agent telemetry poll (≤60s after activity)."
    if source == "install":
        return f"[INFO] Install log {INSTALL_LOG_PATH} is not available on this spoke yet. Start an install or update to create it{detail_text}"
    if source == "watchdog":
        return f"[INFO] Watchdog log {WATCHDOG_LOG_PATH} is not available on this host yet{detail_text}"
    return f"[WARN] Live service journal for {JOURNAL_UNIT} is unavailable on this spoke{detail_text}"


def _encode_sse_line(text: str) -> str:
    return f"data: {json.dumps(text)}\n\n"


async def _stream_keepalive() -> str:
    await asyncio.sleep(LOG_STREAM_KEEPALIVE_SECS)
    return ": keepalive\n\n"


async def _poll_agent_inbox(hostname: str, approved_hostname: str | None = None) -> list[dict[str, Any]]:
    async with state_lock:
        # Reset stale 'delivered' commands to 'pending' so they are re-sent if the agent
        # restarted without acking them (same logic as WS reconnect reset).
        # Only reset commands older than 30s to avoid re-delivering in-progress commands.
        _stale_threshold = 30.0
        now = time.time()
        reset = 0
        stale_reset_ids: list[str] = []
        for cmd in commands:
            if (
                cmd.get("status") == "delivered"
                and _command_matches_agent(cmd, hostname, approved_hostname)
                and (now - float(cmd.get("updated_at") or cmd.get("created_at") or now)) >= _stale_threshold
            ):
                cmd["status"] = "pending"
                cmd["updated_at"] = now
                reset += 1
                stale_reset_ids.append(cmd.get("id", ""))
        if reset:
            _save_commands()
            for cmd_id in stale_reset_ids:
                _trace("stale_reset", hostname=hostname, approved_as=approved_hostname, cmd_id=cmd_id)
        pending, expired, purged = _peek_pending_agent_commands_locked(hostname, approved_hostname)
        if pending:
            _mark_commands_delivered_locked([command["id"] for command in pending])
        serialized = _serialize_commands()
        payload = [_serialize_command_for_agent(command) for command in pending]

    _trace(
        "inbox_polled",
        hostname=hostname,
        approved_as=approved_hostname,
        commands_delivered=len(pending) if pending else 0,
        stale_reset=reset,
        actions=[c.get("action") for c in pending] if pending else [],
        cmd_ids=[c.get("id") for c in pending] if pending else [],
    )
    if pending or expired or purged or reset:
        await broadcast({"type": "commands_update", "commands": serialized})
    return payload


@app.websocket("/ws/client")
async def ws_client_endpoint(
    websocket: WebSocket,
    hostname: str = Query(""),
    platform: str = Query("linux"),
    api_key: str = Query(""),
) -> None:
    if not hostname:
        await websocket.close(code=4400, reason="hostname is required")
        return
    if not _valid_shared_client_key(api_key):
        logger.warning("Rejected /ws/client for %s with invalid shared client API key", hostname or "unknown")
        await websocket.close(code=4403, reason="invalid client key")
        return
    await websocket.accept()
    client_ws_connections[hostname] = websocket
    try:
        await _push_pending_agent_commands(hostname, websocket)
        await websocket.send_json({"type": "hello", "hostname": hostname, "platform": platform})
        # Send current throttle directive so client immediately uses the right interval
        if _server_pressure["throttle_interval"] != 15:
            await websocket.send_json({"type": "throttle", "interval": _server_pressure["throttle_interval"]})
        while True:
            data = await websocket.receive_json()
            msg_type = str(data.get("type") or "status").strip().lower()
            if msg_type == "status":
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                payload.setdefault("hostname", hostname)
                payload.setdefault("platform", platform)
                status = ClientStatus(**payload)
                client_payload, watchdog_changed, ignored = await _apply_client_status(status)
                if ignored is None:
                    await broadcast({"type": "status_update", "client": client_payload})
                    if watchdog_changed:
                        await _broadcast_proxmox_state()
                    asyncio.create_task(_sync_sim_tags_for_client(hostname))
                    await websocket.send_json({"type": "status_ack", "hostname": hostname})
            elif msg_type == "ack":
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                await _ack_command_internal(payload)
                await websocket.send_json({"type": "ack_ok", "id": payload.get("id")})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "sync":
                await _push_pending_agent_commands(hostname, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        if client_ws_connections.get(hostname) is websocket:
            client_ws_connections.pop(hostname, None)


@app.websocket("/ws/proxmox")
async def ws_proxmox_endpoint(
    websocket: WebSocket,
    hostname: str = Query(""),
    api_key: str = Query(""),
) -> None:
    if not hostname:
        await websocket.close(code=4400, reason="hostname is required")
        return
    approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, websocket.client.host if websocket.client else "unknown", time.time())
    if response is not None or approved_hostname is None:
        detail = response.body.decode("utf-8", errors="ignore") if isinstance(response, JSONResponse) else "agent not approved"
        await websocket.close(code=4401, reason=detail[:120])
        return
    await websocket.accept()
    state.proxmox_ws_connection = websocket
    state.proxmox_ws_hostname = approved_hostname
    if state.proxmox_ws_disconnect_task is not None:
        state.proxmox_ws_disconnect_task.cancel()
        state.proxmox_ws_disconnect_task = None
    _trace("agent_ws_connect", hostname=approved_hostname)
    try:
        # Reset any 'delivered' commands back to 'pending' so they are re-sent.
        # Commands pushed via WS before a spoke restart are marked 'delivered' but
        # never acked (agent lost the connection), so they would be silently abandoned.
        async with state_lock:
            reset_count = _reset_delivered_commands_locked(approved_hostname, approved_hostname)
        await _push_pending_agent_commands(approved_hostname, websocket, approved_hostname)
        while True:
            data = await websocket.receive_json()
            msg_type = str(data.get("type") or "telemetry").strip().lower()
            if msg_type == "telemetry":
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
                payload_hostname = str(node.get("hostname") or hostname).strip() or hostname
                if not _proxmox_hostnames_match(payload_hostname, approved_hostname):
                    await websocket.close(code=4403, reason="hostname mismatch")
                    return
                await _apply_proxmox_telemetry_state(payload, approved_hostname, time.time())
                await websocket.send_json({"type": "telemetry_ack", "hostname": approved_hostname})
            elif msg_type in {"backup_progress", "reseed_progress"}:
                await _relay_proxmox_progress_to_hub(data)
            elif msg_type == "ack":
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                await _ack_command_internal(payload)
                await websocket.send_json({"type": "ack_ok", "id": payload.get("id")})
            elif msg_type in {"token_provisioned", "token_provision_error"}:
                req_id = str(data.get("request_id") or "").strip()
                q = _proxmox_token_provision_queues.get(req_id)
                if q is not None:
                    if msg_type == "token_provisioned":
                        await q.put({"ok": True, "token": data.get("token")})
                    else:
                        await q.put({"ok": False, "error": data.get("error", "Agent token provision failed")})
                else:
                    logger.warning("Received %s but no waiting provision queue for request_id=%r", msg_type, req_id)
            elif msg_type == "ping":
                # Agent heartbeat — update last_seen so UI stays current even
                # when full telemetry times out (e.g. during a VM reclone).
                proxmox_state["last_seen"] = time.time()
                proxmox_state["connected"] = True
                await websocket.send_json({"type": "pong"})
            elif msg_type == "sync":
                async with state_lock:
                    _reset_delivered_commands_locked(approved_hostname, approved_hostname)
                await _push_pending_agent_commands(approved_hostname, websocket, approved_hostname)
    except WebSocketDisconnect:
        pass
    finally:
        if state.proxmox_ws_connection is websocket:
            state.proxmox_ws_connection = None
            state.proxmox_ws_hostname = approved_hostname
            _trace("agent_ws_disconnect", hostname=approved_hostname)
            state.proxmox_ws_disconnect_task = asyncio.create_task(_proxmox_disconnect_grace(approved_hostname))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    token = (websocket.query_params.get("token") or _parse_bearer_token(websocket.headers.get("authorization"))).strip()
    expected_token = str(settings.get("admin_ws_token", "") or "").strip()
    if not expected_token or not token or not secrets.compare_digest(token, expected_token):
        logger.warning("Rejected browser WebSocket connection with invalid admin token")
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    ws_connections.append(websocket)
    # Send initial state snapshot — each message is individually guarded so one
    # serialisation error cannot take down the entire connection.
    try:
        await websocket.send_text(json.dumps({"type": "full_state", "clients": await current_clients()}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect full_state error: %s", exc)
    try:
        state._repo_ver = await asyncio.to_thread(_get_repo_version)
        await websocket.send_text(json.dumps({"type": "repo_status", "synced": repo_state["synced"], "error": repo_state["error"], "last_sync": repo_state["last_sync"], "repo_version": state._repo_ver}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect repo_status error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "relay_status", **_relay_status_payload()}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect relay_status error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "settings_update", "settings": await api_settings_get()}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect settings_update error: %s", exc)
    try:
        if (
            proxmox_state["connected"]
            or proxmox_state["vms"]
            or proxmox_state.get("usb_state")
            or proxmox_state.get("unknown_usb")
            or pending_proxmox_agents
            or approved_proxmox_agents
        ):
            await websocket.send_text(json.dumps({"type": "proxmox_update", **_proxmox_status_payload()}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect proxmox_update error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "reclone_update", **dict(reclone_state)}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect reclone_update error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "update_all_progress", **dict(state.update_all_state)}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect update_all_progress error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(state.central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect central_update error: %s", exc)
    try:
        # Send current kill switch state so reconnecting clients don't miss a change
        await websocket.send_text(json.dumps({"type": "gkill_switch_update", "value": gkill_switch_state["value"]}))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect gkill_switch_update error: %s", exc)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(ValueError):
            ws_connections.remove(websocket)


_SPOKE_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VM Console</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { width: 100%; height: 100%; background: #1a1a2e; overflow: hidden; }
    #toolbar {
      display: flex; align-items: center; gap: 10px;
      padding: 6px 12px; background: #16213e; border-bottom: 1px solid #333;
      color: #ccc; font-family: sans-serif; font-size: 13px;
    }
    #toolbar button {
      padding: 4px 12px; border: 1px solid #555; border-radius: 4px;
      background: #0f3460; color: #eee; cursor: pointer; font-size: 12px;
    }
    #toolbar button:hover { background: #1a5276; }
    #status { margin-left: auto; font-size: 12px; }
    #status.connected { color: #2ecc71; }
    #status.disconnected { color: #e74c3c; }
    #status.connecting { color: #f39c12; }
    #screen { width: 100%; height: calc(100vh - 38px); }
    #screen canvas { width: 100% !important; height: 100% !important; }
  </style>
</head>
<body>
  <div id="toolbar">
    <strong>VM Console</strong>
    <button onclick="sendCtrlAltDel()">Ctrl+Alt+Del</button>
    <button onclick="toggleFullscreen()">Fullscreen</button>
    <span id="status" class="connecting">Connecting\u2026</span>
  </div>
  <div id="screen"></div>
  <script type="module">
    import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js';
    const sessionId = '__SESSION_ID__';
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = proto + '//' + location.host + '/ws/console/' + sessionId;
    const statusEl = document.getElementById('status');
    let rfb;
    function setStatus(msg, cls) { statusEl.textContent = msg; statusEl.className = cls; }
    try {
      rfb = new RFB(document.getElementById('screen'), wsUrl, { credentials: { password: '' } });
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.addEventListener('connect', () => setStatus('Connected', 'connected'));
      rfb.addEventListener('disconnect', (e) => setStatus('Disconnected: ' + (e.detail?.reason || 'closed'), 'disconnected'));
      rfb.addEventListener('credentialsrequired', () => { const p = prompt('VNC Password:') || ''; rfb.sendCredentials({ password: p }); });
      rfb.addEventListener('securityfailure', (e) => setStatus('Security failure: ' + (e.detail?.reason || 'unknown'), 'disconnected'));
    } catch (err) { setStatus('Error: ' + err.message, 'disconnected'); }
    window.rfb = rfb;
    window.sendCtrlAltDel = () => rfb && rfb.sendCtrlAltDel();
    window.toggleFullscreen = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.getElementById('screen').requestFullscreen().catch(() => {});
    };
  </script>
</body>
</html>"""


@app.get("/console", response_class=HTMLResponse)
async def console_page(session_id: str = Query(...)) -> HTMLResponse:
    """Serve the noVNC console page for a direct Proxmox VM console session."""
    return HTMLResponse(content=_SPOKE_CONSOLE_HTML.replace("__SESSION_ID__", session_id))


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    index = STATIC_DIR / "index.html"
    html = index.read_text()
    html = html.replace("{{WEBUI_MODE}}", "spoke")
    auth_provider = _normalize_spoke_auth_provider(settings.get("auth_provider", "local"))
    auth_required = _spoke_auth_required()
    authenticated = not auth_required or bool(_validate_spoke_session(request.cookies.get(_SPOKE_SESSION_COOKIE, "")))
    html = html.replace(
        "</head>",
        (
            f"<script>window.__SPOKE_WS_TOKEN__ = {json.dumps(settings.get('admin_ws_token', ''))};"
            f"window.__SPOKE_AUTH_REQUIRED__ = {json.dumps(auth_required)};"
            f"window.__SPOKE_AUTHENTICATED__ = {json.dumps(authenticated)};"
            f"window.__SPOKE_AUTH_PROVIDER__ = {json.dumps(auth_provider)};</script></head>"
        ),
        1,
    )
    # Inject version as cache-busting query param on static assets so the browser
    # automatically fetches updated files after "Check & Update Now" — no manual
    # hard-refresh required.
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={APP_VERSION}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={APP_VERSION}"')
    html = html.replace('src="/static/js/main.js"', f'src="/static/js/main.js?v={APP_VERSION}"')
    return HTMLResponse(content=html)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# === extracted service rebinds (must precede router includes below) ===

# state_cache helpers moved to services/state_cache.py
from services.state_cache import (  # noqa: E402,F401 (re-export for internal callers)
    _load_state_cache,
    _save_state_cache,
)

# notifications helpers moved to services/notifications.py
from services.notifications import (  # noqa: E402,F401 (re-export for internal callers)
    _check_transitions_and_notify,
    _public_notification_settings,
    _send_email_notifications,
    _send_teams_notifications,
)

# proxmox_agent helpers moved to services/proxmox_agent.py
from services.proxmox_agent import (  # noqa: E402,F401 (re-export for internal callers)
    _ack_command_internal,
    _apply_proxmox_telemetry_state,
    _approved_proxmox_payload,
    _async_save_commands,
    _async_save_reclone_state,
    _async_save_vm_watchdog,
    _authorize_proxmox_agent,
    _broadcast_proxmox_state,
    _broadcast_reclone_state,
    _broadcast_update_state,
    _cleanup_commands_locked,
    _command_args_signature,
    _command_matches_agent,
    _enqueue_command_locked,
    _find_active_duplicate_command_locked,
    _find_proxmox_vm,
    _forward_hub_passthrough_to_proxmox,
    _get_proxmox_host_config,
    _get_proxmox_token_for_host,
    _guest_supports_reclone,
    _handle_command_trace_request,
    _handle_provision_proxmox_token,
    _handle_vnc_proxy_request,
    _has_any_proxmox_token,
    _has_pending_reclone,
    _hub_check_approval,
    _hub_command_blocked_by_reseed,
    _hub_config_isolation_result,
    _hub_isolated,
    _hub_reseed_block_result,
    _hub_self_register,
    _hub_targets_proxmox_agent,
    _hub_tls_verify,
    _load_commands,
    _load_reclone_state,
    _load_update_state,
    _load_vm_watchdog,
    _make_command,
    _mark_commands_delivered_locked,
    _normalize_command_action,
    _normalize_command_type,
    _normalize_proxmox_hostname,
    _normalize_proxmox_usb_state,
    _parse_reclone_schedule,
    _peek_pending_agent_commands_locked,
    _pending_proxmox_payload,
    _proxmox_disconnect_grace,
    _proxmox_hostname_aliases,
    _proxmox_hostnames_match,
    _proxmox_status_payload,
    _proxmox_unassigned_present_usb,
    _proxmox_update_args,
    _proxmox_update_branch,
    _proxmox_usb_config_payload,
    _push_pending_agent_commands,
    _push_pending_commands_for_target,
    _push_pending_commands_for_targets,
    _push_to_github,
    _queue_command,
    _queue_proxmox_agent_update,
    _queue_proxmox_command,
    _queue_unlock_template_command,
    _reclone_command_args,
    _reclone_targets_for_run,
    _record_vm_watchdog_clone_completed,
    _relay_hub_base_url,
    _relay_proxmox_progress_to_hub,
    _relay_vnc_to_hub,
    _reset_delivered_commands_locked,
    _resolve_proxmox_agent_hostname,
    _resolve_proxmox_update_target,
    _resolve_proxmox_vm_target,
    _revert_hub_managed_if_auth_failure,
    _run_hub_repo_sync,
    _run_rolling_reclone,
    _run_self_update,
    _run_update_all,
    _sanitize_proxmox_tag,
    _save_commands,
    _save_proxmox_host_config,
    _save_proxmox_token_for_host,
    _save_reclone_state,
    _save_update_state,
    _save_vm_watchdog,
    _serialize_command_for_agent,
    _serialize_commands,
    _trim_commands_locked,
    _update_ini_section,
    _update_provision_run_state,
    _update_reclone_log,
    _update_service_health,
    _upsert_pending_proxmox_agent,
    _vm_watchdog_key,
    check_for_update,
    expire_commands,
    hub_isolation_monitor,
    vm_watchdog_loop,
)

# aruba_poller helpers moved to services/aruba_poller.py
from services.aruba_poller import (  # noqa: E402,F401 (re-export for internal callers)
    _apply_central_feed,
    _central_cfg,
    _central_headers,
    _central_ready,
    _central_status_payload,
    _central_token_state,
    _fetch_central_client_names,
    _fetch_central_token,
    _fetch_new_central_token,
    _hw_alerts_payload,
    _is_new_central_api,
    _poll_central_once,
    _probe_central_token,
    _public_central_api_settings,
    _refresh_central_token,
    _reset_central_runtime_tokens,
    _sim_clients_per_wsite,
    _sync_central_runtime_config,
    _telemetry_filtered_browse_dict,
    _telemetry_filtered_browse_list,
    _test_classic_central_connection,
    central_poller,
    central_token_manager,
)

# settings helpers moved to services/settings.py
from services.settings import (  # noqa: E402,F401 (re-export for internal callers)
    _get_cached_settings,
    _public_acme_settings,
    _public_settings,
)

# === end service rebinds ===


# auth routes moved to routers/auth.py
from routers import auth as _auth_router  # noqa: E402
app.include_router(_auth_router.router)

# aruba routes moved to routers/aruba.py
from routers import aruba as _aruba_router  # noqa: E402
app.include_router(_aruba_router.router)

# proxmox routes moved to routers/proxmox.py
from routers import proxmox as _proxmox_router  # noqa: E402
app.include_router(_proxmox_router.router)

# config routes moved to routers/config.py
from routers import config as _config_router  # noqa: E402
app.include_router(_config_router.router)

# commands routes moved to routers/commands.py
from routers import commands as _commands_router  # noqa: E402
app.include_router(_commands_router.router)

# relay routes moved to routers/relay.py
from routers import relay as _relay_router  # noqa: E402
app.include_router(_relay_router.router)

# logs routes moved to routers/logs.py
from routers import logs as _logs_router  # noqa: E402
app.include_router(_logs_router.router)

# settings routes moved to routers/settings.py
from routers import settings as _settings_router  # noqa: E402
app.include_router(_settings_router.router)
from routers.settings import api_settings_get  # noqa: E402,F401 (re-export for internal callers)

# simulations routes moved to routers/simulations.py
from routers import simulations as _simulations_router  # noqa: E402
app.include_router(_simulations_router.router)

# system routes moved to routers/system.py
from routers import system as _system_router  # noqa: E402
app.include_router(_system_router.router)

# updates routes moved to routers/updates.py
from routers import updates as _updates_router  # noqa: E402
app.include_router(_updates_router.router)

# clients routes moved to routers/clients.py
from routers import clients as _clients_router  # noqa: E402
app.include_router(_clients_router.router)

# misc routes moved to routers/misc.py
from routers import misc as _misc_router  # noqa: E402
app.include_router(_misc_router.router)

# Demo scenario routes moved to routers/demo.py
from routers import demo as _demo_router  # noqa: E402
app.include_router(_demo_router.router)
