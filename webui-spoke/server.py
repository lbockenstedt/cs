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

logging.basicConfig(level=logging.INFO)
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
REPO_URL = os.getenv("REPO_URL", "https://github.com/solutions-hpe/client-sim.git")
CLIENT_SIM_REPO_RAW = os.getenv("CLIENT_SIM_REPO_RAW", "https://raw.githubusercontent.com/solutions-hpe/client-sim")
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


def _parse_protected_vmids(raw: str) -> list[int | tuple[int, int]]:
    """Parse a protected VMIDs string into a list of ints and (lo, hi) range tuples.

    Accepts comma-separated entries where each entry is either a single VMID
    (e.g. ``101``) or an inclusive range (e.g. ``100-90000``).
    """
    result: list[int | tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            # Could be a range like "100-90000"
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
                if lo <= hi:
                    result.append((lo, hi))
            except ValueError:
                pass
        else:
            try:
                result.append(int(part))
            except ValueError:
                pass
    return result


_TEMPLATE_VMID_RANGE_CAP = 1000


def _normalize_vmid_spec(raw: Any, *, field_name: str = "template VMID spec") -> str:
    parts: list[str] = []
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"{field_name}: range start must be <= end ({token})")
            if (hi - lo) > _TEMPLATE_VMID_RANGE_CAP:
                raise ValueError(f"{field_name}: range too large ({token}); max span is {_TEMPLATE_VMID_RANGE_CAP + 1} VMIDs")
            parts.append(f"{lo}-{hi}")
            continue
        if re.fullmatch(r"\d+", token):
            parts.append(str(int(token)))
            continue
        raise ValueError(f"{field_name}: invalid token '{token}'")
    return ", ".join(parts)


def _parse_vmid_spec(raw: Any, *, field_name: str = "template VMID spec") -> list[int]:
    normalized = _normalize_vmid_spec(raw, field_name=field_name)
    vmids: set[int] = set()
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            vmids.update(range(lo, hi + 1))
        else:
            vmids.add(int(token))
    return sorted(vmids)


def _template_spec_key(slot: int) -> str:
    return f"vm_image_{slot}_template_spec"


def _template_id_key(slot: int) -> str:
    return f"vm_image_{slot}_template_id"


def _legacy_template_id(source: Mapping[str, Any], slot: int) -> str:
    if slot == 1:
        keys = ("vm_image_1_template_id", "usb_linux_template_id", "usb_template_id")
        default = "100"
    else:
        keys = ("vm_image_2_template_id", "usb_windows_template_id")
        default = "200"
    for key in keys:
        raw = str(source.get(key, "") or "").strip()
        if re.fullmatch(r"\d+", raw):
            return str(max(1, int(raw)))
    return default


def _resolved_template_spec(source: Mapping[str, Any], slot: int) -> str:
    spec_key = _template_spec_key(slot)
    if spec_key in source:
        raw = str(source.get(spec_key, "") or "").strip()
        if not raw:
            return ""
        try:
            return _normalize_vmid_spec(raw, field_name=spec_key)
        except ValueError:
            return _legacy_template_id(source, slot)
    return _legacy_template_id(source, slot)


def _primary_template_id(spec: str, fallback: str) -> str:
    vmids = _parse_vmid_spec(spec) if str(spec or "").strip() else []
    return str(vmids[0]) if vmids else fallback


def _validate_template_specs(spec1: str, spec2: str) -> None:
    overlap = sorted(set(_parse_vmid_spec(spec1, field_name="vm_image_1_template_spec")) & set(_parse_vmid_spec(spec2, field_name="vm_image_2_template_spec")))
    if overlap:
        preview = ", ".join(str(vmid) for vmid in overlap[:5])
        suffix = "…" if len(overlap) > 5 else ""
        raise HTTPException(status_code=422, detail=f"VM Image 1 and VM Image 2 template VMID specs overlap: {preview}{suffix}")


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
_ENC_PREFIX = "enc:"
_SENSITIVE_CFG_KEYS = {"access_token", "refresh_token", "client_secret"}
_SENSITIVE_CLASSIC_API_KEYS = {"password"}
_SENSITIVE_CENTRAL_API_KEYS = {"client_secret"}
_SENSITIVE_TOP_KEYS = {"relay_api_key", "github_token", "client_api_key", "admin_ws_token", "admin_password", "auth_ldap_bind_password", "auth_radius_secret", "auth_tacacs_secret"}
_SENSITIVE_TOP_DICT_KEYS = {"proxmox_approved_agents"}
_SENSITIVE_NOTIF_KEYS = {"smtp_password", "teams_webhook_url"}

try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _key_file = BASE_DIR / ".secret_key"
    if _key_file.exists():
        _fernet = _Fernet(_key_file.read_bytes().strip())
    else:
        _fernet = None
        logger.warning("No .secret_key found — credentials stored as plaintext")
except Exception:
    _fernet = None
    logger.warning("cryptography unavailable or key error — credentials stored as plaintext")


def _encrypt_secret(value: str) -> str:
    if not _fernet or not value:
        return value
    return _ENC_PREFIX + _fernet.encrypt(value.encode()).decode()


def _decrypt_secret(value: str) -> str:
    if not value or not value.startswith(_ENC_PREFIX):
        return value  # plaintext or empty — return as-is (legacy compat)
    if not _fernet:
        return value  # no key — return ciphertext unchanged
    try:
        return _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt a secret field — may be corrupted or from a different key")
        return ""


def _encrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields encrypted for disk storage."""
    out = copy.deepcopy(raw)
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _encrypt_secret(out[key])
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _encrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _encrypt_secret(out["central_config"][key])
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _encrypt_secret(out["central_api"]["classic"][key])
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _encrypt_secret(out["central_api"]["central"][key])
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _encrypt_secret(out["notifications"][key])
    return out


def _decrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields decrypted into memory."""
    out = copy.deepcopy(raw)
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _decrypt_secret(out[key])
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _decrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _decrypt_secret(out["central_config"][key])
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _decrypt_secret(out["central_api"]["classic"][key])
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _decrypt_secret(out["central_api"]["central"][key])
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _decrypt_secret(out["notifications"][key])
    return out


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


_settings_cache: dict[str, Any] = {}
_settings_cache_time: float = 0.0
_SETTINGS_CACHE_TTL: float = 30.0  # seconds


def _invalidate_settings_cache() -> None:
    global _settings_cache, _settings_cache_time
    _settings_cache = {}
    _settings_cache_time = 0.0


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
_state_cache_last_save: float = 0.0
STATE_CACHE_MIN_INTERVAL = 10.0  # max one write per 10 s

# WS delta: skip proxmox broadcast when payload hasn't changed
_last_proxmox_hash: str = ""

# INI cache: avoid re-parsing simulation.conf + client-setup.conf on every request
_sim_conf_cache: dict[str, Any] = {
    "sim_mtime": -1.0,
    "client_mtime": -1.0,
    "simulations": {},
}

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


async def _async_save_vm_watchdog() -> None:
    """Persist VM watchdog state without blocking the event loop."""
    serialized = json.dumps(vm_watchdog, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, VM_WATCHDOG_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist VM watchdog state to %s: %s", VM_WATCHDOG_FILE, exc)


async def _async_save_commands() -> None:
    """Persist command queue without blocking the event loop."""
    serialized = json.dumps(commands, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, COMMAND_QUEUE_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist command queue to %s: %s", COMMAND_QUEUE_FILE, exc)


async def _async_save_reclone_state() -> None:
    """Persist reclone state without blocking the event loop."""
    serialized = json.dumps(reclone_state, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, RECLONE_STATE_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist reclone state to %s: %s", RECLONE_STATE_FILE, exc)


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


def _save_state_cache(force: bool = False) -> None:
    global _state_cache_last_save
    now = time.time()
    if not force and (now - _state_cache_last_save) < STATE_CACHE_MIN_INTERVAL:
        return
    try:
        cache = {
            "proxmox_state": dict(proxmox_state),
            "proxmox_states": {k: {ek: ev for ek, ev in v.items() if ek != "vms"} for k, v in proxmox_states.items()},
            "central_status": central_status,
            "central_wireless_clients": dict(central_wireless_clients),
            "repo_state": dict(repo_state),
            "ts": now,
        }
        _atomic_write_json(STATE_CACHE_FILE, cache)
        _state_cache_last_save = now
    except Exception as exc:
        logger.warning("Could not write state cache: %s", exc)


def _save_commands() -> None:
    try:
        _atomic_write_json(COMMAND_QUEUE_FILE, commands)
    except Exception as exc:
        logger.warning("Could not persist command queue to %s: %s", COMMAND_QUEUE_FILE, exc)


def _save_reclone_state() -> None:
    try:
        _atomic_write_json(RECLONE_STATE_FILE, reclone_state)
    except Exception as exc:
        logger.warning("Could not persist reclone state to %s: %s", RECLONE_STATE_FILE, exc)


def _save_relay_state() -> None:
    try:
        _atomic_write_json(RELAY_STATE_FILE, relay_state)
    except Exception as exc:
        logger.warning("Could not persist relay state to %s: %s", RELAY_STATE_FILE, exc)


def _save_update_state() -> None:
    try:
        _atomic_write_json(UPDATE_STATE_FILE, update_state)
    except Exception as exc:
        logger.warning("Could not persist update state to %s: %s", UPDATE_STATE_FILE, exc)


def _save_vm_watchdog() -> None:
    try:
        _atomic_write_json(VM_WATCHDOG_FILE, vm_watchdog)
    except Exception as exc:
        logger.warning("Could not persist VM watchdog state to %s: %s", VM_WATCHDOG_FILE, exc)


def _load_state_cache() -> None:
    """Restore last-known state from disk so the UI renders immediately on restart
    instead of showing empty state for up to one full agent poll interval (60 s)."""
    try:
        if not STATE_CACHE_FILE.exists():
            return
        cache = json.loads(STATE_CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - cache.get("ts", 0)
        cached_px = cache.get("proxmox_state", {})
        if cached_px:
            proxmox_state.update(cached_px)
            # Restore connected status only if last_seen is within OFFLINE_TIMEOUT;
            # otherwise agent has gone quiet and we should show disconnected.
            last_seen_ts = cached_px.get("last_seen")
            if last_seen_ts and (time.time() - last_seen_ts) <= OFFLINE_TIMEOUT:
                proxmox_state["connected"] = cached_px.get("connected", False)
            else:
                proxmox_state["connected"] = False
        central_status.update(cache.get("central_status", {}))
        central_wireless_clients.update(cache.get("central_wireless_clients", {}))
        # Restore per-agent states (without VMs — those re-populate on first telemetry push).
        # Mark connected=False if the agent hasn't been seen within OFFLINE_TIMEOUT.
        cached_px_states = cache.get("proxmox_states", {})
        for hn, st in cached_px_states.items():
            if not isinstance(st, dict):
                continue
            last_seen_ts = st.get("last_seen")
            connected = bool(st.get("connected", False)) and bool(
                last_seen_ts and (time.time() - last_seen_ts) <= OFFLINE_TIMEOUT
            )
            proxmox_states[hn] = {**st, "connected": connected, "vms": []}
        cached_repo = cache.get("repo_state", {})
        if cached_repo:
            # Restore last_sync timestamp and last error for display, but mark
            # synced=False — we haven't actually synced since this restart yet.
            repo_state["last_sync"] = cached_repo.get("last_sync")
            repo_state["error"] = cached_repo.get("error")
            repo_state["synced"] = False
        logger.info("Restored state cache from disk (age=%.0fs)", age)
    except Exception as exc:
        logger.warning("Could not load state cache: %s", exc)


def _load_commands() -> None:
    try:
        if not COMMAND_QUEUE_FILE.exists():
            return
        raw = json.loads(COMMAND_QUEUE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("command queue must be a list")
        now = time.time()
        changed = False
        restored: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                changed = True
                continue
            cmd = dict(entry)
            if cmd.get("status") in ("executing", "delivered"):
                cmd["status"] = "pending"
                cmd["updated_at"] = now
                changed = True
            restored.append(cmd)
        commands[:] = restored
        expired, purged = _cleanup_commands_locked(now)
        if expired or purged:
            changed = True
        if changed:
            _save_commands()
        logger.info("Restored %d command(s) from disk", len(commands))
    except Exception as exc:
        logger.warning("Could not load command queue: %s", exc)


def _load_reclone_state() -> None:
    try:
        if not RECLONE_STATE_FILE.exists():
            return
        raw = json.loads(RECLONE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("reclone state must be an object")
        for key in reclone_state:
            if key in raw:
                reclone_state[key] = raw[key]
        changed = False
        if reclone_state.get("status") == "running":
            reclone_state["status"] = "interrupted"
            reclone_state["current_vm"] = None
            reclone_state["started_at"] = None
            log_entry = {
                "vmid": None,
                "name": "System",
                "status": "interrupted",
                "timestamp": iso_utcnow(),
                "message": "Rolling reclone interrupted by restart",
            }
            reclone_state["log"] = list(reclone_state.get("log") or []) + [log_entry]
            reclone_state["log"] = reclone_state["log"][-200:]
            changed = True
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
                changed = True
        if changed:
            _save_reclone_state()
        logger.info("Restored reclone state from disk")
    except Exception as exc:
        logger.warning("Could not load reclone state: %s", exc)


def _load_relay_state() -> None:
    global relay_registration_refresh_needed
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
        relay_registration_refresh_needed = bool(relay_state.get("enabled"))
        _save_relay_state()
        logger.info("Restored relay state from disk")
    except Exception as exc:
        logger.warning("Could not load relay state: %s", exc)


def _load_update_state() -> None:
    try:
        if not UPDATE_STATE_FILE.exists():
            return
        raw = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("update state must be an object")
        for key in update_state:
            # Never restore current_version from disk — it must always reflect
            # the INSTALLER_VERSION file so that a self-update restart shows the
            # new version rather than the stale pre-update value.
            if key == "current_version":
                continue
            if key in raw:
                update_state[key] = raw[key]
        if update_state.get("update_in_progress"):
            update_state["update_in_progress"] = False
            update_state["update_error"] = "Update interrupted by restart"
            _save_update_state()
        logger.info("Restored update state from disk")
    except Exception as exc:
        logger.warning("Could not load update state: %s", exc)


def _load_vm_watchdog() -> None:
    try:
        if not VM_WATCHDOG_FILE.exists():
            return
        raw = json.loads(VM_WATCHDOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("vm watchdog state must be an object")
        restored: dict[str, dict[str, Any]] = {}
        changed = False
        for raw_vmid, entry in raw.items():
            if not isinstance(entry, dict):
                changed = True
                continue
            try:
                vmid_key = str(int(raw_vmid))
            except (TypeError, ValueError):
                changed = True
                continue
            clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
            if clone_completed_at is None:
                changed = True
                continue
            try:
                reclone_count = max(0, int(entry.get("reclone_count", 0) or 0))
            except (TypeError, ValueError):
                reclone_count = 0
                changed = True
            normalized = {
                "clone_completed_at": clone_completed_at,
                "reclone_count": reclone_count,
                "hostname": str(entry.get("hostname") or "").strip(),
            }
            if entry != normalized or raw_vmid != vmid_key:
                changed = True
            restored[vmid_key] = normalized
        vm_watchdog.clear()
        vm_watchdog.update(restored)
        if changed:
            _save_vm_watchdog()
        logger.info("Restored VM watchdog state from disk")
    except Exception as exc:
        logger.warning("Could not load VM watchdog state: %s", exc)


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


def _reset_central_runtime_tokens() -> None:
    global central_auth_error
    central_token["access_token"] = None
    central_token["refresh_token"] = None
    central_token["expires_at"] = 0.0
    central_auth_error = None


def _get_cached_settings() -> dict[str, Any]:
    global _settings_cache, _settings_cache_time
    now = time.monotonic()
    if _settings_cache and (now - _settings_cache_time) < _SETTINGS_CACHE_TTL:
        return copy.deepcopy(_settings_cache)

    cfg = dict(settings["central_config"])
    # Strip all secrets — return only non-sensitive fields + presence flags.
    # Runtime token flags are refreshed in api_settings_get() so they never go stale.
    for secret_key in ("client_secret", "access_token", "refresh_token"):
        cfg.pop(secret_key, None)
    cfg["access_token_configured"] = bool(settings["central_config"].get("access_token") or central_token.get("access_token"))
    cfg["refresh_token_configured"] = bool(settings["central_config"].get("refresh_token") or central_token.get("refresh_token"))
    cfg["client_secret_configured"] = bool(settings["central_config"].get("client_secret"))

    _settings_cache = {
        "repo_url": REPO_URL,
        "repo_branch": settings.get("repo_branch", ""),
        "repo_sync_interval": settings.get("repo_sync_interval", SYNC_INTERVAL),
        "session_timeout_minutes": int(settings.get("session_timeout_minutes", 30)),
        "github_token_configured": bool(settings.get("github_token")),
        "hub_managed": bool(settings.get("hub_managed", False)),
        "hub_isolation_timeout": int(settings.get("hub_isolation_timeout", 3600)),  # Expose the stored isolation timeout so the setup form can render the current safeguard value.
        "central_api": _public_central_api_settings(),
        "central_config": cfg,
        "site_mappings": settings["site_mappings"],
        "spoke_monitored_items": settings.get("spoke_monitored_items", []),
        "monitored_checks": settings["monitored_checks"],
        "hardware_checks": settings.get("hardware_checks", []),
        "usb_vidpids": settings.get("usb_vidpids", "[]"),
        "usb_missing_timeout": settings.get("usb_missing_timeout", "60"),
        "vm_image_1_template_id": settings.get("vm_image_1_template_id", settings.get("usb_linux_template_id", settings.get("usb_template_id", "100"))),
        "vm_image_1_template_spec": settings.get("vm_image_1_template_spec", _resolved_template_spec(settings, 1)),
        "vm_image_2_template_id": settings.get("vm_image_2_template_id", settings.get("usb_windows_template_id", "200")),
        "vm_image_2_template_spec": settings.get("vm_image_2_template_spec", _resolved_template_spec(settings, 2)),
        "vm_image_1_pct": settings.get("vm_image_1_pct", "50"),
        "usb_auto_provision": settings.get("usb_auto_provision", "off"),
        "use_all_dongles": _setting_bool("use_all_dongles", False),
        "usb_max_slots": settings.get("usb_max_slots", "24"),
        "cpu_provision_threshold": settings.get("cpu_provision_threshold", "80"),
        "cpu_delete_threshold": settings.get("cpu_delete_threshold", "90"),
        "mem_provision_threshold": settings.get("mem_provision_threshold", "80"),
        "mem_delete_threshold": settings.get("mem_delete_threshold", "90"),
        "vmid_start": int(settings.get("vmid_start", 0) or 0),
        "usb_ignored_vidpids": settings.get("usb_ignored_vidpids", "[]"),
        "ignored_hostnames": settings.get("ignored_hostnames", '["sim-rpi-0000"]'),
        "vm_silent_timeout": settings.get("vm_silent_timeout", "24"),
        "reclone_schedule_enabled": settings.get("reclone_schedule_enabled", "off"),
        "reclone_schedule_cron": settings.get("reclone_schedule_cron", "sunday 02:00"),
        "reclone_concurrency": settings.get("reclone_concurrency", "1"),
        "protected_vmids": settings.get("protected_vmids", ""),
        "l1_vlan_start": settings.get("l1_vlan_start", "100"),
        "l1_vlan_end": settings.get("l1_vlan_end", "199"),
        "guest_agent_watchdog_enabled": settings.get("guest_agent_watchdog_enabled", "on"),
        "watchdog_reboot_enabled": settings.get("watchdog_reboot_enabled", "on"),
        "guest_agent_grace_minutes": settings.get("guest_agent_grace_minutes", "20"),
        "guest_agent_check_interval_minutes": settings.get("guest_agent_check_interval_minutes", "10"),
        "guest_agent_reboot_after_minutes": settings.get("guest_agent_reboot_after_minutes", "10"),
        "guest_agent_reclone_after_minutes": settings.get("guest_agent_reclone_after_minutes", "30"),
        "notifications": _public_notification_settings(),
        "relay_enabled": settings.get("relay_enabled", "off"),
        "relay_server_url": settings.get("relay_server_url", ""),
        "hub_tls_verify": settings.get("hub_tls_verify", "off"),
        "relay_spoke_name": settings.get("relay_spoke_name", ""),
        "relay_tenant_hint": settings.get("relay_tenant_hint", settings.get("relay_tenant_id", "")),
        "relay_spoke_id": settings.get("relay_spoke_id", ""),
        "relay_tenant_id": settings.get("relay_tenant_id", settings.get("relay_tenant_hint", "")),
        "relay_poll_interval": settings.get("relay_poll_interval", RELAY_INTERVAL_DEFAULT),
        "relay_api_key_configured": bool(settings.get("relay_api_key")),
        "admin_password_configured": bool(_admin_password()),
        "auth_provider": _normalize_spoke_auth_provider(settings.get("auth_provider", "local")),
        "auth_ldap_url": settings.get("auth_ldap_url", ""),
        "auth_ldap_bind_dn": settings.get("auth_ldap_bind_dn", ""),
        "auth_ldap_bind_password_configured": bool(settings.get("auth_ldap_bind_password")),
        "auth_ldap_user_base": settings.get("auth_ldap_user_base", ""),
        "auth_ldap_user_filter": settings.get("auth_ldap_user_filter", "(&(objectClass=user)(sAMAccountName={username}))"),
        "auth_ldap_group_admin": settings.get("auth_ldap_group_admin", ""),
        "auth_ldap_group_viewer": settings.get("auth_ldap_group_viewer", ""),
        "auth_radius_host": settings.get("auth_radius_host", ""),
        "auth_radius_port": int(settings.get("auth_radius_port", 1812)),
        "auth_radius_secret_configured": bool(settings.get("auth_radius_secret")),
        "auth_radius_role_attr": settings.get("auth_radius_role_attr", "Filter-Id"),
        "auth_radius_admin_val": settings.get("auth_radius_admin_val", "admin"),
        "auth_tacacs_host": settings.get("auth_tacacs_host", ""),
        "auth_tacacs_port": int(settings.get("auth_tacacs_port", 49)),
        "auth_tacacs_secret_configured": bool(settings.get("auth_tacacs_secret")),
        "auth_tacacs_admin_priv": int(settings.get("auth_tacacs_admin_priv", 15)),
        "spoke_tls": settings.get("spoke_tls", "off"),
        "proxmox_api_token_configured": bool(settings.get("proxmox_api_token", "").strip()),
        "proxmox_tokens_configured": {
            hn: bool(str(tok or "").strip())
            for hn, tok in (settings.get("proxmox_tokens") or {}).items()
        },
        "proxmox_config": copy.deepcopy(settings.get("proxmox_config") or {}),
    }
    _settings_cache_time = now
    return copy.deepcopy(_settings_cache)



def _public_settings() -> dict[str, Any]:
    payload = _get_cached_settings()
    payload["central_config"]["access_token_configured"] = bool(
        settings["central_config"].get("access_token") or central_token.get("access_token")
    )
    payload["central_config"]["refresh_token_configured"] = bool(
        settings["central_config"].get("refresh_token") or central_token.get("refresh_token")
    )
    payload["admin_password_configured"] = bool(_admin_password())
    payload["proxmox_config"] = copy.deepcopy(settings.get("proxmox_config") or {})
    return payload


def _public_central_api_settings() -> dict[str, Any]:
    cfg = copy.deepcopy(settings.get("central_api", _default_central_api_settings()))
    cfg.setdefault("classic", {})
    cfg.setdefault("central", {})
    cfg["classic"].pop("password", None)
    cfg["central"].pop("client_secret", None)
    cfg["classic"]["password_configured"] = bool(settings.get("central_api", {}).get("classic", {}).get("password"))
    cfg["central"]["client_secret_configured"] = bool(settings.get("central_api", {}).get("central", {}).get("client_secret"))
    return cfg



def _public_notification_settings() -> dict[str, Any]:
    notif = copy.deepcopy(settings.get("notifications", {}))
    smtp_password = str(notif.pop("smtp_password", "") or "")
    teams_webhook_url = str(notif.pop("teams_webhook_url", "") or "")
    notif["smtp_password_configured"] = bool(smtp_password)
    notif["teams_webhook_url_configured"] = bool(teams_webhook_url)
    return notif



def _public_acme_settings(cfg: Any) -> dict[str, Any]:
    data = asdict(cfg)
    credentials = dict(cfg.dns_credentials or {})
    data["dns_credentials"] = {key: "" for key in credentials}
    data["dns_credentials_configured"] = {key: bool(value) for key, value in credentials.items()}
    data["cf_api_token_set"] = bool(credentials.get("cf_api_token"))
    data["he_ddns_key_set"] = bool(credentials.get("he_ddns_key"))
    return data


def _sync_central_runtime_config() -> None:
    settings["central_config"] = _build_runtime_central_config(settings.get("central_api", _default_central_api_settings()), settings.get("central_config", {}))
    _reset_central_runtime_tokens()


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
central_status: dict[str, dict[str, Any]] = {}
central_wireless_clients: dict[str, int] = {}   # wsite → client count from Central API
# wsite → list of (timestamp_float, client_count_int) samples (rolling 60 min)
_client_count_samples: dict[str, list[tuple[float, int]]] = {}
CLIENT_COUNT_WINDOW = 3600   # seconds of history to keep
CLIENT_COUNT_MIN_SAMPLES = 3  # minimum samples before flagging
CLIENT_COUNT_DROP_PCT = 25.0  # percent drop that triggers alert
central_history: list[dict[str, Any]] = []   # in-memory 24-h window
central_auth_error: str | None = None          # last auth/token failure message
history_lock = asyncio.Lock()
# Browse data for distributed mode — populated each poll cycle (new_central only)
central_browse_alerts: list[dict[str, Any]] = []
central_browse_insights: list[dict[str, Any]] = []
central_browse_devices_by_site: dict[str, list[dict[str, Any]]] = {}
central_browse_clients_by_site: dict[str, dict[str, Any]] = {}
central_browse_clients: list[dict[str, Any]] = []  # individual client records
# Server-side cache for /api/central/browse — avoids hammering Central API on every tab open.
_central_browse_response_cache: dict[str, Any] = {}
_central_browse_response_cached_at: float = 0.0
_central_browse_fetching: bool = False  # lock to prevent concurrent on-demand fetches
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
hardware_alert_devices: dict[str, dict[str, list[str]]] = {}

# In hub-connected (centralized) mode the hub computes hardware_alerts and pushes the
# full pre-built list (id/name/device_type/total/sites) to the spoke.  We cache it
# here so _hw_alerts_payload() can return it when local settings["hardware_checks"] is
# empty (i.e. the spoke has no locally-configured checks).
_hub_fed_hardware_alerts: list[dict] = []

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
def _history_cutoff() -> float:
    return time.time() - HISTORY_HOURS * 3600


def _load_history() -> list[dict[str, Any]]:
    """Load last 24 h from the JSONL file into memory."""
    if not HISTORY_FILE.exists():
        return []
    cutoff = _history_cutoff()
    result: list[dict[str, Any]] = []
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("ts", 0) >= cutoff:
                    result.append(record)
            except json.JSONDecodeError:
                pass
    except Exception as exc:
        logger.warning("Could not read history file: %s", exc)
    return result


def _append_and_trim_history(new_records: list[dict[str, Any]]) -> None:
    """Append new records to the JSONL file and remove lines older than 24 h."""
    cutoff = _history_cutoff()
    existing: list[str] = []
    if HISTORY_FILE.exists():
        try:
            for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ts", 0) >= cutoff:
                        existing.append(line)
                except json.JSONDecodeError:
                    pass
        except Exception as exc:
            logger.warning("Could not read history file for trimming: %s", exc)

    for record in new_records:
        existing.append(json.dumps(record))

    try:
        HISTORY_FILE.write_text("\n".join(existing) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write history file: %s", exc)


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



def _central_cfg() -> dict[str, str]:
    return settings.get("central_config", {})


def _is_new_central_api() -> bool:
    return _central_cfg().get("api_version") == "new_central"


def _central_ready() -> bool:
    """Minimum config needed to make API calls."""
    cfg = _central_cfg()
    if not cfg.get("cluster_url"):
        return False
    if _is_new_central_api():
        # New Central: need client_id + client_secret to auto-fetch tokens
        return bool(cfg.get("client_id") and cfg.get("client_secret"))
    # Classic: need a token already loaded or stored
    return bool(cfg.get("access_token") or central_token.get("access_token"))


def _central_token_state() -> dict[str, str]:
    """Return {state, detail} describing the current Central API auth status.

    States: not_configured | auth_failed | token_expired | connected
    """
    cfg = _central_cfg()
    if not cfg.get("cluster_url"):
        return {"state": "not_configured", "detail": "No cluster URL — configure in Setup tab"}
    if _is_new_central_api():
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return {"state": "not_configured", "detail": "Client ID / Client Secret required for Central mode"}
    else:
        if not cfg.get("access_token") and not central_token.get("access_token"):
            if settings.get("central_api", {}).get("mode") == "classic":
                return {"state": "not_configured", "detail": "Classic mode is saved separately — use Test Connection in Setup to validate credentials."}
            return {"state": "not_configured", "detail": "No access token — configure in Setup tab"}
    tok = central_token.get("access_token")
    if not tok:
        err = central_auth_error or "Authentication not yet attempted"
        return {"state": "auth_failed", "detail": err}
    if time.time() >= central_token.get("expires_at", 0):
        if central_auth_error:
            return {"state": "auth_failed", "detail": central_auth_error}
        if _can_refresh():
            return {"state": "token_expired", "detail": "Token has expired — will refresh on next poll"}
        return {"state": "token_expired", "detail": "Token has expired — re-enter a valid token in Setup tab"}
    return {"state": "connected", "detail": "Token valid"}


async def _apply_central_feed(feed: dict) -> None:
    """Apply hub-provided Central data feed to local in-memory state (centralized mode)."""
    global central_status, central_wireless_clients, hardware_alert_devices, _hub_fed_hardware_alerts
    new_status = feed.get("status") or {}
    new_wireless = feed.get("wireless_clients") or {}
    new_total = feed.get("total_clients") or {}
    token_valid = bool(feed.get("token_valid", False))
    hardware_alerts = feed.get("hardware_alerts") or []

    central_status.clear()
    for wsite, checks in new_status.items():
        if isinstance(checks, dict):
            central_status[wsite] = {
                cid: dict(v) for cid, v in checks.items() if isinstance(v, dict)
            }
    central_wireless_clients.clear()
    central_wireless_clients.update({w: int(c or 0) for w, c in new_wireless.items()})

    # In centralized mode the hub polls Central and pushes total_clients (wired + wireless).
    # Populate _client_count_samples so the Sites health tab works the same way
    # it does in distributed mode (where _poll_central_once fills the samples).
    # Prefer total_clients; fall back to wireless_clients for older hub versions.
    counts_for_health = new_total or new_wireless
    if counts_for_health:
        now_cc = time.time()
        cutoff_cc = now_cc - CLIENT_COUNT_WINDOW
        for _wsite, _count in counts_for_health.items():
            _val = int(_count or 0)
            existing = _client_count_samples.get(_wsite)
            if not existing:
                # First-time feed for this site: seed CLIENT_COUNT_MIN_SAMPLES synthetic
                # backdated samples so the UI can show status immediately rather than
                # staying in "Collecting" until 3 real polls have accumulated.
                _client_count_samples[_wsite] = [
                    (now_cc - (CLIENT_COUNT_MIN_SAMPLES - i) * 60, _val)
                    for i in range(CLIENT_COUNT_MIN_SAMPLES)
                ]
            else:
                _client_count_samples[_wsite].append((now_cc, _val))
                _client_count_samples[_wsite] = [
                    s for s in _client_count_samples[_wsite] if s[0] >= cutoff_cc
                ]
        _save_client_count_baseline()

    hardware_alert_devices = {}
    for alert in hardware_alerts:
        if not isinstance(alert, dict):
            continue
        check_id = str(alert.get("id") or "").strip()
        if not check_id:
            continue
        sites = alert.get("sites") or {}
        site_devices: dict[str, list[str]] = {}
        for wsite, info in sites.items():
            if not isinstance(info, dict):
                continue
            devices = [str(device).strip() for device in info.get("devices") or [] if str(device).strip()]
            if devices:
                site_devices[str(wsite)] = devices
        hardware_alert_devices[check_id] = site_devices

    # Cache the full pre-built hardware_alerts list from the hub feed.  This is used
    # by _hw_alerts_payload() when the spoke has no locally-configured hardware_checks
    # (i.e. in hub-connected / centralized mode).  The hub already includes id, name,
    # device_type, total, and sites — exactly the shape _hw_alerts_payload() produces.
    _hub_fed_hardware_alerts = [a for a in hardware_alerts if isinstance(a, dict) and a.get("id")]

    # Update token state so spoke Central tab shows connected status
    if token_valid:
        central_token.setdefault("access_token", "_hub_managed_")
        central_token["expires_at"] = time.time() + 3600
    else:
        central_token["access_token"] = None
        central_token["expires_at"] = 0.0

    # Apply browse data pushed by the hub (centralized mode only).
    # The hub filters the browse cache to only this spoke's assigned sites before pushing.
    global central_browse_alerts, central_browse_insights, central_browse_devices_by_site, central_browse_clients_by_site, central_browse_clients
    browse_alerts = feed.get("central_browse_alerts")
    browse_insights = feed.get("central_browse_insights")
    browse_clients_by_site = feed.get("central_browse_clients_by_site")
    browse_clients = feed.get("central_browse_clients")  # individual records
    browse_devices = feed.get("central_browse_devices_by_site")
    browse_changed = False
    if isinstance(browse_alerts, list):
        central_browse_alerts = browse_alerts
        browse_changed = True
    if isinstance(browse_insights, list):
        central_browse_insights = browse_insights
        browse_changed = True
    if isinstance(browse_clients_by_site, dict):
        central_browse_clients_by_site = browse_clients_by_site
        browse_changed = True
    if isinstance(browse_clients, list):
        central_browse_clients = browse_clients
        browse_changed = True
    if isinstance(browse_devices, dict):
        central_browse_devices_by_site = browse_devices
        browse_changed = True
    if browse_changed:
        # Invalidate the server-side browse response cache so the next API call
        # assembles fresh data from the hub-pushed browse globals.
        global _central_browse_response_cache, _central_browse_response_cached_at
        _central_browse_response_cache = {}
        _central_browse_response_cached_at = 0.0

    await broadcast({
        "type": "central_update",
        "status": _central_status_payload(),
        "wireless_clients": dict(central_wireless_clients),
        "hardware_alerts": _hw_alerts_payload(),
        "client_count_status": _client_count_payload(),
        "ts": time.time(),
        "token_state": _central_token_state(),
    })


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


async def _fetch_new_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Obtain a token for New Central via HPE GreenLake client_credentials grant."""
    cfg = _central_cfg()
    try:
        resp = await client.post(
            _NEW_CENTRAL_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not resp.is_success:
            return False, f"Token request failed (HTTP {resp.status_code}): {resp.text[:300]}"
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            return False, f"No access_token in GLP response: {resp.text[:300]}"
        expires_in = payload.get("expires_in", 7200)
        central_token["access_token"] = token
        central_token["refresh_token"] = None
        central_token["expires_at"] = time.time() + expires_in - 60
        logger.info("New Central token obtained via client_credentials (expires in %ss)", expires_in)
        return True, "Token obtained via client_credentials."
    except Exception as exc:
        return False, f"GLP token request error: {exc}"


async def _fetch_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Load/obtain the access token and verify it against a probe endpoint.

    For New Central: obtains a token via GLP client_credentials grant.
    For Classic: loads the user-pasted token from settings and probes the API.
    Returns (success, detail_message).
    """
    global central_auth_error
    if _is_new_central_api():
        ok, msg = await _fetch_new_central_token(client)
        if not ok:
            central_auth_error = msg
            return False, msg
        # Probe to confirm the token works against the base URL
        ok, msg = await _probe_central_token(client)
        central_auth_error = None if ok else msg
        return ok, msg

    cfg = _central_cfg()
    token = cfg.get("access_token", "").strip()
    if not token:
        return False, "No access token configured."

    central_token["access_token"] = token
    if cfg.get("refresh_token"):
        central_token["refresh_token"] = cfg["refresh_token"]
    central_token["expires_at"] = time.time() + 7200

    ok, msg = await _probe_central_token(client)
    central_auth_error = None if ok else msg
    return ok, msg


async def _probe_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Probe the Central API to confirm the in-memory token is accepted."""
    cfg = _central_cfg()
    base_url = cfg["cluster_url"].rstrip("/")
    token = central_token.get("access_token", "")
    headers = {"Authorization": f"Bearer {token}"}

    if _is_new_central_api():
        # New Central v1alpha1 — sites-health is the lightest reliable endpoint
        probe_urls = [
            (f"{base_url}/network-monitoring/v1alpha1/sites-health", {}),
            (f"{base_url}/network-monitoring/v1alpha1/devices", {"limit": 1}),
        ]
    else:
        # Classic Central
        probe_urls = [
            (f"{base_url}/configuration/v2/groups", {"limit": 1, "offset": 0}),
            (f"{base_url}/monitoring/v1/alerts", {"limit": 1}),
            (f"{base_url}/monitoring/v2/alerts", {"limit": 1}),
            (f"{base_url}/platform/v1/customer_id", {}),
        ]
    last_status: int = 0
    last_body: str = ""
    for url, params in probe_urls:
        try:
            logger.info("Central probe → GET %s params=%s", url, params)
            resp = await client.get(url, headers=headers, params=params, timeout=15)
            last_status = resp.status_code
            last_body = resp.text[:400]
            logger.info("Central probe ← %s: %s", resp.status_code, last_body[:200])
            if resp.status_code == 200:
                logger.info("Aruba Central token validated via %s", url)
                return True, "Token validated successfully."
            if resp.status_code == 401:
                # Token is definitely invalid — try refresh before giving up
                central_token["access_token"] = None
                ok, msg = await _refresh_central_token(client)
                if ok:
                    return True, f"Access token was expired; successfully refreshed. {msg}"
                return False, f"Token rejected (401). Central response: {last_body}"
            if resp.status_code == 400:
                # 400 means the endpoint exists and accepted our token but wants different params.
                # That's enough to confirm the token is valid.
                logger.info("Central token confirmed via %s (400 = endpoint live, token accepted)", url)
                return True, "Token validated successfully (endpoint reachable, token accepted)."
            # 403/404 = wrong scope or endpoint missing — try next probe
            logger.info("Central probe %s returned %s — trying next", url, resp.status_code)
        except Exception as exc:
            return False, f"Connection error reaching {base_url}: {exc}"

    return False, (
        f"Could not confirm token with Central (last HTTP status: {last_status}). "
        f"Response: {last_body}. "
        "Check the Cluster URL and that the token has monitoring or configuration scope."
    )


async def _refresh_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Refresh/renew the access token.

    New Central: re-requests via GLP client_credentials (no refresh token).
    Classic: uses refresh_token grant against Central's OAuth endpoint.
    Returns (success, detail_message).
    """
    if not _can_refresh():
        return False, "Cannot refresh: missing client_id or client_secret."
    if _is_new_central_api():
        return await _fetch_new_central_token(client)
    cfg = _central_cfg()
    token_url = cfg["cluster_url"].rstrip("/") + "/oauth2/token"
    refresh_tok = cfg.get("refresh_token") or central_token.get("refresh_token", "")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": refresh_tok,
    }
    if cfg.get("customer_id"):
        data["customer_id"] = cfg["customer_id"]
    try:
        resp = await client.post(token_url, data=data, timeout=15)
        if not resp.is_success:
            return False, f"Refresh failed (HTTP {resp.status_code}): {resp.text[:300]}"
        payload = _parse_upstream_json(resp)
        new_access = payload["access_token"]
        new_refresh = payload.get("refresh_token", refresh_tok)
        central_token["access_token"] = new_access
        central_token["refresh_token"] = new_refresh
        central_token["expires_at"] = time.time() + payload.get("expires_in", 7200) - 60
        settings["central_config"]["access_token"] = new_access
        settings["central_config"]["refresh_token"] = new_refresh
        _save_settings()
        logger.info("Aruba Central token refreshed successfully")
        return True, "Token refreshed successfully."
    except UpstreamJSONError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Refresh request failed: {exc}"


async def _test_classic_central_connection(client: httpx.AsyncClient) -> tuple[bool, str]:
    classic_cfg = settings.get("central_api", {}).get("classic", {})
    base_url = str(classic_cfg.get("url", "")).strip().rstrip("/")
    username = str(classic_cfg.get("username", "")).strip()
    password = str(classic_cfg.get("password", ""))
    if not base_url or not username or not password:
        return False, "Central API not configured — enter URL, Username, and Password in Setup."
    try:
        resp = await client.get(base_url, auth=(username, password), timeout=15, follow_redirects=True)
    except Exception as exc:
        return False, f"Connection error reaching {base_url}: {exc}"
    if resp.status_code in (401, 403):
        return False, f"Classic credentials rejected (HTTP {resp.status_code})."
    if resp.status_code >= 500:
        return False, f"Classic endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
    return True, "Connected to Classic API successfully."


def _central_headers() -> dict[str, str]:
    token = central_token.get("access_token")
    if not token:
        raise HTTPException(status_code=503, detail="Aruba Central token not available — check connection settings")
    return {"Authorization": f"Bearer {token}"}


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


async def central_token_manager() -> None:
    """Background task: keep token valid. Runs every 5 minutes."""
    global central_auth_error
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if _central_ready():
                    no_token = not central_token.get("access_token")
                    expiring = time.time() >= central_token.get("expires_at", 0) - 300
                    if no_token:
                        ok, msg = await _fetch_central_token(client)
                        if not ok:
                            logger.warning("Central token load failed: %s", msg)
                        # Broadcast updated token state regardless of success
                        await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()})
                    elif expiring and _can_refresh():
                        ok, msg = await _refresh_central_token(client)
                        if not ok:
                            logger.warning("Central token refresh failed: %s", msg)
                            central_auth_error = f"Token refresh failed: {msg}"
                            central_token["access_token"] = None  # force re-fetch next cycle
                        else:
                            central_auth_error = None
                        await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()})
                _update_service_health("central_token", ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _update_service_health("central_token", ok=False, error=str(exc))
                logger.exception("Central token manager error: %s", exc)
            await asyncio.sleep(300)


# ── Aruba Central poll loop ───────────────────────────────────────────────────

async def _fetch_nc_browse_for_spoke(client: httpx.AsyncClient) -> None:
    """Fetch new_central browse data (alerts, insights, devices, clients) filtered to
    this spoke's assigned sites.  Results are stored in the module-level
    central_browse_* variables so they can be included in the telemetry sent to hub."""
    global central_browse_alerts, central_browse_insights, central_browse_devices_by_site, central_browse_clients_by_site, central_browse_clients

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

    central_browse_alerts = new_alerts
    central_browse_insights = new_insights
    central_browse_devices_by_site = new_devices_by_site
    central_browse_clients_by_site = new_clients_by_site
    central_browse_clients = new_clients
    # Invalidate the server-side browse response cache so the next API call
    # returns fresh data assembled from these updated globals.
    global _central_browse_response_cache, _central_browse_response_cached_at
    _central_browse_response_cache = {}
    _central_browse_response_cached_at = 0.0
    logger.info("NC browse fetch complete: %d alerts, %d insights, %d sites with devices, %d sites with clients (%d individual)",
                len(new_alerts), len(new_insights), len(new_devices_by_site), len(new_clients_by_site), len(new_clients))


async def _poll_central_once(client: httpx.AsyncClient) -> None:
    """Single poll cycle: fetch alerts + insights per mapped site, evaluate checks."""
    if not _central_ready() or not central_token.get("access_token"):
        return

    site_mappings: dict[str, str] = settings.get("site_mappings", {})
    monitored: list[dict[str, Any]] = settings.get("monitored_checks", [])
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    if not site_mappings or (not monitored and not hw_checks):
        return

    hw_check_ids: set[str] = {c["id"] for c in hw_checks}

    cfg = _central_cfg()
    base_url = cfg["cluster_url"].rstrip("/")
    headers = _central_headers()
    now = time.time()
    new_records: list[dict[str, Any]] = []

    # Accumulate hardware alert devices across all sites this cycle
    new_hw_devices: dict[str, dict[str, list[str]]] = {c["id"]: {} for c in hw_checks}

    for wsite, central_site in site_mappings.items():
        site_check_status: dict[str, Any] = {}

        # ── Fetch alerts for this site ────────────────────────────
        alert_type_counts: dict[str, int] = {}
        site_health: dict[str, Any] = {}

        if _is_new_central_api():
            # New Central v1alpha1: derive synthetic alert_type_counts from available endpoints.
            # Fetch sites-health, devices, and clients in parallel for the mapped site.

            # ── sites-health ──────────────────────────────────────────────
            site_id: str | None = None
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code == 401 and _can_refresh():
                    ok, _ = await _refresh_central_token(client)
                    if ok:
                        headers = _central_headers()
                    resp = await client.get(
                        f"{base_url}/network-monitoring/v1alpha1/sites-health",
                        headers=headers,
                        timeout=20,
                    )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == central_site.lower():
                            site_health = item
                            site_id = item.get("siteId") or item.get("site_id")
                            score = item.get("healthScore", item.get("health_score", 100))
                            ap_count = item.get("apCount", item.get("ap_count", 0))
                            alert_type_counts["SITE_HEALTH"] = int(score)
                            alert_type_counts["AP_COUNT"] = int(ap_count)
                            break
            except Exception as exc:
                logger.warning("New Central sites-health fetch failed for site %s: %s", central_site, exc)

            # ── devices (AP_DOWN, SWITCH_DOWN, GATEWAY_DOWN) ──────────────
            try:
                params: dict[str, Any] = {"limit": 500}
                if site_id:
                    params["filter"] = f"siteId eq '{site_id}'"
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=20,
                )
                if resp.status_code == 200:
                    ap_down = switch_down = gw_down = 0
                    for dev in resp.json().get("items", []):
                        dtype = (dev.get("deviceType") or "").upper()
                        status = (dev.get("status") or "").upper()
                        is_down = status not in ("UP", "ONLINE")
                        if dtype == "ACCESS_POINT" and is_down:
                            ap_down += 1
                        elif dtype == "SWITCH" and is_down:
                            switch_down += 1
                        elif dtype == "GATEWAY" and is_down:
                            gw_down += 1
                    alert_type_counts["AP_DOWN"] = ap_down
                    alert_type_counts["SWITCH_DOWN"] = switch_down
                    alert_type_counts["GATEWAY_DOWN"] = gw_down
            except Exception as exc:
                logger.warning("New Central devices fetch failed for site %s: %s", central_site, exc)

            # ── clients (CLIENT_COUNT) ─────────────────────────────────────
            try:
                cparams: dict[str, Any] = {}
                if site_id:
                    cparams["site-id"] = site_id
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/clients",
                    headers=headers, params=cparams, timeout=20,
                )
                if resp.status_code == 200:
                    alert_type_counts["CLIENT_COUNT"] = int(resp.json().get("count", 0))
            except Exception as exc:
                logger.warning("New Central clients fetch failed for site %s: %s", central_site, exc)

            # New Central: no insights endpoint — skip
            insight_cat_counts: dict[str, int] = {}
        else:
            for alerts_path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
                try:
                    resp = await client.get(
                        f"{base_url}{alerts_path}",
                        headers=headers,
                        params={"site": central_site, "limit": 1000},
                        timeout=20,
                    )
                    if resp.status_code == 401 and _can_refresh():
                        ok, _ = await _refresh_central_token(client)
                        if ok:
                            headers = _central_headers()
                        resp = await client.get(
                            f"{base_url}{alerts_path}",
                            headers=headers,
                            params={"site": central_site, "limit": 1000},
                            timeout=20,
                        )
                    if resp.status_code == 200:
                        data = resp.json()
                        for alert in data.get("alerts", []):
                            atype = alert.get("alert_type") or alert.get("type", "")
                            if atype:
                                alert_type_counts[atype] = alert_type_counts.get(atype, 0) + 1
                                # Collect device names for hardware checks
                                if atype in hw_check_ids:
                                    dev = (alert.get("device_name") or alert.get("hostname")
                                           or alert.get("name") or "").strip()
                                    if dev:
                                        new_hw_devices.setdefault(atype, {}).setdefault(wsite, [])
                                        if dev not in new_hw_devices[atype][wsite]:
                                            new_hw_devices[atype][wsite].append(dev)
                        break
                    if resp.status_code == 404:
                        continue
                except Exception as exc:
                    logger.warning("Central alerts fetch failed for site %s: %s", central_site, exc)
                    break

            # ── Fetch insights for this site ──────────────────────────
            insight_cat_counts: dict[str, int] = {}
            try:
                resp = await client.get(
                    f"{base_url}/aiops/v1/insights",
                    headers=headers,
                    params={"site_name": central_site, "limit": 1000},
                    timeout=20,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for insight in data.get("insights", []):
                        cat = insight.get("category") or insight.get("type", "")
                        if cat:
                            insight_cat_counts[cat] = insight_cat_counts.get(cat, 0) + 1
            except Exception as exc:
                logger.warning("Central insights fetch failed for site %s: %s", central_site, exc)

        # ── Evaluate each monitored check ─────────────────────────
        for check in monitored:
            check_type = check.get("type", "")
            check_id = check.get("id", "")
            check_name = check.get("name", check_id)
            if not check_id:
                continue

            if check_type == "alert":
                count = alert_type_counts.get(check_id, 0)
            elif check_type == "insight":
                count = insight_cat_counts.get(check_id, 0)
            else:
                continue

            status = "OK" if count > 0 else "ERROR"
            site_check_status[check_id] = {
                "status": status,
                "count": count,
                "check_name": check_name,
                "check_type": check_type,
                "ts": now,
            }
            new_records.append({
                "ts": now,
                "wsite": wsite,
                "central_site": central_site,
                "check_type": check_type,
                "check_id": check_id,
                "check_name": check_name,
                "status": status,
                "count": count,
            })

        central_status[wsite] = site_check_status

        # ── Fetch wireless client count for this site from Central ─
        wl_count = 0
        try:
            if _is_new_central_api():
                # New API: client count lives in site_health payload
                wl_count = int(
                    site_health.get("clientCount")
                    or site_health.get("client_count")
                    or 0
                )
            else:
                # Classic API: query wireless clients with site filter.
                # Try both "site" and "site_name" — Central uses each in
                # different API versions.
                fetched = False
                for clients_path in ["/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"]:
                    for site_param in ["site", "site_name"]:
                        resp = await client.get(
                            f"{base_url}{clients_path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1},
                            timeout=20,
                        )
                        if resp.status_code == 401 and _can_refresh():
                            ok, _ = await _refresh_central_token(client)
                            if ok:
                                headers = _central_headers()
                            resp = await client.get(
                                f"{base_url}{clients_path}",
                                headers=headers,
                                params={site_param: central_site, "limit": 1},
                                timeout=20,
                            )
                        logger.info(
                            "Central wireless clients %s ?%s=%s → %s body=%s",
                            clients_path, site_param, central_site,
                            resp.status_code, resp.text[:200],
                        )
                        if resp.status_code == 200:
                            body = resp.json()
                            wl_count = int(body.get("total") or body.get("count") or 0)
                            fetched = True
                            break
                        if resp.status_code == 404:
                            continue
                    if fetched:
                        break
        except Exception as exc:
            logger.warning("Central wireless client count fetch failed for site %s: %s", central_site, exc)
        central_wireless_clients[wsite] = wl_count

        _client_count_samples.setdefault(wsite, []).append((now, wl_count))
        cutoff_cc = now - CLIENT_COUNT_WINDOW
        _client_count_samples[wsite] = [
            s for s in _client_count_samples[wsite] if s[0] >= cutoff_cc
        ]

    # ── Commit hardware alert devices + detect transitions ────────
    global hardware_alert_devices
    hardware_alert_devices = new_hw_devices
    await _check_transitions_and_notify(now)

    # ── Persist client count baseline ─────────────────────────────
    _save_client_count_baseline()

    # ── Persist history ───────────────────────────────────────────
    if new_records:
        cutoff = _history_cutoff()
        async with history_lock:
            central_history[:] = [r for r in central_history if r["ts"] >= cutoff]
            central_history.extend(new_records)
        await asyncio.to_thread(_append_and_trim_history, new_records)

    await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": now, "token_state": _central_token_state()})
    _save_state_cache()
    # In distributed mode with new_central, also fetch browse data (alerts, insights,
    # devices, clients) filtered to this spoke's assigned sites so the hub can assemble
    # a complete multi-site view.
    if _is_new_central_api():
        try:
            await _fetch_nc_browse_for_spoke(client)
        except Exception as exc:
            logger.warning("NC browse fetch failed: %s", exc)


def _central_status_payload() -> dict[str, Any]:
    """Serialize current central_status for WS / API responses."""
    return {
        wsite: {
            check_id: {
                "status": info["status"],
                "count": info["count"],
                "check_name": info["check_name"],
                "check_type": info["check_type"],
                "ts": info["ts"],
            }
            for check_id, info in checks.items()
        }
        for wsite, checks in central_status.items()
    }


def _hw_alerts_payload() -> list[dict[str, Any]]:
    """Serialize hardware_alert_devices merged with check metadata for broadcast.

    In distributed mode (spoke has its own Central credentials) the spoke builds
    hardware_alert_devices from its own polling and this function assembles the
    payload from settings["hardware_checks"].

    In hub-connected (centralized) mode the spoke's settings["hardware_checks"] is
    empty — the hub computes the alerts and pushes a pre-built list via the feed,
    stored in _hub_fed_hardware_alerts.  Fall back to that list so the simulation
    view can display gateway/AP/switch status correctly.
    """
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    if not hw_checks:
        # Hub-connected mode: return the pre-built list pushed by the hub
        return list(_hub_fed_hardware_alerts)
    site_mappings: dict[str, str] = settings.get("site_mappings", {})
    result = []
    for check in hw_checks:
        cid = check["id"]
        devices_by_wsite = hardware_alert_devices.get(cid, {})
        total = sum(len(devs) for devs in devices_by_wsite.values())
        sites_out = {}
        for wsite, devs in devices_by_wsite.items():
            sites_out[wsite] = {
                "site_name": site_mappings.get(wsite, wsite),
                "devices": devs,
            }
        result.append({
            "id": cid,
            "name": check.get("name") or _HW_FRIENDLY.get(cid, cid),
            "device_type": check.get("device_type") or _auto_device_type(cid),
            "total": total,
            "sites": sites_out,
        })
    return result


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




def _sim_clients_per_wsite(active_snap: dict[str, Any]) -> dict[str, int]:
    """Count currently-online sim clients grouped by wsite.

    Reads simulation.conf directly so the result is always fresh regardless
    of whether the /api/simulations endpoint has been called.  Falls back to
    _sim_conf_cache when the conf file is unavailable.
    """
    # Build sim_id → wsite from simulation.conf (s0..s9 buckets)
    sim_to_wsite: dict[str, str] = {}
    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    try:
        parser = configparser.ConfigParser()
        parser.read_string(sim_conf_path.read_text(encoding="utf-8"))
        for section in parser.sections():
            if parser.has_option(section, "wsite"):
                wsite_val = parser.get(section, "wsite").strip()
                if wsite_val:
                    sim_to_wsite[section] = wsite_val
    except Exception:
        # Fall back to cached data if conf is unreadable
        for sim_id, info in _sim_conf_cache.get("simulations", {}).items():
            wsite_val = str(info.get("wsite", "")).strip()
            if wsite_val:
                sim_to_wsite[sim_id] = wsite_val

    if not sim_to_wsite:
        return {}

    # Count online clients per wsite
    counts: dict[str, int] = {w: 0 for w in set(sim_to_wsite.values())}
    for client_data in active_snap.values():
        sim_id = client_data.get("simulation_id", "")
        wsite_val = sim_to_wsite.get(sim_id, "")
        if not wsite_val:
            continue
        if compute_online(client_data.get("last_seen", datetime.min.replace(tzinfo=timezone.utc))):
            counts[wsite_val] = counts.get(wsite_val, 0) + 1
    return counts


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


async def _check_transitions_and_notify(now: float) -> None:
    """Detect green→red transitions for sim checks and hardware checks, fire notifications."""
    notif = settings.get("notifications", {})
    transitions: list[dict[str, Any]] = []

    # ── Sim check transitions ─────────────────────────────────────
    for wsite, checks in central_status.items():
        for check_id, info in checks.items():
            key = f"sim:{check_id}:{wsite}"
            new_state = info["status"]  # "OK" or "ERROR"
            old_state = _prev_check_states.get(key)
            _prev_check_states[key] = new_state
            if old_state == "OK" and new_state == "ERROR":
                transitions.append({
                    "type": "sim",
                    "name": info.get("check_name", check_id),
                    "wsite": wsite,
                    "detail": f"Check '{info.get('check_name', check_id)}' turned red at site {wsite}",
                })

    # ── Hardware alert transitions ────────────────────────────────
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    for check in hw_checks:
        cid = check["id"]
        total = sum(len(d) for d in hardware_alert_devices.get(cid, {}).values())
        new_state = "ERROR" if total > 0 else "OK"
        key = f"hw:{cid}"
        old_state = _prev_check_states.get(key)
        _prev_check_states[key] = new_state
        if old_state == "OK" and new_state == "ERROR":
            name = check.get("name") or _HW_FRIENDLY.get(cid, cid)
            transitions.append({
                "type": "hardware",
                "name": name,
                "detail": f"Hardware alert '{name}' is now active ({total} device(s) affected)",
            })

    for wsite, info in _client_count_payload().items():
        key = f"cc:{wsite}"
        new_state = info["status"]
        if new_state == "NO_DATA":
            _prev_check_states[key] = new_state
            continue
        old_state = _prev_check_states.get(key)
        _prev_check_states[key] = new_state
        if old_state == "OK" and new_state == "DEGRADED":
            transitions.append({
                "type": "client_count",
                "name": f"Client count — {info['site_name']}",
                "detail": (
                    f"Client count at {info['site_name']} dropped {info['drop_pct']:.1f}% "
                    f"(current: {info['current']}, avg: {info['hourly_avg']:.1f})"
                ),
            })

    if not transitions:
        return

    # ── Send notifications ────────────────────────────────────────
    for t in transitions:
        logger.warning("ALERT TRANSITION: %s", t["detail"])

    if notif.get("teams_enabled") and notif.get("teams_webhook_url"):
        await _send_teams_notifications(notif["teams_webhook_url"], transitions)

    if notif.get("email_enabled") and notif.get("smtp_host") and notif.get("smtp_to"):
        await asyncio.to_thread(_send_email_notifications, notif, transitions)


async def _send_teams_notifications(webhook_url: str, transitions: list[dict]) -> None:
    """POST an Adaptive Card to a Teams incoming webhook for each transition."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for t in transitions:
                card = {
                    "type": "message",
                    "attachments": [{
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
                                 "text": f"🔴 Client Simulator Alert: {t['name']}"},
                                {"type": "TextBlock", "text": t["detail"], "wrap": True},
                            ],
                        },
                    }],
                }
                resp = await client.post(webhook_url, json=card)
                if resp.status_code not in (200, 202):
                    logger.warning("Teams webhook returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Teams notification failed: %s", exc)


def _send_email_notifications(notif: dict, transitions: list[dict]) -> None:
    """Send SMTP email for each transition (runs in thread pool)."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    to_addrs = notif.get("smtp_to", [])
    if isinstance(to_addrs, str):
        to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]
    if not to_addrs:
        return

    body_lines = ["Client Simulator Alert\n"]
    for t in transitions:
        body_lines.append(f"• {t['detail']}")
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = notif.get("smtp_from", "client-sim@localhost")
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = f"[Client Simulator] {len(transitions)} check(s) turned RED"
    msg.attach(MIMEText(body, "plain"))

    try:
        host = notif.get("smtp_host", "")
        port = int(notif.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if port != 25:
                smtp.starttls()
            user = notif.get("smtp_user", "")
            pwd = notif.get("smtp_password", "")
            if user and pwd:
                smtp.login(user, pwd)
            smtp.sendmail(msg["From"], to_addrs, msg.as_string())
        logger.info("Email notification sent to %s", to_addrs)
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)


async def central_poller() -> None:
    """Background task: poll Central every CENTRAL_POLL_INTERVAL seconds."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if settings.get("hub_aruba_polling_mode") == "centralized":
                    # Polling is delegated to the hub — mark health ok so the
                    # UI doesn't show a stale warning, then sleep until next check.
                    _update_service_health("central_poller", ok=True)
                    await asyncio.sleep(300)
                    continue
                await _poll_central_once(client)
                _update_service_health("central_poller", ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _update_service_health("central_poller", ok=False, error=str(exc))
                logger.exception("Central poll error: %s", exc)
            await asyncio.sleep(CENTRAL_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global central_history
    logger.info("=" * 60)
    logger.info("Client Simulator  v%s  starting up", INSTALLER_VERSION)
    logger.info("=" * 60)
    _debug_event("server_start", f"v{INSTALLER_VERSION} ui:v{APP_VERSION}")
    central_history = await asyncio.to_thread(_load_history)
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
proxmox_ws_connection: WebSocket | None = None
proxmox_ws_hostname: str | None = None
proxmox_ws_disconnect_task: asyncio.Task[Any] | None = None
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
GKILL_SWITCH_URL = "https://raw.githubusercontent.com/solutions-hpe/client-sim/main/kill_switch.txt"
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
relay_registration_refresh_needed = bool(relay_state["enabled"])
# Capped registration diagnostic log — last 50 attempts
_RELAY_DIAG_MAX = 50
relay_diag_log: list[dict[str, Any]] = []
_relay_ws_send_json: Callable[[dict[str, Any]], Awaitable[None]] | None = None
_relay_ws_spoke_id: str | None = None
_shell_sessions: dict[str, dict[str, Any]] = {}
_repo_ver: str | None = None
_proxmox_reseed_in_progress = False

# Hub-synced monitored items — fetched each relay cycle, cached here
_hub_monitored_items: dict[str, Any] = {"items": [], "has_sites": False, "assigned_sites": []}


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
_prev_usb_by_vmid: dict[str, str] = {}
# VMIDs for which a delete command has been queued but not yet confirmed by telemetry.
# Kept as a set so the UI can show "deleting…" immediately instead of the row vanishing.
_pending_delete_vmids: set[int] = set()
# Cooldown: earliest time a new auto-delete may be queued (updated after each
# confirmed deletion so the fleet has time to stabilise before the next one).
_delete_gate_cooldown_until: float = 0.0
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
_cpu_samples: list[tuple[float, float]] = []
_mem_samples: list[tuple[float, float]] = []
_resource_samples_started: float = 0.0  # epoch when first sample was recorded
_RESOURCE_SAMPLE_WINDOW = 3600  # seconds (1 hour)

# ── Proxmox VM simulation tags ────────────────────────────────────────────────
# Tracks which sim tags we last applied per (agent_hostname, vmid) to avoid
# redundant API calls.  Keyed this way because VMIDs can collide across nodes.
_vm_applied_sim_tags: dict[tuple[str, int], frozenset[str]] = {}
_SIM_TAG_PREFIX = "sim-"


def _sanitize_proxmox_tag(name: str) -> str:
    """Normalize a simulation name to a Proxmox-safe tag with sim- prefix."""
    name = re.sub(r'[^a-z0-9]+', '-', str(name).strip().lower()).strip('-')
    tag = f"{_SIM_TAG_PREFIX}{name}" if not name.startswith(_SIM_TAG_PREFIX) else name
    return tag[:64] if name else ""


def _merge_sim_tags(current_tags_str: str, desired_sim_tags: list[str]) -> str:
    """Replace only sim-prefixed tags while preserving any manual Proxmox tags."""
    existing = [t.strip() for t in current_tags_str.split(';') if t.strip()]
    non_sim = [t for t in existing if not t.lower().startswith(_SIM_TAG_PREFIX)]
    merged = non_sim + sorted(set(t for t in desired_sim_tags if t))
    return ';'.join(merged)


def _get_proxmox_token_for_host(hostname: str | None) -> str:
    """Return per-host token if set, falling back to the legacy global token."""
    if hostname:
        per_host = str((settings.get("proxmox_tokens") or {}).get(hostname, "") or "").strip()
        if per_host:
            return per_host
    return str(settings.get("proxmox_api_token", "") or "").strip()


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


def _get_proxmox_host_config(hostname: Any) -> dict[str, Any]:
    proxmox_config = settings.get("proxmox_config") or {}
    if not isinstance(proxmox_config, dict):
        return {}
    normalized = _normalize_proxmox_hostname(hostname)
    if not normalized:
        return {}
    resolved = _resolve_proxmox_agent_hostname(normalized, proxmox_config) or normalized
    data = proxmox_config.get(resolved, {})
    return dict(data) if isinstance(data, dict) else {}


def _save_proxmox_host_config(hostname: str, updates: dict[str, Any]) -> dict[str, Any]:
    proxmox_config = settings.setdefault("proxmox_config", {})
    persisted_config = _persisted.setdefault("proxmox_config", {})
    current = proxmox_config.get(hostname, {})
    if not isinstance(current, dict):
        current = {}
    entry = dict(current)
    if "vm_set_override" in updates:
        vm_set_override = _sanitize_vm_set_override(updates.get("vm_set_override"))
        if vm_set_override:
            entry["vm_set_override"] = vm_set_override
        else:
            entry.pop("vm_set_override", None)
    if entry:
        proxmox_config[hostname] = entry
        persisted_config[hostname] = dict(entry)
    else:
        proxmox_config.pop(hostname, None)
        persisted_config.pop(hostname, None)
    _save_settings()
    return dict(entry)


def _has_any_proxmox_token() -> bool:
    if _get_proxmox_token_for_host(None):
        return True
    return any(str(tok or "").strip() for tok in (settings.get("proxmox_tokens") or {}).values())


def _save_proxmox_token_for_host(hostname: str, token: str) -> None:
    tokens = settings.setdefault("proxmox_tokens", {})
    persisted_tokens = _persisted.setdefault("proxmox_tokens", {})
    tokens[hostname] = token
    persisted_tokens[hostname] = token
    _save_settings()


async def _apply_sim_tags_for_vm(vmid: int, agent_hostname: str, desired_sim_tags: list[str], current_tags_str: str = "") -> None:
    """Call Proxmox REST API to update simulation tags on a single VM."""
    global _vm_applied_sim_tags
    desired_set = frozenset(t for t in desired_sim_tags if t)
    cache_key = (agent_hostname, vmid)
    if _vm_applied_sim_tags.get(cache_key) == desired_set:
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
            _vm_applied_sim_tags[cache_key] = desired_set
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
    global _resource_samples_started
    cpu_pct = node.get("cpu_percent")
    mem_used = node.get("mem_used_kb")
    mem_total = node.get("mem_total_kb")
    cutoff = now - _RESOURCE_SAMPLE_WINDOW
    if cpu_pct is not None:
        if not _resource_samples_started:
            _resource_samples_started = now
        _cpu_samples.append((now, float(cpu_pct)))
        _cpu_samples[:] = [(ts, v) for ts, v in _cpu_samples if ts >= cutoff]
    try:
        if mem_used is not None and mem_total:
            mem_total_f = float(mem_total)
            if mem_total_f > 0:
                if not _resource_samples_started:
                    _resource_samples_started = now
                mem_pct = (float(mem_used) / mem_total_f) * 100.0
                _mem_samples.append((now, mem_pct))
                _mem_samples[:] = [(ts, v) for ts, v in _mem_samples if ts >= cutoff]
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    _save_resource_cache()
_RESOURCE_CACHE_SAVE_INTERVAL = 60.0  # persist at most once per minute
_resource_cache_last_saved: float = 0.0


def _load_resource_cache() -> None:
    """Restore resource samples from disk so restarts don't reset the 1-hour window."""
    global _cpu_samples, _mem_samples, _resource_samples_started
    try:
        if not RESOURCE_CACHE_FILE.exists():
            return
        data = json.loads(RESOURCE_CACHE_FILE.read_text())
        cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
        loaded_cpu = [(float(ts), float(v)) for ts, v in (data.get("cpu_samples") or []) if float(ts) >= cutoff]
        loaded_mem = [(float(ts), float(v)) for ts, v in (data.get("mem_samples") or []) if float(ts) >= cutoff]
        started = float(data.get("started") or 0)
        _cpu_samples = loaded_cpu
        _mem_samples = loaded_mem
        _resource_samples_started = started if started > 0 else 0.0
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
            len(_cpu_samples), len(_mem_samples),
            time.time() - _resource_samples_started if _resource_samples_started else 0,
        )
    except Exception:
        logger.debug("Could not load resource cache from %s", RESOURCE_CACHE_FILE, exc_info=True)


def _save_resource_cache(force: bool = False) -> None:
    """Persist resource samples so the 1-hour window survives service restarts."""
    global _resource_cache_last_saved
    now = time.time()
    if not force and (now - _resource_cache_last_saved) < _RESOURCE_CACHE_SAVE_INTERVAL:
        return
    _resource_cache_last_saved = now
    try:
        _atomic_write_json(RESOURCE_CACHE_FILE, {
            "cpu_samples": _cpu_samples,
            "mem_samples": _mem_samples,
            "started": _resource_samples_started,
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
update_all_state: dict[str, Any] = {
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
last_schedule_trigger: str | None = None
_hub_repo_sync_task: asyncio.Task[Any] | None = None  # dedup guard for fire-and-forget repo_sync

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


def _update_service_health(name: str, *, ok: bool, error: str | None = None) -> None:
    now = iso_utcnow()
    entry = service_health.setdefault(name, {"run_count": 0, "consecutive_errors": 0})
    entry["last_run"] = now
    entry["run_count"] = entry.get("run_count", 0) + 1
    if ok:
        entry["last_success"] = now
        entry["last_error_msg"] = None
        entry["consecutive_errors"] = 0
        entry["status"] = "ok"
    else:
        entry["last_error_msg"] = error or "unknown error"
        entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
        entry["status"] = "error" if entry["consecutive_errors"] >= 3 else "warning"


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


def _normalize_command_action(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalize_command_type(value: Any) -> str | None:
    normalized = str(value or "").strip().replace("-", "_")
    return normalized or None


def _command_args_signature(args: dict[str, Any] | None) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        safe_args = json.loads(json.dumps(args or {}, default=str))
        return json.dumps(safe_args, sort_keys=True, separators=(",", ":"))


def _trim_commands_locked() -> None:
    if len(commands) <= COMMAND_MAX:
        return
    terminal = {"completed", "failed", "expired"}
    idx = 0
    while len(commands) > COMMAND_MAX and idx < len(commands):
        if commands[idx].get("status") in terminal:
            del commands[idx]
            continue
        idx += 1
    if len(commands) > COMMAND_MAX:
        del commands[:len(commands) - COMMAND_MAX]


def _cleanup_commands_locked(now: float | None = None) -> tuple[int, int]:
    now = now or time.time()
    expired = 0
    for cmd in commands:
        if cmd.get("status") in {"pending", "delivered"} and (now - cmd.get("created_at", now)) > COMMAND_EXPIRE_SECS:
            cmd["status"] = "expired"
            cmd["updated_at"] = now
            cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
            expired += 1
            _trace(
                "command_expired",
                cmd_id=cmd.get("id"),
                action=cmd.get("action"),
                target=cmd.get("target"),
                age_secs=round(now - cmd.get("created_at", now)),
            )

    before = len(commands)
    commands[:] = [
        cmd for cmd in commands
        if cmd.get("status") not in {"completed", "failed", "expired"}
        or now < float(cmd.get("purge_after") or cmd.get("updated_at", cmd.get("created_at", now)) + COMMAND_RESULT_RETENTION_SECS)
    ]
    purged = before - len(commands)
    _trim_commands_locked()
    if expired or purged:
        _save_commands()
    return expired, purged


def _find_active_duplicate_command_locked(target: str, action: str, args: dict[str, Any]) -> dict[str, Any] | None:
    args_sig = _command_args_signature(args)
    for cmd in commands:
        if cmd.get("target") != target or cmd.get("action") != action:
            continue
        if cmd.get("status") not in {"pending", "delivered"}:
            continue
        if _command_args_signature(cmd.get("args", {})) == args_sig:
            return cmd
    return None


def _enqueue_command_locked(target: str, action: str, args: dict[str, Any] | None = None, command_type: str | None = None, relay: bool = False) -> tuple[dict[str, Any], bool, int, int]:
    normalized_action = _normalize_command_action(action)
    normalized_type = _normalize_command_type(command_type)
    normalized_args = dict(args or {})
    if target == "proxmox" and normalized_action == "delete_vm":
        # Hub relay commands use lenient validation — inventory may be stale or not yet loaded.
        # The proxmox agent performs the real validation before executing the delete.
        normalized_args = _prepare_delete_vm_args(normalized_args, strict=not relay)

    # Block all single-VM actions on protected VMIDs (start, stop, reboot, snapshot, reclone)
    _VM_ACTIONS = {"start_vm", "stop_vm", "reboot_vm", "snapshot_vm", "reclone_vm", "delete_vm"}
    if target == "proxmox" and normalized_action in _VM_ACTIONS:
        vmid = normalized_args.get("vmid")
        if _is_protected_vmid(vmid):
            raise HTTPException(
                status_code=403,
                detail=f"VM {vmid} is protected and cannot be managed from this UI",
            )

    now = time.time()
    expired, purged = _cleanup_commands_locked(now)
    existing = _find_active_duplicate_command_locked(target, normalized_action, normalized_args)
    if existing is not None:
        return existing, False, expired, purged

    cmd = _make_command(target, normalized_action, normalized_args, command_type=normalized_type)
    commands.append(cmd)
    _trim_commands_locked()
    _save_commands()
    return cmd, True, expired, purged


def _make_command(target: str, action: str, args: dict | None = None, command_type: str | None = None) -> dict[str, Any]:
    now = time.time()
    return {
        "id": str(uuid.uuid4()),
        "target": target,
        "action": _normalize_command_action(action),
        "args": dict(args or {}),
        "type": _normalize_command_type(command_type),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + COMMAND_EXPIRE_SECS,
        "purge_after": None,
        "result": None,
        "message": None,
    }


def _serialize_command_for_agent(command: dict[str, Any]) -> dict[str, Any]:
    return {"id": command["id"], "action": command["action"], "args": command["args"], "type": command.get("type")}


def _command_matches_agent(command: dict[str, Any], hostname: str, approved_hostname: str | None = None) -> bool:
    if command["target"] == hostname:
        return True
    if approved_hostname is None:
        return False
    return (
        _proxmox_hostnames_match(command["target"], approved_hostname)
        or command["target"] == "proxmox"
    )


def _reset_delivered_commands_locked(hostname: str, approved_hostname: str | None = None) -> int:
    """On WS reconnect, reset 'delivered' commands back to 'pending' so they are re-sent.

    Commands that were pushed via WS and marked 'delivered' but never acked (because the
    connection dropped before the agent could process them) would otherwise be silently
    abandoned — _peek_pending_agent_commands_locked only returns 'pending' commands.
    Resetting them to 'pending' ensures they are re-delivered on the next push.
    """
    now = time.time()
    reset = 0
    for cmd in commands:
        if cmd.get("status") == "delivered" and _command_matches_agent(cmd, hostname, approved_hostname):
            cmd["status"] = "pending"
            cmd["updated_at"] = now
            reset += 1
    if reset:
        _save_commands()
    return reset


def _peek_pending_agent_commands_locked(hostname: str, approved_hostname: str | None = None) -> tuple[list[dict[str, Any]], int, int]:
    expired, purged = _cleanup_commands_locked()
    pending = [
        command for command in commands
        if command["status"] == "pending" and _command_matches_agent(command, hostname, approved_hostname)
    ]
    return pending, expired, purged


def _mark_commands_delivered_locked(command_ids: list[str]) -> bool:
    if not command_ids:
        return False
    ids = set(command_ids)
    now = time.time()
    changed = False
    for command in commands:
        if command["id"] in ids and command["status"] == "pending":
            command["status"] = "delivered"
            command["updated_at"] = now
            changed = True
    if changed:
        _save_commands()
    return changed


async def _push_pending_agent_commands(hostname: str, websocket: WebSocket, approved_hostname: str | None = None) -> bool:
    async with state_lock:
        pending, expired, purged = _peek_pending_agent_commands_locked(hostname, approved_hostname)
        payload = [_serialize_command_for_agent(command) for command in pending]
        serialized = _serialize_commands()
    if not payload:
        if expired or purged:
            await broadcast({"type": "commands_update", "commands": serialized})
        return True
    _trace("agent_ws_push", hostname=hostname,
           commands=[{"action": c.get("action"), "args": {k: v for k, v in (c.get("args") or {}).items() if k in {"vmid", "vm_type"}}} for c in payload])
    try:
        await websocket.send_json({"type": "commands", "commands": payload})
    except Exception as exc:
        _trace("agent_ws_push_err", hostname=hostname, error=str(exc))
        return False
    async with state_lock:
        changed = _mark_commands_delivered_locked([command["id"] for command in pending])
        serialized = _serialize_commands()
    if changed or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return True


async def _push_pending_commands_for_target(target: str) -> None:
    normalized = str(target or "").strip()
    if not normalized:
        return
    websocket = client_ws_connections.get(normalized)
    if websocket is not None:
        if not await _push_pending_agent_commands(normalized, websocket):
            client_ws_connections.pop(normalized, None)
    global proxmox_ws_connection, proxmox_ws_hostname
    if proxmox_ws_connection is not None and proxmox_ws_hostname and (
        normalized == "proxmox" or _proxmox_hostnames_match(normalized, proxmox_ws_hostname)
    ):
        if not await _push_pending_agent_commands(proxmox_ws_hostname, proxmox_ws_connection, proxmox_ws_hostname):
            proxmox_ws_connection = None
            proxmox_ws_hostname = None


async def _push_pending_commands_for_targets(targets: list[str]) -> None:
    seen: set[str] = set()
    for target in targets:
        normalized = str(target or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            await _push_pending_commands_for_target(normalized)


def _serialize_commands() -> list[dict[str, Any]]:
    return [
        {**cmd, "age_secs": int(time.time() - cmd["created_at"])}
        for cmd in commands
    ]


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


def _vm_watchdog_key(vmid: Any) -> str | None:
    try:
        return str(int(vmid))
    except (TypeError, ValueError):
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


def _record_vm_watchdog_clone_completed(
    vmid: Any,
    hostname: Any,
    *,
    clone_completed_at: float | None = None,
    reclone_count: int | None = None,
) -> bool:
    vmid_key = _vm_watchdog_key(vmid)
    if vmid_key is None:
        return False
    current = vm_watchdog.get(vmid_key) or {}
    if reclone_count is None:
        try:
            reclone_count = max(0, int(current.get("reclone_count", 0) or 0))
        except (TypeError, ValueError):
            reclone_count = 0
    vm_watchdog[vmid_key] = {
        "clone_completed_at": float(clone_completed_at if clone_completed_at is not None else time.time()),
        "reclone_count": max(0, int(reclone_count)),
        "hostname": str(hostname or current.get("hostname") or "").strip(),
    }
    return True


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


def _proxmox_usb_config_payload(hostname: str | None = None) -> dict[str, Any]:
    # Read sim_phy from the repo's simulation.conf so the agent knows which
    # USB device type (wired/wireless/any) to provision and assign.
    sim_phy = "wireless"
    try:
        sim_conf = REPO_DIR / "configs" / "simulation.conf"
        if sim_conf.exists():
            parser = configparser.ConfigParser()
            parser.read_string(sim_conf.read_text(encoding="utf-8"))
            _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")
            sim_phy = parser.get("simulation", "sim_phy", fallback="wireless").strip().lower() or "wireless"
    except Exception:
        pass
    if sim_phy not in {"wireless", "ethernet", "any"}:
        sim_phy = "wireless"
    image1_template_spec = _resolved_template_spec(settings, 1)
    image2_template_spec = _resolved_template_spec(settings, 2)
    host_config = _get_proxmox_host_config(hostname) if hostname else {}
    vm_set_override = _sanitize_vm_set_override(host_config.get("vm_set_override", 0))
    return {
        "vidpids": _parse_json_list(settings.get("usb_vidpids", "[]")),
        "missing_timeout": _setting_int("usb_missing_timeout", 60, 1),
        "image1_template_id": int(_primary_template_id(image1_template_spec, _legacy_template_id(settings, 1)) or 100),
        "image1_template_spec": image1_template_spec,
        "image2_template_id": int(_primary_template_id(image2_template_spec, _legacy_template_id(settings, 2)) or 200),
        "image2_template_spec": image2_template_spec,
        "template_vmid_specs": [image1_template_spec, image2_template_spec],
        "image1_pct": max(0, min(100, int(str(settings.get("vm_image_1_pct", "50")).strip() or "50"))),
        "auto_provision": _normalize_toggle(settings.get("usb_auto_provision", "off")),
        "use_all_dongles": _setting_bool("use_all_dongles", False),
        "max_slots": max(1, min(256, int(str(settings.get("usb_max_slots", "24")).strip() or "24"))),
        "vmid_start": int(settings.get("vmid_start", 0) or 0),
        "vm_set_override": vm_set_override,
        "ignored_vidpids": _parse_json_list(settings.get("usb_ignored_vidpids", "[]")),
        "sim_phy": sim_phy,
        "reclone_concurrency": max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1")),
        "l1_vlan_start": max(1, min(4094, int(str(settings.get("l1_vlan_start", "100")).strip() or "100"))),
        "l1_vlan_end": max(1, min(4094, int(str(settings.get("l1_vlan_end", "199")).strip() or "199"))),
        "guest_agent_watchdog_enabled": _normalize_toggle(settings.get("guest_agent_watchdog_enabled", "on")),
        "guest_agent_grace_minutes": max(1, int(str(settings.get("guest_agent_grace_minutes", "20")).strip() or "20")),
        "guest_agent_check_interval_minutes": max(1, int(str(settings.get("guest_agent_check_interval_minutes", "10")).strip() or "10")),
        "guest_agent_reboot_after_minutes": max(1, int(str(settings.get("guest_agent_reboot_after_minutes", "10")).strip() or "10")),
        "guest_agent_reclone_after_minutes": max(1, int(str(settings.get("guest_agent_reclone_after_minutes", "30")).strip() or "30")),
        "watchdog_reboot_enabled": _normalize_toggle(settings.get("watchdog_reboot_enabled", "on")),
        "cpu_provision_threshold": max(0, min(100, int(str(settings.get("cpu_provision_threshold", "80")).strip() or "80"))),
        "mem_provision_threshold": max(0, min(100, int(str(settings.get("mem_provision_threshold", "80")).strip() or "80"))),
    }


def _normalize_proxmox_hostname(hostname: Any) -> str:
    return str(hostname or "").strip().rstrip(".").lower()


def _proxmox_hostname_aliases(hostname: Any) -> tuple[str, ...]:
    normalized = _normalize_proxmox_hostname(hostname)
    if not normalized:
        return ()
    aliases = [normalized]
    short = normalized.split(".", 1)[0]
    if short and short not in aliases:
        aliases.append(short)
    return tuple(aliases)


def _proxmox_hostnames_match(left: Any, right: Any) -> bool:
    left_aliases = set(_proxmox_hostname_aliases(left))
    return bool(left_aliases and left_aliases.intersection(_proxmox_hostname_aliases(right)))


def _resolve_proxmox_agent_hostname(hostname: Any, registry: dict[str, Any]) -> str | None:
    if not isinstance(registry, dict):
        return None
    for registered_hostname in registry:
        if _proxmox_hostnames_match(hostname, registered_hostname):
            return registered_hostname
    return None


def _upsert_pending_proxmox_agent(hostname: Any, client_ip: str, now: float) -> str | None:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
    if not resolved_hostname:
        resolved_hostname = _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        return None
    entry = pending_proxmox_agents.get(resolved_hostname)
    if entry is None:
        pending_proxmox_agents[resolved_hostname] = {"ip": client_ip, "first_seen": now, "last_seen": now}
    else:
        entry["ip"] = client_ip
        entry["last_seen"] = now
    return resolved_hostname


def _pending_proxmox_payload() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "hostname": hostname,
            "ip": info.get("ip", ""),
            "first_seen": info.get("first_seen", now),
            "last_seen": info.get("last_seen", now),
        }
        for hostname, info in pending_proxmox_agents.items()
    ]


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


def _approved_proxmox_payload() -> list[dict[str, Any]]:
    result = []
    for hostname in approved_proxmox_agents:
        state = proxmox_states.get(hostname, {})
        host_config = _get_proxmox_host_config(hostname)
        vm_set_override = _sanitize_vm_set_override(state.get("vm_set_override", host_config.get("vm_set_override", 0)))
        result.append({
            "hostname": hostname,
            "connected": bool(state.get("connected", False)),
            "last_seen": state.get("last_seen"),
            "agent_version": state.get("agent_version"),
            "pve_version": state.get("pve_version"),
            "vm_count": int(state.get("vm_count", 0)),
            "usb_count": int(state.get("usb_count", 0)),
            "node": state.get("node", {}),
            "provision_halt": _current_provision_halt(state),
            "cpu_1h_avg": state.get("cpu_1h_avg"),
            "mem_1h_avg": state.get("mem_1h_avg"),
            "vmid_range": state.get("vmid_range"),
            "vm_set_override": vm_set_override,
            "effective_vm_set": int(state.get("effective_vm_set", vm_set_override or _hostname_vm_set_number(hostname))),
        })
    return result


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


def _proxmox_status_payload() -> dict[str, Any]:
    node = proxmox_state.get("node") or {}
    client_seen = {hostname: client.get("last_seen") for hostname, client in clients.items()}
    usb_by_vmid = {
        str(entry.get("vmid")): entry
        for entry in proxmox_state.get("usb_state", [])
        if entry.get("vmid") is not None
    }
    vms = []
    current_vmids: set[int] = set()
    for vm in proxmox_state.get("vms") or []:
        enriched_vm = dict(vm)
        enriched_vm["pending_checkin"] = _vm_pending_checkin(enriched_vm, client_seen)
        enriched_vm["watchdog_tracked"] = bool(_vm_watchdog_key(vm.get("vmid")) and vm_watchdog.get(_vm_watchdog_key(vm.get("vmid"))))
        usb_entry = usb_by_vmid.get(str(vm.get("vmid")), {})
        enriched_vm["prov_status"] = usb_entry.get("prov_status") or "active"
        try:
            vmid_int = int(vm.get("vmid"))
            current_vmids.add(vmid_int)
            if vmid_int in _pending_delete_vmids:
                enriched_vm["status"] = "deleting"
        except (TypeError, ValueError):
            pass
        vms.append(enriched_vm)
    # Include any pending-delete VMIDs that have already been removed from agent telemetry
    # so the UI keeps showing them as "deleting…" until the next full render cycle.
    for pending_vmid in _pending_delete_vmids:
        if pending_vmid not in current_vmids:
            vms.append({
                "vmid": pending_vmid,
                "name": f"VM {pending_vmid}",
                "status": "deleting",
                "type": "qemu",
                "prov_status": "active",
                "pending_checkin": False,
                "watchdog_tracked": False,
            })
    return {
        **proxmox_state,
        "vms": vms,
        "prov_run": dict(proxmox_state.get("prov_run") or {}),
        "hostname": str(node.get("hostname") or "").strip(),
        "pending_proxmox": _pending_proxmox_payload(),
        "approved_proxmox": _approved_proxmox_payload(),
        "reclone_state": dict(reclone_state),
        "client_os_counts": _client_os_counts(),
        "auto_recovery_pending": _auto_recovery_pending_vmids(),
        "webui_vmid": WEBUI_VMID,
        "reseed_in_progress": bool(_proxmox_reseed_in_progress),
        "cpu_1h_avg": _resource_1h_average(_cpu_samples),
        "mem_1h_avg": _resource_1h_average(_mem_samples),
        "provision_halt": _current_provision_halt(),
        "cpu_est_avg": _resource_estimated_average(_cpu_samples),
        "mem_est_avg": _resource_estimated_average(_mem_samples),
        "resource_samples_started": _resource_samples_started or None,
        "resource_sample_count": len(_cpu_samples),
        "pending_command_count": len([c for c in commands if c.get("status") in ("queued", "delivered")]),
        "spoke_version": APP_VERSION,
    }


def _find_proxmox_vm(vmid: int) -> dict[str, Any] | None:
    for vm in proxmox_state.get("vms") or []:
        try:
            if int(vm.get("vmid")) == vmid:
                return dict(vm)
        except (TypeError, ValueError):
            continue
    return None


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


async def _broadcast_proxmox_state() -> None:
    global _last_proxmox_hash
    _save_state_cache()
    payload = _proxmox_status_payload()
    h = hashlib.md5(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    if h == _last_proxmox_hash:
        return
    _last_proxmox_hash = h
    await broadcast({"type": "proxmox_update", **payload})


async def _broadcast_reclone_state() -> None:
    await _async_save_reclone_state()
    await broadcast({"type": "reclone_update", **dict(reclone_state)})


def _update_reclone_log(vmid: int, name: str, status: str, message: str | None = None) -> None:
    timestamp = iso_utcnow()
    for entry in reversed(reclone_state["log"]):
        if entry.get("vmid") == vmid and entry.get("status") in {"queued", "in_progress"}:
            entry.update({"name": name, "status": status, "timestamp": timestamp})
            if message:
                entry["message"] = message
            elif entry.get("message") and status in {"queued", "in_progress"}:
                entry.pop("message", None)
            break
    else:
        entry = {"vmid": vmid, "name": name, "status": status, "timestamp": timestamp}
        if message:
            entry["message"] = message
        reclone_state["log"].append(entry)
    reclone_state["log"] = reclone_state["log"][-200:]
    _save_reclone_state()


def _parse_reclone_schedule(value: Any) -> tuple[str, int, int] | None:
    raw = str(value or "").strip().lower()
    parts = raw.split()
    if len(parts) != 2:
        return None
    day, clock = parts
    if day not in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
        return None
    try:
        hour, minute = (int(piece) for piece in clock.split(":", 1))
    except ValueError:
        return None
    if hour not in range(24) or minute not in range(60):
        return None
    return day, hour, minute


def _has_pending_reclone(vmid: int) -> bool:
    for cmd in commands:
        if cmd.get("action") != "reclone_vm":
            continue
        if int(cmd.get("args", {}).get("vmid", -1)) != vmid:
            continue
        if cmd.get("status") in {"pending", "delivered"}:
            return True
    return False


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


def _proxmox_unassigned_present_usb() -> list[dict[str, Any]]:
    assigned_buses = {
        str(entry.get("bus_path", "")).strip()
        for entry in proxmox_state.get("usb_state", [])
        if str(entry.get("bus_path", "")).strip() and entry.get("vmid") is not None
    }
    return [
        dict(entry)
        for entry in proxmox_state.get("present_usb", [])
        if str(entry.get("bus_path", "")).strip()
        and str(entry.get("bus_path", "")).strip() not in assigned_buses
    ]



def _normalize_proxmox_usb_state(
    usb_state: Any,
    present_usb: Any,
) -> list[dict[str, Any]]:
    present_by_bus = {
        str(entry.get("bus_path", "")).strip(): dict(entry)
        for entry in (present_usb if isinstance(present_usb, list) else [])
        if isinstance(entry, dict) and str(entry.get("bus_path", "")).strip()
    }
    normalized: list[dict[str, Any]] = []
    for raw_entry in usb_state if isinstance(usb_state, list) else []:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        bus_path = str(entry.get("bus_path", "")).strip()
        present_entry = present_by_bus.get(bus_path)
        if present_entry:
            entry["missing_since"] = None
            if entry.get("prov_status") in {None, "", "missing", "tearing_down"}:
                entry["prov_status"] = "active"
            if not entry.get("vidpid") and present_entry.get("vidpid"):
                entry["vidpid"] = present_entry.get("vidpid")
            if not entry.get("name") and present_entry.get("name"):
                entry["name"] = present_entry.get("name")
        normalized.append(entry)
    return normalized


def _derive_provision_run_item_status(
    usb_entry: dict[str, Any],
    vm_by_vmid: dict[str, dict[str, Any]],
) -> str:
    if str(usb_entry.get("prov_status") or "").strip().lower() != "provisioning":
        return "done"
    vm = vm_by_vmid.get(str(usb_entry.get("vmid"))) or {}
    vm_status = str(vm.get("status") or "").strip().lower()
    return "configuring" if vm_status == "running" else "cloning"


def _update_provision_run_state(vms: list[dict[str, Any]], usb_state: list[dict[str, Any]], now: int) -> None:
    run = _default_provision_run_state()
    current = proxmox_state.get("prov_run")
    if isinstance(current, dict):
        for key in ("running", "started_at", "updated_at", "completed_at", "total", "completed", "failed"):
            run[key] = current.get(key)
        run["items"] = [
            dict(item)
            for item in current.get("items", [])
            if isinstance(item, dict) and item.get("vmid") is not None
        ]

    vm_by_vmid = {
        str(vm.get("vmid")): dict(vm)
        for vm in (vms if isinstance(vms, list) else [])
        if isinstance(vm, dict) and vm.get("vmid") is not None
    }
    usb_by_vmid = {
        str(entry.get("vmid")): dict(entry)
        for entry in (usb_state if isinstance(usb_state, list) else [])
        if isinstance(entry, dict) and entry.get("vmid") is not None
    }
    provisioning_vmids = [
        vmid
        for vmid, entry in usb_by_vmid.items()
        if str(entry.get("prov_status") or "").strip().lower() == "provisioning"
    ]
    provisioning_vmids.sort(key=lambda value: int(value) if str(value).isdigit() else value)

    if not run.get("running") and provisioning_vmids:
        run = _default_provision_run_state()
        run["running"] = True
        run["started_at"] = now
        run["updated_at"] = now

    items = run["items"]
    item_by_vmid = {str(item.get("vmid")): item for item in items if item.get("vmid") is not None}

    if run.get("running"):
        for vmid in provisioning_vmids:
            entry = usb_by_vmid[vmid]
            item = item_by_vmid.get(vmid)
            if item is None:
                vm = vm_by_vmid.get(vmid) or {}
                item = {
                    "vmid": entry.get("vmid"),
                    "vm_name": str(vm.get("name") or "").strip() or None,
                    "usb_name": str(entry.get("name") or "").strip() or None,
                    "bus_path": str(entry.get("bus_path") or "").strip() or None,
                    "vidpid": str(entry.get("vidpid") or "").strip() or None,
                    "status": _derive_provision_run_item_status(entry, vm_by_vmid),
                    "started_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                items.append(item)
                item_by_vmid[vmid] = item
            elif item.get("status") in {"done", "failed"}:
                item.update({
                    "status": _derive_provision_run_item_status(entry, vm_by_vmid),
                    "started_at": now,
                    "updated_at": now,
                    "completed_at": None,
                })

    for item in items:
        vmid_key = str(item.get("vmid"))
        entry = usb_by_vmid.get(vmid_key)
        vm = vm_by_vmid.get(vmid_key) or {}
        if vm.get("name"):
            item["vm_name"] = str(vm.get("name"))
        if entry:
            if entry.get("name"):
                item["usb_name"] = str(entry.get("name"))
            if entry.get("bus_path"):
                item["bus_path"] = str(entry.get("bus_path"))
            if entry.get("vidpid"):
                item["vidpid"] = str(entry.get("vidpid"))

        previous_status = str(item.get("status") or "pending")
        next_status = previous_status
        if entry and str(entry.get("prov_status") or "").strip().lower() == "provisioning":
            enriched = next((v for v in vms if str(v.get("vmid")) == vmid_key), None)
            # If the watchdog confirms the client has already checked in, the USB state
            # is just lagging — treat as done rather than staying stuck at "configuring".
            if (enriched
                    and enriched.get("watchdog_tracked")
                    and not enriched.get("pending_checkin")
                    and str(enriched.get("status", "")).lower() == "running"):
                next_status = "done"
                item["completed_at"] = item.get("completed_at") or now
            else:
                next_status = _derive_provision_run_item_status(entry, vm_by_vmid)
                item["completed_at"] = None
        elif entry and str(entry.get("prov_status") or "").strip().lower() == "active":
            if previous_status != "failed":
                # Clone finished — keep as "pending_checkin" until the VM's client
                # actually contacts the API (pending_checkin flag on the enriched VM).
                # This keeps run.running=True and the live panel visible through the
                # boot-up gap between clone-complete and first API check-in.
                enriched = next(
                    (v for v in vms if str(v.get("vmid")) == vmid_key),
                    None,
                )
                if enriched and enriched.get("pending_checkin"):
                    next_status = "pending_checkin"
                    item["completed_at"] = None
                else:
                    next_status = "done"
                    item["completed_at"] = item.get("completed_at") or now
        elif run.get("running") and previous_status not in {"done", "failed"}:
            if _prev_usb_by_vmid.get(vmid_key) == "provisioning":
                next_status = "failed"
                item["completed_at"] = item.get("completed_at") or now

        if next_status != previous_status or (entry and str(entry.get("prov_status") or "").strip().lower() == "provisioning"):
            item["updated_at"] = now
        item["status"] = next_status

    run["total"] = len(items)
    run["completed"] = sum(1 for item in items if item.get("status") == "done")
    run["failed"] = sum(1 for item in items if item.get("status") == "failed")

    # "pending_checkin" is an active (non-terminal) status — keep run alive
    active_items = [item for item in items if item.get("status") not in {"done", "failed"}]
    if run.get("running") and items and not active_items:
        run["running"] = False
        run["completed_at"] = now
        run["updated_at"] = now
    elif run.get("running"):
        run["updated_at"] = now

    proxmox_state["prov_run"] = run


def _guest_supports_reclone(vm: dict[str, Any]) -> bool:
    if vm.get("is_template"):
        return False
    if _is_protected_vmid(vm.get("vmid")):
        return False
    return bool(vm.get("reclone_supported"))


def _reclone_targets_for_run() -> list[dict[str, Any]]:
    return sorted(
        [
            dict(vm)
            for vm in proxmox_state.get("vms") or []
            if (
                vm.get("vmid") is not None
                and _guest_supports_reclone(vm)
                and int(vm.get("vmid", 0)) > 9000  # only auto-provisioned sim clients
            )
        ],
        key=lambda vm: int(vm.get("vmid", 0)),
    )



def _reclone_command_args(vm: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "vmid": int(vm.get("vmid")),
        "type": str(vm.get("type") or "qemu"),
    }
    if vm.get("reclone_source_vmid") is not None:
        args["source_vmid"] = int(vm["reclone_source_vmid"])
    if vm.get("reclone_bus_path"):
        args["bus_path"] = str(vm["reclone_bus_path"])
    return args


async def _queue_command(target: str, action: str, args: dict[str, Any] | None = None, command_type: str | None = None) -> dict[str, Any]:
    async with state_lock:
        cmd, created, expired, purged = _enqueue_command_locked(target, action, args, command_type=command_type)
        serialized = _serialize_commands()
    if created or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    if created:
        await _push_pending_commands_for_target(target)
    return cmd


async def _queue_proxmox_command(action: str, args: dict[str, Any] | None = None, command_type: str | None = None, target: str = "proxmox") -> dict[str, Any]:
    # In multi-agent setups, resolve the generic "proxmox" target to the currently
    # WS-connected (primary) agent so the command is delivered to exactly one agent.
    # Commands remain generic "proxmox" if no agent is currently connected via WS
    # (they'll be picked up by whichever agent polls next).
    resolved = target
    if target == "proxmox" and proxmox_ws_hostname:
        resolved = proxmox_ws_hostname
    return await _queue_command(resolved, action, args, command_type=command_type)


def _resolve_proxmox_vm_target(vmid: int | None) -> str:
    """Return the specific agent hostname that owns this vmid, or 'proxmox' if unknown."""
    if vmid is not None:
        owner = _proxmox_agent_vm_map.get(int(vmid))
        if owner and owner in approved_proxmox_agents:
            return owner
    return "proxmox"


async def _queue_unlock_template_command(command_type: str = "unlock_template") -> dict[str, Any]:
    return await _queue_proxmox_command("unlock_template", {}, command_type=command_type)


def _unlock_template_result(cmd: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "queued": True,
        "task_type": "unlock_template",
        "detail": "Template unlock queued",
        "command_id": cmd.get("id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _proxmox_update_branch() -> str:
    branch = str(settings.get("repo_branch", REPO_BRANCH) or REPO_BRANCH).strip()
    return branch or REPO_BRANCH


def _proxmox_update_args() -> dict[str, str]:
    branch = _proxmox_update_branch()
    return {
        "branch": branch,
        "repo_raw": f"{CLIENT_SIM_REPO_RAW.rstrip('/')}/{branch}",
    }


def _resolve_proxmox_update_target() -> str:
    hostname = str((proxmox_state.get("node") or {}).get("hostname") or "").strip()
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if resolved_hostname:
        return resolved_hostname
    if len(approved_proxmox_agents) == 1:
        return next(iter(approved_proxmox_agents))
    if not approved_proxmox_agents:
        raise HTTPException(status_code=409, detail="No approved Proxmox agent is available")
    raise HTTPException(status_code=409, detail="Unable to determine which Proxmox host should be updated")


async def _queue_proxmox_agent_update(target: str | None = None) -> dict[str, Any]:
    resolved_target = target or _resolve_proxmox_update_target()
    if resolved_target not in approved_proxmox_agents:
        raise HTTPException(status_code=404, detail="Proxmox agent not approved")
    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        cmd, created, _expired, _purged = _enqueue_command_locked(resolved_target, "update_agent", _proxmox_update_args())
        serialized = _serialize_commands()
    if not created:
        raise HTTPException(status_code=409, detail=f"An agent update is already queued for {resolved_target}")
    if expired or purged or created:
        await broadcast({"type": "commands_update", "commands": serialized})
    await _push_pending_commands_for_target(resolved_target)
    return cmd


async def _run_rolling_reclone(trigger_type: str) -> None:
    async with reclone_run_lock:
        if reclone_state["status"] == "running":
            return

        vms = _reclone_targets_for_run()
        reclone_state.update({
            "status": "running",
            "type": trigger_type,
            "total": len(vms),
            "completed": 0,
            "failed": 0,
            "current_vm": None,
            "log": [],
            "started_at": iso_utcnow(),
        })
        logger.info("Rolling reclone (%s): %d eligible VMs: %s", trigger_type, len(vms), [v.get("vmid") for v in vms])
        await _broadcast_reclone_state()
        await _broadcast_proxmox_state()

        concurrency = max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1"))

        async def _reclone_one(vm: dict) -> None:
            vmid = int(vm.get("vmid"))
            name = vm.get("name") or f"VM {vmid}"
            _update_reclone_log(vmid, name, "queued")
            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()

            cmd = await _queue_proxmox_command("reclone_vm", _reclone_command_args(vm), command_type=trigger_type)
            deadline = time.time() + 1800
            last_status = "pending"
            poll_interval = 2.0
            while time.time() < deadline:
                # Commands remain in a small module-level list, so a linear scan keeps the
                # lookup simple here without a broader commands storage refactor.
                current = next((item for item in commands if item["id"] == cmd["id"]), None)
                if current is None:
                    break
                status = current.get("status", "pending")
                if status != last_status:
                    if status == "delivered":
                        _update_reclone_log(vmid, name, "in_progress")
                        await _broadcast_reclone_state()
                        await _broadcast_proxmox_state()
                        poll_interval = 5.0
                    last_status = status
                if status in {"completed", "failed", "expired"}:
                    final_status = "completed" if status == "completed" else "failed"
                    _update_reclone_log(vmid, name, final_status, str(current.get("message") or "").strip() or None)
                    if final_status == "completed":
                        _record_vm_watchdog_clone_completed(vmid, name)
                        await _async_save_vm_watchdog()
                        reclone_state["completed"] += 1
                    else:
                        reclone_state["failed"] += 1
                    await _broadcast_reclone_state()
                    await _broadcast_proxmox_state()
                    return
                await asyncio.sleep(poll_interval)
                if status == "pending":
                    poll_interval = min(poll_interval * 2, 10.0)
            logger.warning("Rolling reclone: VM %s (%s) timed out", vmid, name)
            _trace("reclone_timeout", vmid=vmid, name=name, cmd_id=cmd.get("id"), trigger=trigger_type)
            _update_reclone_log(vmid, name, "failed", "Timed out waiting for Proxmox agent ACK")
            reclone_state["failed"] += 1
            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()

        try:
            for i in range(0, len(vms), concurrency):
                batch = vms[i:i + concurrency]
                reclone_state["current_vm"] = int(batch[0].get("vmid")) if batch else None
                await _broadcast_reclone_state()
                await asyncio.gather(*(_reclone_one(vm) for vm in batch))

            # After recloning existing VMs, trigger provisioning for any
            # unassigned dongles (present USB device with no VM allocated).
            unassigned = _proxmox_unassigned_present_usb()
            if unassigned:
                logger.info(
                    "Rolling reclone: found %d unassigned dongle(s) — queuing provision_unassigned",
                    len(unassigned),
                )
                await _queue_proxmox_command("provision_unassigned", {}, command_type=trigger_type)

            reclone_state["status"] = "failed" if reclone_state["failed"] else "completed"
        except Exception as exc:
            logger.exception("Rolling reclone failed: %s", exc)
            reclone_state["status"] = "failed"
            reclone_state["failed"] += 1
        finally:
            reclone_state["current_vm"] = None
            # Capture a last_run summary before resetting so the UI can show
            # "Last run: X completed, Y failed" even after the tile goes idle.
            reclone_state["last_run"] = {
                "timestamp": iso_utcnow(),
                "completed": reclone_state["completed"],
                "failed": reclone_state["failed"],
                "type": trigger_type,
            }
            if reclone_state["status"] != "running":
                reclone_state["started_at"] = None

            # Once the run has reached a terminal state (completed / failed),
            # reset all counters and the log back to idle so the Fleet Reclone
            # tile disappears and shows 0 instead of lingering at the last
            # progress value.  The last_run summary we just captured above is
            # preserved so the "Last run" line in the UI still reflects what
            # happened.
            terminal_statuses = {"completed", "failed", "interrupted"}
            if reclone_state["status"] in terminal_statuses:
                saved_last_run = reclone_state["last_run"]
                saved_auto_log = reclone_state.get("auto_recovery_log") or []
                reclone_state.update({
                    "status": "idle",
                    "type": None,
                    "total": 0,
                    "completed": 0,
                    "failed": 0,
                    "current_vm": None,
                    "log": [],
                    "started_at": None,
                    "last_run": saved_last_run,
                    "auto_recovery_log": saved_auto_log,
                })
                logger.info(
                    "Rolling reclone: terminal state reached — reset to idle "
                    "(completed=%d, failed=%d)",
                    saved_last_run.get("completed", 0),
                    saved_last_run.get("failed", 0),
                )

            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()


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




async def vm_watchdog_loop() -> None:
    await asyncio.sleep(VM_WATCHDOG_INTERVAL_SECS)
    while True:
        try:
            now = time.time()
            client_seen = {hostname: client.get("last_seen") for hostname, client in clients.items()}
            vm_names = {
                str(int(vm.get("vmid"))): str(vm.get("name") or "").strip()
                for vm in proxmox_state.get("vms") or []
                if vm.get("vmid") is not None
            }
            changed = False
            broadcast_needed = False
            for vmid_key, entry in list(vm_watchdog.items()):
                clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
                if clone_completed_at is None:
                    vm_watchdog.pop(vmid_key, None)
                    changed = True
                    broadcast_needed = True
                    continue
                hostname = str(entry.get("hostname") or vm_names.get(vmid_key) or "").strip()
                if hostname and hostname != entry.get("hostname"):
                    entry["hostname"] = hostname
                    changed = True
                if _vm_has_checked_in(hostname, clone_completed_at, client_seen):
                    vm_watchdog.pop(vmid_key, None)
                    changed = True
                    broadcast_needed = True
                    continue
                if (now - clone_completed_at) <= VM_WATCHDOG_TIMEOUT_SECS:
                    continue
                vmid_int = int(vmid_key)
                if _has_pending_reclone(vmid_int):
                    continue
                vm = _find_proxmox_vm(vmid_int) or {"vmid": vmid_int}
                await _queue_proxmox_command("reclone_vm", _reclone_command_args(vm), command_type="watchdog")
                reclone_count = max(0, int(entry.get("reclone_count", 0) or 0)) + 1
                _record_vm_watchdog_clone_completed(
                    vmid_int,
                    hostname or vm.get("name"),
                    clone_completed_at=now,
                    reclone_count=reclone_count,
                )
                changed = True
                broadcast_needed = True
                logger.warning("VM watchdog queued reclone for VM %s (%s) after 24h without check-in", vmid_int, hostname or vm.get("name") or f"VM {vmid_int}")
                _trace("watchdog_reclone_queued", vmid=vmid_int, name=hostname or vm.get("name") or f"VM {vmid_int}", reclone_count=reclone_count)
            if changed:
                await _async_save_vm_watchdog()
            if broadcast_needed:
                await _broadcast_proxmox_state()
            _update_service_health("vm_watchdog", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("vm_watchdog", ok=False, error=str(exc))
            logger.exception("VM watchdog error: %s", exc)
        await asyncio.sleep(VM_WATCHDOG_INTERVAL_SECS)


async def schedule_check() -> None:
    global last_schedule_trigger
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
                        if last_schedule_trigger != trigger_key:
                            last_schedule_trigger = trigger_key
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




async def expire_commands() -> None:
    """Expire stale active commands and purge terminal results after a short grace period."""
    await asyncio.sleep(15)
    while True:
        try:
            async with state_lock:
                expired, purged = _cleanup_commands_locked()
                serialized = _serialize_commands()
            if expired:
                logger.warning("Expired %d stale command(s) from the in-memory queue", expired)
                await broadcast({"type": "commands_update", "commands": serialized})
                await broadcast({"type": "notification", "level": "warning", "message": "One or more commands expired without being ACKed by the agent."})
            elif purged:
                await broadcast({"type": "commands_update", "commands": serialized})
            _update_service_health("command_expiry", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("command_expiry", ok=False, error=str(exc))
            logger.exception("Command expiry error: %s", exc)
        await asyncio.sleep(15)




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


def _hub_isolated() -> bool:  # Compute whether hub-driven config pushes must pause so every safeguard check shares one helper.
    return bool(  # Evaluate the isolation rule in one expression so every caller uses the same last-sync timeout test.
        settings.get("hub_managed")  # Only hub-managed spokes should self-protect because self-managed spokes do not accept hub config pushes.
        and settings.get("relay_enabled") == "on"  # Only an enabled relay can be isolated because disabled hub connectivity should not trigger this safeguard.
        and relay_state.get("last_sync")  # A real last check-in is required so the timeout compares against known hub contact instead of guessing.
        and (time.time() - float(relay_state["last_sync"])) > int(settings.get("hub_isolation_timeout", 3600))  # Enter isolation after the configured no-contact window so stale hubs stop changing live config.
    )  # Share one boolean source of truth so every relay path evaluates isolation consistently.


def _revert_hub_managed_if_auth_failure(status_code: int | None, reason: str) -> bool:
    """Immediately revert hub_managed to False when the hub returns a definitive auth/not-found error.

    A 401 (wrong key), 403 (wrong PSK or forbidden), or 404 (tenant deleted) means the hub cannot
    recognise this spoke anymore — waiting for the isolation timeout would leave the spoke stuck in
    a read-only hub-managed state indefinitely.  Reverting immediately restores local control.

    Returns True if hub_managed was cleared so callers can log/broadcast the change.
    """
    if not settings.get("hub_managed"):
        return False
    if status_code not in (401, 403, 404):
        return False
    settings["hub_managed"] = False
    _save_settings()
    logger.warning(
        "hub_managed reverted to local control — hub auth failure (HTTP %s): %s",
        status_code,
        reason,
    )
    _relay_diag_append("hub_managed_reverted", status_code=status_code, reason=reason)
    return True


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


def _hub_config_isolation_result(task_type: str) -> dict[str, Any]:  # Build a consistent skip result so every blocked hub config push is acknowledged the same way.
    return {  # Return a structured ack payload so the hub can tell a deliberate isolation skip from a transport failure.
        "success": False,  # Mark the ack as non-success so the hub can distinguish a safeguard skip from an applied config change.
        "skipped": True,  # Flag the result as skipped so operators can see the spoke deliberately ignored the push.
        "reason": "hub_isolated",  # Identify the exact safeguard reason so downstream tooling can explain the skip clearly.
        "task_type": task_type,  # Echo the original command type so the hub knows which config action was paused.
        "detail": "Hub isolated — config pushes paused until contact resumes",  # Explain the safeguard outcome so the hub UI does not look like a silent failure.
        "timestamp": datetime.now(timezone.utc).isoformat(),  # Stamp the skip result so operators can correlate it with outage timing.
    }  # Return one reusable skip payload so every blocked hub config ack stays consistent.


async def hub_isolation_monitor() -> None:  # Poll isolation state so the UI updates even when no relay message arrives to trigger a broadcast.
    last_isolated = _hub_isolated()  # Capture the initial state so the monitor only broadcasts when isolation actually changes.
    while True:  # Keep watching in the background so timeout expiry and recovery both reach the UI automatically.
        await asyncio.sleep(60)  # Re-check once per minute so the safeguard flips even when no hub message arrives to trigger a relay broadcast.
        current_isolated = _hub_isolated()  # Recompute isolation from last_sync so timeout expiry and recovery both use the live source of truth.
        if current_isolated != last_isolated:  # Only broadcast on state changes so the monitor updates the UI without creating noisy relay traffic.
            last_isolated = current_isolated  # Remember the new state so the next loop only announces another real transition.
            await _broadcast_relay_state()  # Push the changed isolation state to browsers immediately so banners and status text stay accurate.


async def _broadcast_update_state() -> None:
    _save_update_state()
    await broadcast({"type": "version_status", **dict(update_state)})


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


async def _hub_self_register(server_url: str) -> None:
    """POST to hub /api/spokes/register with full config payload.
    Stores the returned spoke_id. If already approved, also stores api_key and tenant_id."""
    global relay_registration_refresh_needed
    hostname = socket.gethostname()
    spoke_name = settings.get("relay_spoke_name", "").strip() or hostname
    spoke_id = _ensure_relay_spoke_id()
    payload = {
        "spoke_id": spoke_id,
        "hostname": hostname,
        "label": hostname,
        "spoke_name": spoke_name,
        "tenant_id_hint": (settings.get("relay_tenant_id") or settings.get("relay_tenant_hint") or "").strip(),
        "onboarding_psk": settings.get("relay_onboarding_psk", "").strip(),
        "api_key": settings.get("relay_api_key", "").strip(),
        "config": _build_registration_config(),
    }
    _relay_diag_append("register_attempt", url=f"{server_url}/api/spokes/register",
                       hostname=hostname, spoke_name=spoke_name, spoke_id=spoke_id)
    hub_base = _relay_hub_base_url(server_url, settings.get("relay_tenant_id", ""))
    try:
        async with httpx.AsyncClient(timeout=15, verify=_hub_tls_verify()) as hc:
            resp = await hc.post(f"{hub_base}/api/spokes/register", json=payload)
            if resp.status_code == 409:
                data = resp.json()
                conflict = data.get("conflict", "name_in_use")
                msg = data.get("message", f"Spoke name '{spoke_name}' is already in use on the hub. Choose a different name.")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                relay_state.update({
                    "connected": False,
                    "registration_status": "name_conflict",
                    "error": f"{ts} — {msg}",
                })
                relay_registration_refresh_needed = False
                _save_relay_state()
                _relay_diag_append("register_409", conflict=conflict, message=msg)
                logger.warning("Hub registration name conflict: %s", msg)
                return
            resp.raise_for_status()
            data = resp.json()
        spoke_id = str(data.get("spoke_id", "")).strip()
        status = data.get("status", "pending")
        if spoke_id and not _relay_spoke_id_needs_rotation(spoke_id):
            settings["relay_spoke_id"] = spoke_id
        else:
            spoke_id = _ensure_relay_spoke_id()
        if status == "approved":
            tenant_id = data.get("tenant_id", "")
            settings["relay_api_key"] = data.get("api_key", "")
            settings["relay_tenant_id"] = tenant_id
            settings["relay_tenant_hint"] = tenant_id
            relay_state["registration_status"] = "approved"
            relay_state["error"] = ""
            _relay_diag_append("register_ok", status="approved", spoke_id=spoke_id,
                               tenant_id=data.get("tenant_id"))
            logger.info("Hub registration: approved immediately spoke_id=%s tenant_id=%s", spoke_id, data.get("tenant_id"))
        else:
            tenant_hint = str(data.get("tenant_hint", "")).strip()
            settings["relay_api_key"] = ""
            settings["relay_tenant_id"] = ""
            if tenant_hint:
                settings["relay_tenant_hint"] = tenant_hint
            relay_state["registration_status"] = "pending"
            relay_state["error"] = ""
            _relay_diag_append("register_ok", status="pending", spoke_id=spoke_id)
            logger.info("Hub registration submitted: spoke_id=%s status=pending", spoke_id)
        relay_registration_refresh_needed = False
        _save_relay_state()
        _save_settings()
    except Exception as exc:
        relay_registration_refresh_needed = True
        _relay_diag_append("register_error", error=str(exc))
        logger.warning("Hub self-register failed: %s", exc)
        relay_state.update({"connected": False, "error": f"Registration failed: {exc}"})
        _save_relay_state()


async def _hub_check_approval(server_url: str, spoke_id: str) -> None:
    """Re-POST registration to check if spoke has been approved.
    Hub returns 'approved' with api_key and tenant_id once superadmin has approved."""
    global relay_registration_refresh_needed
    hostname = socket.gethostname()
    spoke_name = settings.get("relay_spoke_name", "").strip() or hostname
    tenant_hint = (settings.get("relay_tenant_id") or settings.get("relay_tenant_hint") or "").strip()
    existing_api_key = settings.get("relay_api_key", "").strip()
    existing_tenant_id = settings.get("relay_tenant_id", "").strip()
    had_approval = bool(existing_api_key and existing_tenant_id)
    _relay_diag_append("check_approval", spoke_id=spoke_id)
    hub_base = _relay_hub_base_url(server_url, settings.get("relay_tenant_id", ""))
    try:
        async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc:
            resp = await hc.post(f"{hub_base}/api/spokes/register", json={
                "spoke_id": spoke_id,
                "hostname": hostname,
                "label": hostname,
                "spoke_name": spoke_name,
                "tenant_id_hint": tenant_hint,
                "onboarding_psk": settings.get("relay_onboarding_psk", "").strip(),
                "api_key": existing_api_key,
                "config": _build_registration_config(),
            })
            resp.raise_for_status()
            data = resp.json()
        status = data.get("status", "pending")
        updated = False
        returned_spoke_id = str(data.get("spoke_id", "")).strip()
        if returned_spoke_id and not _relay_spoke_id_needs_rotation(returned_spoke_id) and returned_spoke_id != settings.get("relay_spoke_id", ""):
            settings["relay_spoke_id"] = returned_spoke_id
            spoke_id = returned_spoke_id
            updated = True
        if status == "approved":
            tenant_id = data.get("tenant_id", "")
            settings["relay_api_key"] = data.get("api_key", "")
            settings["relay_tenant_id"] = tenant_id
            settings["relay_tenant_hint"] = tenant_id
            relay_state["registration_status"] = "approved"
            relay_state["error"] = ""
            updated = True
            _relay_diag_append("approval_received", spoke_id=spoke_id,
                               tenant_id=data.get("tenant_id"))
            logger.info("Hub approval received: spoke_id=%s tenant_id=%s", spoke_id, data.get("tenant_id"))
        else:
            tenant_hint = str(data.get("tenant_hint", "")).strip()
            if tenant_hint and tenant_hint != settings.get("relay_tenant_hint", ""):
                settings["relay_tenant_hint"] = tenant_hint
                updated = True
            if had_approval:
                relay_state["registration_status"] = "approved"
                relay_state["error"] = "Hub registration check returned pending; keeping existing approval until credentials are explicitly rejected."
                _relay_diag_append("pending_ignored", spoke_id=spoke_id, tenant_hint=tenant_hint)
                logger.warning("Hub registration check returned pending for approved spoke %s; keeping stored approval", spoke_id)
            else:
                relay_state["registration_status"] = "pending"
                relay_state["error"] = ""
                _relay_diag_append("still_pending", spoke_id=spoke_id)
                logger.info("Hub registration still pending: spoke_id=%s", spoke_id)
        relay_registration_refresh_needed = False
        _save_relay_state()
        if updated:
            _save_settings()
    except Exception as exc:
        relay_registration_refresh_needed = True
        _relay_diag_append("check_approval_error", error=str(exc))
        logger.warning("Hub approval check failed: %s", exc)


def _relay_hub_base_url(server_url: str, tenant_id: str) -> str:
    url = server_url.rstrip("/")
    if tenant_id:
        url = re.sub(rf"/api/{re.escape(tenant_id)}$", "", url)
    return url.rstrip("/")


def _hub_tls_verify() -> bool:
    return _normalize_relay_enabled(settings.get("hub_tls_verify", "off")) == "on"


def _relay_ws_url(server_url: str, tenant_id: str, spoke_id: str, api_key: str) -> str:
    hub_base = _relay_hub_base_url(server_url, tenant_id)
    if hub_base.startswith("https://"):
        hub_base = "wss://" + hub_base[len("https://"):]
    elif hub_base.startswith("http://"):
        hub_base = "ws://" + hub_base[len("http://"):]
    return f"{hub_base}/api/{tenant_id}/spokes/{spoke_id}/ws?api_key={api_key}"


def _telemetry_filtered_browse_list(items: list[dict[str, Any]], site_field: str) -> list[dict[str, Any]]:
    """Return browse list items filtered to only this spoke's assigned Central sites.

    Prevents unassigned-site data (fetched for the local browse tab) from being
    sent to the hub and polluting its distributed-mode aggregation.
    """
    assigned: set[str] = {
        str(v).strip().lower() for v in settings.get("site_mappings", {}).values() if v
    }
    if not assigned:
        return list(items)
    return [i for i in items if str(i.get(site_field) or "").strip().lower() in assigned]


def _telemetry_filtered_browse_dict(by_site: dict[str, Any]) -> dict[str, Any]:
    """Return a by-site browse dict filtered to only this spoke's assigned Central sites."""
    assigned: set[str] = {
        str(v).strip().lower() for v in settings.get("site_mappings", {}).values() if v
    }
    if not assigned:
        return dict(by_site)
    return {k: v for k, v in by_site.items() if str(k).strip().lower() in assigned}


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
        "reseed_in_progress": bool(_proxmox_reseed_in_progress),
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
            "cpu_1h_avg": _resource_1h_average(_cpu_samples),
            "mem_1h_avg": _resource_1h_average(_mem_samples),
            "provision_halt": _current_provision_halt(),
            "prov_run": dict(proxmox_state.get("prov_run") or {}),
            "cpu_est_avg": _resource_estimated_average(_cpu_samples),
            "mem_est_avg": _resource_estimated_average(_mem_samples),
            "resource_samples_started": _resource_samples_started or None,
            "resource_sample_count": len(_cpu_samples),
            "template_lock": str(proxmox_state.get("template_lock") or ""),
            "reseed_in_progress": bool(_proxmox_reseed_in_progress),
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
                "wireless_clients": dict(central_wireless_clients),
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
                "central_alerts": _telemetry_filtered_browse_list(central_browse_alerts, "site"),
                "central_insights": _telemetry_filtered_browse_list(central_browse_insights, "site"),
                "central_devices_by_site": _telemetry_filtered_browse_dict(central_browse_devices_by_site),
                "central_clients_by_site": _telemetry_filtered_browse_dict(central_browse_clients_by_site),
                # Individual client records (filtered to assigned sites) for hub distributed aggregation
                "central_clients": _telemetry_filtered_browse_list(central_browse_clients, "site"),
            },
            "reclone_state": {
                k: v for k, v in reclone_state.items() if k != "auto_recovery_log"
            },
        }


def _hub_reseed_block_result() -> dict[str, str]:
    return {
        "error": "reseed_in_progress",
        "message": "Reseed in progress — provisioning paused. Try again shortly.",
    }


async def _forward_hub_passthrough_to_proxmox(cmd_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if proxmox_ws_connection is None:
        raise RuntimeError("Proxmox agent is not connected")
    if cmd_type == "backup":
        logger.info(f"Forwarding backup command to proxmox agent: vm_ids={payload.get('vm_ids')}")
    elif cmd_type == "reseed":
        logger.info(f"Forwarding reseed command to proxmox agent: vm_ids={payload.get('vm_ids')}")
    else:
        logger.info(f"Forwarding {cmd_type} command to proxmox agent: action={payload.get('action')}")
    if cmd_type == "command":
        await proxmox_ws_connection.send_json({"type": cmd_type, **payload})
    else:
        await proxmox_ws_connection.send_json({"type": cmd_type, "payload": payload})
    return {
        "success": True,
        "task_type": cmd_type,
        "detail": f"Forwarded {cmd_type} command to proxmox agent",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _hub_targets_proxmox_agent(target: str) -> bool:
    normalized = _normalize_proxmox_hostname(target)
    if not normalized:
        return False
    if normalized == "proxmox":
        return True
    if proxmox_ws_hostname and _proxmox_hostnames_match(normalized, proxmox_ws_hostname):
        return True
    return _resolve_proxmox_agent_hostname(normalized, approved_proxmox_agents) is not None


def _hub_command_blocked_by_reseed(cmd_type: str, target: str, action: str) -> bool:
    if not _proxmox_reseed_in_progress:
        return False
    if cmd_type == "proxmox_reclone_all":
        return True
    return _hub_targets_proxmox_agent(target) and action in {"reclone_vm", "provision_unassigned"}


async def _relay_proxmox_progress_to_hub(message: dict[str, Any]) -> None:
    if _relay_ws_send_json is None:
        return
    outbound = dict(message)
    payload = outbound.get("payload") if isinstance(outbound.get("payload"), dict) else None
    if payload is not None:
        payload = dict(payload)
        if "spoke_id" not in payload and _relay_ws_spoke_id:
            payload["spoke_id"] = _relay_ws_spoke_id
        outbound["payload"] = payload
    elif "spoke_id" not in outbound and _relay_ws_spoke_id:
        outbound["spoke_id"] = _relay_ws_spoke_id
    await _relay_ws_send_json(outbound)


# ── VNC relay ─────────────────────────────────────────────────────────────────

_vnc_sessions: dict[str, asyncio.Queue] = {}
_direct_console_sessions: dict[str, dict[str, Any]] = {}
_DIRECT_CONSOLE_TTL = 60  # seconds until session token expires


async def _relay_vnc_to_hub(message: dict[str, Any]) -> None:
    """Forward a VNC frame/control message back to the hub."""
    if _relay_ws_send_json is None:
        return
    outbound = dict(message)
    if "spoke_id" not in outbound and _relay_ws_spoke_id:
        outbound["spoke_id"] = _relay_ws_spoke_id
    await _relay_ws_send_json(outbound)


async def _handle_vnc_proxy_request(message: dict[str, Any]) -> None:
    """Open a WebSocket to Proxmox vncwebsocket and relay frames to/from hub."""
    request_id = str(message.get("request_id") or "").strip()
    vmid = int(message.get("vmid") or 0)
    vmtype = str(message.get("vmtype") or "qemu").strip().lower()

    if not request_id or not vmid:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Missing request_id or vmid"})
        return

    proxmox_host = str(_proxmox_agent_vm_map.get(vmid) or proxmox_ws_hostname or "").strip()
    api_token = _get_proxmox_token_for_host(proxmox_host)

    if not proxmox_host:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Proxmox host unknown — no agent connected"})
        return
    if not api_token:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Proxmox API token not configured on spoke"})
        return

    # Ask Proxmox to create a VNC ticket via REST
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    node = proxmox_host.split(".")[0]
    vncproxy_url = f"https://{proxmox_host}:8006/api2/json/nodes/{node}/{vmtype}/{vmid}/vncproxy"
    headers = {"Authorization": f"PVEAPIToken={api_token}"}

    try:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(vncproxy_url, headers=headers, json={"websocket": 1}, timeout=10)
        if resp.status_code != 200:
            await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": f"Proxmox vncproxy returned {resp.status_code}: {resp.text[:200]}"})
            return
        body = resp.json()
        ticket = body["data"]["ticket"]
        port = body["data"]["port"]
    except Exception as exc:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": f"Proxmox vncproxy call failed: {exc}"})
        return

    # Register an inbound queue so browser→proxmox frames can be forwarded
    inbound_queue: asyncio.Queue = asyncio.Queue()
    _vnc_sessions[request_id] = inbound_queue

    import urllib.parse as _urlparse
    params = _urlparse.urlencode({"port": port, "vncticket": ticket})
    ws_path = f"/api2/json/nodes/{node}/{vmtype}/{vmid}/vncwebsocket?{params}"
    ws_url = f"wss://{proxmox_host}:8006{ws_path}"

    if websockets is None:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "websockets library not installed"})
        _vnc_sessions.pop(request_id, None)
        return

    try:
        connect_kwargs: dict[str, Any] = {
            "ssl": ssl_ctx,
            "open_timeout": 20,
            "max_size": None,
        }
        # Use correct keyword for this version of websockets
        import inspect as _inspect
        hdr_key = "additional_headers" if "additional_headers" in _inspect.signature(websockets.connect).parameters else "extra_headers"
        connect_kwargs[hdr_key] = headers

        await _relay_vnc_to_hub({"type": "vnc_proxy_response", "request_id": request_id})

        async with websockets.connect(ws_url, **connect_kwargs) as px_ws:

            async def proxmox_to_hub() -> None:
                async for raw in px_ws:
                    data = raw if isinstance(raw, bytes) else raw.encode()
                    await _relay_vnc_to_hub({
                        "type": "vnc_frame_to_browser",
                        "request_id": request_id,
                        "data": __import__("base64").b64encode(data).decode(),
                    })

            async def hub_to_proxmox() -> None:
                while True:
                    msg = await inbound_queue.get()
                    if msg is None:
                        break
                    raw = __import__("base64").b64decode(msg.get("data", ""))
                    await px_ws.send(raw)

            t1 = asyncio.create_task(proxmox_to_hub())
            t2 = asyncio.create_task(hub_to_proxmox())
            try:
                done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)
            finally:
                pass
    except Exception as exc:
        logger.warning("VNC relay error for request %s: %s", request_id, exc)
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": str(exc)})
    finally:
        _vnc_sessions.pop(request_id, None)
        await _relay_vnc_to_hub({"type": "vnc_disconnect", "request_id": request_id})


async def _handle_provision_proxmox_token(message: dict[str, Any]) -> None:
    """Auto-create a Proxmox API token via pvesh and report it back to the hub."""
    request_id = str(message.get("request_id") or "").strip()

    async def _send_error(error: str) -> None:
        await _relay_vnc_to_hub({"type": "proxmox_token_provision_error", "request_id": request_id, "error": error})

    # pvesh may not be in the systemd service PATH — check all common Proxmox locations.
    # Use os.path.isfile only (not os.access X_OK) since the service may run as a user
    # that lacks execute permission on the stat but can still exec via the kernel.
    pvesh_candidates = [
        shutil.which("pvesh"),
        "/usr/bin/pvesh",
        "/usr/sbin/pvesh",
        "/usr/local/bin/pvesh",
        "/usr/share/pve-manager/bin/pvesh",
        "/opt/proxmox/bin/pvesh",
    ]
    pvesh_path = next((c for c in pvesh_candidates if c and os.path.isfile(c)), None)
    if not pvesh_path:
        # Last resort: try running pvesh directly and let the OS sort out the path
        try:
            probe = await asyncio.create_subprocess_exec(
                "pvesh", "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(probe.wait(), timeout=5.0)
            if probe.returncode == 0:
                pvesh_path = "pvesh"
        except Exception:
            pass
    if not pvesh_path:
        # pvesh not available locally — try relaying to the Proxmox agent if connected.
        if proxmox_ws_connection is not None:
            logger.info("provision_proxmox_token: pvesh not found locally, relaying to proxmox agent")
            q: asyncio.Queue = asyncio.Queue(maxsize=1)
            _proxmox_token_provision_queues[request_id] = q
            try:
                await proxmox_ws_connection.send_json({
                    "type": "create_proxmox_token",
                    "request_id": request_id,
                })
                result = await asyncio.wait_for(q.get(), timeout=30.0)
                if result.get("ok"):
                    token = str(result.get("token") or "").strip()
                    settings["proxmox_api_token"] = token
                    _persisted["proxmox_api_token"] = token
                    _save_settings()
                    logger.info("Proxmox API token provisioned via agent: relaying to hub")
                    await _relay_vnc_to_hub({
                        "type": "proxmox_token_provisioned",
                        "request_id": request_id,
                        "token": token,
                    })
                else:
                    await _send_error(str(result.get("error") or "Agent failed to provision token"))
            except asyncio.TimeoutError:
                await _send_error("Proxmox agent did not respond to token creation request within 30 seconds")
            except Exception as exc:
                await _send_error(f"Failed to relay token request to agent: {exc}")
            finally:
                _proxmox_token_provision_queues.pop(request_id, None)
        else:
            await _send_error(
                "pvesh not found locally and no Proxmox agent is connected. "
                "Ensure the proxmox-agent.sh service is running on the Proxmox host."
            )
        return

    TOKEN_ID = "cs-hub"
    USER = "root@pam"
    logger.info("provision_proxmox_token: using pvesh at %s", pvesh_path)

    try:
        # Remove any existing token with this ID so we always get a fresh secret
        del_proc = await asyncio.create_subprocess_exec(
            pvesh_path, "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10.0)
    except Exception:
        pass  # Token may not exist yet — ignore

    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh_path, "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode != 0:
            await _send_error(f"pvesh create failed: {stderr.decode().strip()[:300]}")
            return

        data = json.loads(stdout.decode().strip())
        secret = str(data.get("value") or "").strip()
        if not secret:
            await _send_error("pvesh returned no token value in response")
            return

        full_token = f"{USER}!{TOKEN_ID}={secret}"
        settings["proxmox_api_token"] = full_token
        _persisted["proxmox_api_token"] = full_token
        _save_settings()
        logger.info("Proxmox API token auto-provisioned: %s!%s", USER, TOKEN_ID)

        await _relay_vnc_to_hub({"type": "proxmox_token_provisioned", "request_id": request_id, "token": full_token})

    except asyncio.TimeoutError:
        await _send_error("pvesh timed out after 15 seconds")
    except json.JSONDecodeError as exc:
        await _send_error(f"Could not parse pvesh output: {exc}")
    except Exception as exc:
        await _send_error(f"Unexpected error: {exc}")

async def _handle_log_fetch(message: dict[str, Any]) -> None:
    """Fetch log lines from journal/agent/watchdog/install and send back to hub."""
    request_id = str(message.get("request_id") or "").strip()
    source = str(message.get("source") or "journal").strip().lower()
    lines = min(max(int(message.get("lines") or 200), 10), 2000)

    if not request_id:
        return

    async def _send_response(log_lines: list[str], error: str | None = None) -> None:
        if _relay_ws_send_json is None:
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
        if _relay_ws_spoke_id:
            out["spoke_id"] = _relay_ws_spoke_id
        await _relay_ws_send_json(out)

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


async def _handle_command_trace_request(message: dict[str, Any]) -> None:
    """Send the command relay trace buffer back to the hub."""
    request_id = str(message.get("request_id") or "").strip()
    if not request_id or _relay_ws_send_json is None:
        return
    async with state_lock:
        cmds_snapshot = list(_serialize_commands())
    out: dict[str, Any] = {
        "type": "command_trace_response",
        "request_id": request_id,
        "agent_connected": proxmox_ws_connection is not None,
        "agent_hostname": proxmox_ws_hostname,
        "command_queue": cmds_snapshot,
        "trace": list(reversed(_command_trace)),
    }
    if _relay_ws_spoke_id:
        out["spoke_id"] = _relay_ws_spoke_id
    await _relay_ws_send_json(out)


async def _relay_shell_message(message: dict[str, Any]) -> None:
    if _relay_ws_send_json is None:
        raise RuntimeError("Hub relay is not connected")
    await _relay_ws_send_json(message)


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

    if notify_exit and _relay_ws_send_json is not None:
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
            global _hub_repo_sync_task
            already_running = _hub_repo_sync_task is not None and not _hub_repo_sync_task.done()
            if cmd_id:
                await ack_fn(cmd_id, "executed", {
                    "success": True,
                    "task_type": "repo_sync",
                    "detail": "Repo sync already in progress" if already_running else "Repo sync started",
                    "started": not already_running,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            if not already_running:
                _hub_repo_sync_task = asyncio.create_task(_run_hub_repo_sync())
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
            agent_connected = proxmox_ws_connection is not None if target == "proxmox" else bool(client_ws_connections.get(target))
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
    global relay_registration_refresh_needed
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

    if relay_registration_refresh_needed:
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
                global _hub_monitored_items
                mon_resp = await hc.get(f"{base}/monitored-items", headers=headers, timeout=10)
                if mon_resp.status_code == 200:
                    _hub_monitored_items = mon_resp.json()
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
            relay_registration_refresh_needed = True
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
    global relay_registration_refresh_needed, _relay_ws_send_json, _relay_ws_spoke_id
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

            if relay_registration_refresh_needed:
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

                _relay_ws_send_json = send_json
                _relay_ws_spoke_id = spoke_id
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
                    if _relay_ws_send_json is send_json:
                        _relay_ws_send_json = None
                        _relay_ws_spoke_id = None
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
                relay_registration_refresh_needed = True
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
                relay_registration_refresh_needed = True
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


def _push_to_github(files_changed: list[str], commit_message: str) -> bool:
    token = settings.get("github_token", "").strip()
    if not token:
        raise ValueError("GitHub token not configured")

    if not (REPO_DIR / ".git").exists():
        raise RuntimeError(f"{REPO_DIR} exists but is not a git repository")

    # Ensure git identity is set (required for commit)
    try:
        _git("config", "user.name")
    except RuntimeError:
        _git("config", "user.name", "Client Simulator")
    try:
        _git("config", "user.email")
    except RuntimeError:
        _git("config", "user.email", "client-sim@localhost")

    askpass_script = BASE_DIR / f".git-askpass-{uuid.uuid4().hex}.sh"
    askpass_script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *Password*) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    askpass_script.chmod(0o700)
    push_env = {
        "GIT_ASKPASS": str(askpass_script),
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_TOKEN": token,
    }

    _git("remote", "set-url", "origin", REPO_URL)
    try:
        _git("add", *files_changed)
        # Check if there is anything staged
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_DIR,
        )
        if status.returncode == 0:
            return False  # nothing staged
        _git("commit", "-m", commit_message)
        _git("push", env=push_env)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            askpass_script.unlink()
        _git("remote", "set-url", "origin", REPO_URL)


def _update_ini_section(filepath: Path, section: str, updates: dict[str, str]) -> None:
    text = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    normalized_updates = {str(key).strip(): str(value) for key, value in updates.items() if str(key).strip()}

    updated_lines: list[str] = []
    found_keys: set[str] = set()
    section_found = False
    in_target_section = False

    def append_missing_keys() -> None:
        for key, value in normalized_updates.items():
            if key not in found_keys:
                updated_lines.append(f"{key}={value}")

    for line in lines:
        match = re.match(r"^\s*\[(?P<section>[^\]]+)\]\s*$", line)
        if match:
            if in_target_section:
                append_missing_keys()
            current_section = match.group("section")
            in_target_section = current_section == section
            section_found = section_found or in_target_section
            updated_lines.append(line)
            continue

        if in_target_section:
            key_match = re.match(r"^(?P<indent>\s*)(?P<key>[^=\s#;][^=]*?)\s*=.*$", line)
            if key_match:
                key = key_match.group("key").strip()
                if key in normalized_updates:
                    updated_lines.append(f"{key_match.group('indent')}{key}={normalized_updates[key]}")
                    found_keys.add(key)
                    continue

        updated_lines.append(line)

    if in_target_section:
        append_missing_keys()

    if not section_found:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(f"[{section}]")
        append_missing_keys()

    output = newline.join(updated_lines)
    if updated_lines and (text.endswith("\n") or not text):
        output += newline
    filepath.write_text(output, encoding="utf-8")


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


async def _run_hub_repo_sync() -> dict[str, Any]:
    repo_version = await _sync_repo_now()
    output: dict[str, Any] = {"repo_version": repo_version}
    detail = f"Client-Sim repo synced{f' ({repo_version})' if repo_version else ''}"

    if approved_proxmox_agents:
        try:
            cmd = await _queue_proxmox_agent_update()
            output.update({
                "agent_command_id": cmd["id"],
                "agent_target": cmd["target"],
                "agent_branch": cmd.get("args", {}).get("branch"),
            })
            detail += f"; queued Proxmox agent update for {cmd['target']}"
        except HTTPException as exc:
            detail_msg = str(exc.detail or exc)
            if exc.status_code == 409 and "already queued" in detail_msg.lower():
                detail += f"; {detail_msg}"
            else:
                raise RuntimeError(detail_msg) from exc
    else:
        detail += "; no approved Proxmox agent available"

    return {
        "success": True,
        "task_type": "repo_sync",
        "detail": detail,
        "output": output,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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


async def check_for_update() -> None:
    """Background task: check for a new installer version every 24 hours with
    a random 2-hour jitter. Auto-applies the update when a new version is
    detected and the spoke is idle (no active reclone or reseed)."""
    import random
    # Spread initial check across first 2 hours to avoid update stampedes
    initial_jitter = random.uniform(0, 7200)
    logger.info("Update checker: first check in %.0f seconds", initial_jitter)
    await asyncio.sleep(initial_jitter)
    while True:
        try:
            available = await asyncio.to_thread(_get_repo_version)
            import datetime
            update_state["available_version"] = available
            update_state["last_checked"] = datetime.datetime.now().isoformat(timespec="seconds")
            update_state["update_available"] = (
                available is not None
                and available != update_state["current_version"]
            )
            logger.info(
                "Version check: installed=%s repo=%s update_available=%s",
                update_state["current_version"],
                available,
                update_state["update_available"],
            )
            _update_service_health("update_checker", ok=True)
            await _broadcast_update_state()

            # Auto-apply if update available and spoke is idle
            if (
                update_state["update_available"]
                and not update_state["update_in_progress"]
                and reclone_state.get("status") != "running"
                and not _proxmox_reseed_in_progress
            ):
                logger.info(
                    "Auto-update: new version %s available and spoke is idle — applying",
                    available,
                )
                asyncio.create_task(_run_self_update())
            elif update_state["update_available"]:
                logger.info(
                    "Auto-update: new version %s available but spoke is busy — will retry next cycle",
                    available,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("update_checker", ok=False, error=str(exc))
            logger.exception("Update checker error: %s", exc)
        # 24 hours + up to 2-hour jitter to prevent all spokes checking simultaneously
        await asyncio.sleep(UPDATE_CHECK_INTERVAL + random.uniform(0, 7200))




async def _run_update_all() -> None:
    """Queue the shared Proxmox update command, wait for its ACK, then self-update the WebUI."""
    global update_all_state

    approved = list(approved_proxmox_agents.keys())
    agent_cmd_ids: list[str] = []

    # ── Phase 1: Agent update ────────────────────────────────────────────────
    try:
        async with state_lock:
            update_args = _proxmox_update_args()
            for hostname in approved:
                cmd, _created, _expired, _purged = _enqueue_command_locked(hostname, "update_agent", dict(update_args))
                agent_cmd_ids.append(cmd["id"])

        update_all_state.update({
            "running": True,
            "phase": "agents" if agent_cmd_ids else "webui",
            "total_agents": len(agent_cmd_ids),
            "completed_agents": 0,
            "failed_agents": 0,
            "agent_cmds": agent_cmd_ids,
            "started_at": time.time(),
            "error": None,
        })
        await broadcast({"type": "update_all_progress", **update_all_state})
        await broadcast({"type": "commands_update", "commands": _serialize_commands()})
        await _push_pending_commands_for_targets(approved)

        if agent_cmd_ids:
            deadline = time.time() + 300
            while time.time() < deadline:
                await asyncio.sleep(5)
                async with state_lock:
                    command_statuses = {
                        c["id"]: c["status"]
                        for c in commands
                        if c["id"] in agent_cmd_ids
                    }
                done = sum(1 for status in command_statuses.values() if status in ("completed", "failed"))
                failed = sum(1 for status in command_statuses.values() if status == "failed")
                update_all_state["completed_agents"] = done
                update_all_state["failed_agents"] = failed
                await broadcast({"type": "update_all_progress", **update_all_state})
                if done >= len(agent_cmd_ids):
                    break
            else:
                logger.warning(
                    "Update All: agent ACK timed out after 300s — proceeding to WebUI update anyway"
                )

        if len(approved) == 0:
            logger.info("Update All: no approved agents, proceeding directly to WebUI update")
        else:
            logger.info(
                "Update All: agents done (%d/%d failed), proceeding to WebUI update",
                update_all_state["completed_agents"],
                update_all_state["failed_agents"],
            )
    except Exception as exc:
        logger.error("Update All: agent phase error (continuing to WebUI update): %s", exc)
        update_all_state["error"] = str(exc)

    # ── Phase 2: WebUI self-update ───────────────────────────────────────────
    try:
        update_all_state["phase"] = "webui"
        update_all_state["error"] = None
        await broadcast({"type": "update_all_progress", **update_all_state})

        # Sync the local repo cache before running the installer so it gets
        # the freshest content from GitHub (same as the /api/self-update path).
        try:
            async with _git_lock:
                await asyncio.to_thread(sync_repo_once)
            logger.info("Update All: repo synced before installer")
        except Exception as sync_exc:
            logger.warning("Update All: repo sync failed (%s) — installer will retry git fetch", sync_exc)

        await _run_self_update()
        if update_state.get("update_error"):
            update_all_state["phase"] = "failed"
            update_all_state["error"] = str(update_state["update_error"])
            logger.error("Update All: WebUI self-update failed: %s", update_state["update_error"])
        else:
            update_all_state["phase"] = "done"
    except Exception as exc:
        update_all_state["phase"] = "failed"
        update_all_state["error"] = str(exc)
        logger.error("Update All: WebUI phase error: %s", exc)
    finally:
        update_all_state["running"] = False
        await broadcast({"type": "update_all_progress", **update_all_state})


async def _run_self_update() -> None:
    """Re-run the installer from the synced repo. Systemd will restart the service."""
    if update_state["update_in_progress"]:
        return
    # Wait for any pending/delivered Proxmox agent commands (e.g. update_agent) to be
    # acked before restarting. Without this delay, the spoke restarts and loses the
    # in-memory command state before the agent has a chance to ack the command.
    _agent_update_wait_secs = 180
    _agent_update_deadline = time.time() + _agent_update_wait_secs
    while time.time() < _agent_update_deadline:
        async with state_lock:
            active = [c for c in commands if c.get("status") in ("pending", "delivered")]
        if not active:
            break
        logger.info(
            "Self-update: waiting for %d Proxmox agent command(s) to complete before restarting (%.0fs remaining)...",
            len(active),
            _agent_update_deadline - time.time(),
        )
        await asyncio.sleep(5)
    if not _INSTALLER_PATH.exists():
        msg = f"Self-update: installer not found at {_INSTALLER_PATH}"
        logger.error(msg)
        update_state["update_error"] = msg
        await _broadcast_update_state()
        return
    update_state["update_in_progress"] = True
    update_state["update_log"] = []
    update_state["update_error"] = None
    await _broadcast_update_state()
    try:
        import shlex as _shlex, os as _os
        # Use create_subprocess_shell so /bin/sh resolves bash via its own PATH.
        # This is more robust than exec when systemd strips the PATH env.
        full_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        installer = _shlex.quote(str(_INSTALLER_PATH))
        # Pass --branch and --port so the bootstrap step can curl the right branch.
        _branch = _shlex.quote(os.environ.get("REPO_BRANCH", "main"))
        _port   = _shlex.quote(os.environ.get("PORT", "8000"))
        _base     = f'/bin/bash {installer} --branch {_branch} --port {_port}'
        shell_cmd = _base if _os.geteuid() == 0 else f'sudo -n /bin/bash {installer} --branch {_branch} --port {_port}'
        logger.info("Self-update: shell_cmd=%s", shell_cmd)
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PATH": full_path},
            start_new_session=True,  # detach from server's process group so SIGTERM on restart doesn't kill installer
        )
        assert proc.stdout is not None
        _ansi_re = re.compile(r'\x1b(?:\[[0-9;]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[^[\]])')
        async for raw in proc.stdout:
            line = _ansi_re.sub('', raw.decode(errors="replace")).rstrip()
            update_state["update_log"].append(line)
            logger.info("self-update: %s", line)
            await _broadcast_update_state()
        await proc.wait()
        # -15 (SIGTERM) is expected when the installer schedules a deferred
        # `systemctl restart` and asyncio cleans up the subprocess transport
        # when the server is stopped.  If the restart step already ran, treat
        # it as success rather than surfacing a misleading error.
        restart_triggered = any(
            "Restarting client-sim-dashboard" in l for l in update_state["update_log"]
        )
        if proc.returncode != 0 and not (proc.returncode == -15 and restart_triggered):
            logger.error("Self-update installer exited with code %s", proc.returncode)
            update_state["update_in_progress"] = False
            update_state["update_error"] = f"Installer exited with code {proc.returncode} — check logs"
            await _broadcast_update_state()
        else:
            logger.info("Self-update installer completed successfully (rc=%s)", proc.returncode)
            update_state["update_in_progress"] = False
            update_state["update_error"] = None
            await _broadcast_update_state()
    except Exception as exc:
        logger.exception("Self-update failed")
        update_state["update_in_progress"] = False
        update_state["update_error"] = str(exc)
        update_state["update_log"].append(f"ERROR: {exc}")
        await _broadcast_update_state()




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


@app.get("/api/auth/check")
async def spoke_auth_check(request: Request):
    auth_required = _spoke_auth_required()
    if not auth_required:
        return {
            "auth_required": False,
            "authenticated": True,
            "username": "admin",
            "role": "admin",
            "auth_provider": "local",
        }
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    user = _validate_spoke_session(token)
    return {
        "auth_required": True,
        "authenticated": bool(user),
        "username": user.username if user else "",
        "role": user.role if user else "",
        "auth_provider": user.auth_provider if user else _normalize_spoke_auth_provider(settings.get("auth_provider", "local")),
    }


@app.post("/api/auth/login")
async def spoke_auth_login(payload: _SpokeLoginRequest):
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    provider = _normalize_spoke_auth_provider(settings.get("auth_provider", "local"))
    user: SpokeUser | None = None

    if not _spoke_auth_required():
        user = SpokeUser(username=username or "admin", role="admin", auth_provider="local")
    elif provider == "ldap" and username and password:
        user = await _ldap_authenticate(username, password)
    elif provider == "radius" and username and password:
        user = await _radius_authenticate(username, password)
    elif provider == "tacacs" and username and password:
        user = await _tacacs_authenticate(username, password)

    if user is None:
        user = _check_credentials(username, password)

    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_spoke_session(user)
    resp = JSONResponse({"ok": True, "role": user.role, "username": user.username})
    resp.set_cookie(_SPOKE_SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=_get_session_ttl())
    return resp


@app.post("/api/auth/logout")
async def spoke_auth_logout(request: Request):
    token = request.cookies.get(_SPOKE_SESSION_COOKIE, "")
    if token:
        _spoke_sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SPOKE_SESSION_COOKIE)
    return resp


@app.post("/api/auth/change-password")
async def change_password(payload: ChangePasswordPayload, user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    current_password = str(payload.current_password or "")
    new_password = str(payload.new_password or "").strip()
    stored_password = _admin_password()
    if stored_password:
        # A password is already set — require the current one to change it.
        if not current_password:
            raise HTTPException(status_code=401, detail="Current password is required.")
        if not secrets.compare_digest(current_password.strip(), stored_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if not new_password:
        raise HTTPException(status_code=422, detail="New password is required")
    settings["admin_password"] = new_password
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}


@app.get("/api/auth/local-users")
async def list_local_users(user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    users = [{"username": "admin", "role": "admin"}]
    users.extend({"username": entry["username"], "role": entry["role"]} for entry in _get_local_users())
    return users


@app.post("/api/auth/local-users")
async def create_local_user(payload: LocalUserCreatePayload, user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    role_raw = str(payload.role or "admin").strip().lower()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account already exists")
    if not password:
        raise HTTPException(status_code=422, detail="Password is required")
    if role_raw not in _LOCAL_USER_ROLES:
        raise HTTPException(status_code=422, detail="Role must be admin, viewer, or demo")

    users = _get_local_users()
    if any(str(entry.get("username", "")).strip().lower() == username.lower() for entry in users):
        raise HTTPException(status_code=409, detail="User already exists")

    users.append({
        "username": username,
        "password_hash": _hash_local_password(password),
        "role": role_raw,
    })
    settings["local_users"] = users
    _save_settings()
    return {"ok": True}


@app.delete("/api/auth/local-users/{username}")
async def delete_local_user(username: str, user: SpokeUser = Depends(require_auth)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    username = str(username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="The primary admin account cannot be deleted")
    if username.lower() == user.username.lower():
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    users = _get_local_users()
    remaining = [entry for entry in users if str(entry.get("username", "")).strip().lower() != username.lower()]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail="User not found")

    settings["local_users"] = remaining
    _save_settings()
    _spoke_sessions.clear()
    return {"ok": True}


@app.post("/api/auth/test")
async def test_auth_provider(payload: dict, request: Request):
    """Test auth provider connectivity (admin only)."""
    user = _validate_spoke_session(request.cookies.get(_SPOKE_SESSION_COOKIE, ""))
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin required")

    provider = str(payload.get("provider", settings.get("auth_provider", "local")) or "local").strip().lower()
    if provider == "ldap":
        try:
            from ldap3 import ALL, Connection, Server

            def _ldap_probe() -> None:
                srv = Server(settings["auth_ldap_url"], get_info=ALL)
                with Connection(srv, user=settings["auth_ldap_bind_dn"], password=settings["auth_ldap_bind_password"], auto_bind=True):
                    pass
            await asyncio.to_thread(_ldap_probe)
            return {"ok": True, "detail": f"Connected to {settings['auth_ldap_url']}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    if provider == "radius":
        return {"ok": True, "detail": "RADIUS: send a test login to verify"}
    if provider == "tacacs":
        try:
            def _tacacs_probe() -> None:
                s = socket.create_connection(
                    (settings["auth_tacacs_host"], int(settings.get("auth_tacacs_port", 49))),
                    timeout=5,
                )
                s.close()
            await asyncio.to_thread(_tacacs_probe)
            return {"ok": True, "detail": f"TCP connection to {settings['auth_tacacs_host']}:{settings.get('auth_tacacs_port', 49)} OK"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": "Local auth — no external connectivity needed"}


@app.get("/api/settings")
async def api_settings_get() -> dict[str, Any]:
    return _public_settings()


@app.post("/api/bootstrap")
async def api_bootstrap(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, str]:
    """One-time hub configuration — only accepted from localhost (via qm guest exec / pct exec).

    Security model:
      - Only 127.0.0.1 / ::1 can call this endpoint — enforced server-side.
      - If relay_server_url is already configured the endpoint returns 409 (idempotent lock).
      - The installer invokes this via `qm guest exec` / `pct exec` so the request never
        crosses the network; an external caller cannot reach it.
    """
    global relay_registration_refresh_needed

    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning("Bootstrap attempt rejected from non-localhost %s", client_host)
        raise HTTPException(status_code=403, detail="bootstrap only accepted from localhost")

    if settings.get("relay_server_url", "").strip():
        raise HTTPException(status_code=409, detail="hub already configured — bootstrap is one-time only")

    hub_url = str(body.get("relay_server_url", "") or "").strip()
    tenant_id = str(body.get("relay_tenant_id", "") or "").strip()
    onboarding_psk = str(body.get("relay_onboarding_psk", "") or "").strip()

    if not hub_url:
        raise HTTPException(status_code=422, detail="relay_server_url is required")

    settings["relay_server_url"] = hub_url
    if tenant_id:
        settings["relay_tenant_id"] = tenant_id
        settings["relay_tenant_hint"] = tenant_id
    if onboarding_psk:
        settings["relay_onboarding_psk"] = onboarding_psk
    settings["relay_enabled"] = "on"
    _save_settings()

    relay_state.update({
        "enabled": True,
        "connected": False,
        "error": None,
        "registration_status": _relay_registration_status_from_settings(),
    })
    relay_registration_refresh_needed = True
    _save_relay_state()
    task = background_tasks.get("relay")
    if task and not task.done():
        task.cancel()
    background_tasks["relay"] = asyncio.get_event_loop().create_task(relay_loop())

    logger.info("Bootstrap: hub configured to %s (tenant: %s)", hub_url, tenant_id or "none")
    return {"status": "ok", "relay_server_url": hub_url, "relay_tenant_id": tenant_id, "has_psk": bool(onboarding_psk)}


@app.post("/api/settings")
async def api_settings_update(update: SettingsUpdate) -> dict[str, Any]:
    global relay_registration_refresh_needed
    changed_branch = False
    relay_config_changed = False
    auth_provider_changed = False
    autoprov_disabled = False
    update_data = update.model_dump(exclude_none=True)

    if settings.get("hub_managed"):
        non_relay = set(update_data.keys()) - HUB_LOCAL_ALLOWED_KEYS
        if non_relay:
            raise HTTPException(status_code=403, detail="Settings are hub-managed. Only relay settings can be changed locally.")

    if update.repo_branch is not None:
        branch = update.repo_branch.strip()
        if not branch or not re.match(r'^[a-zA-Z0-9._/\-]+$', branch):
            raise HTTPException(status_code=422, detail="Invalid branch name")
        settings["repo_branch"] = branch
        changed_branch = True

    if update.github_token is not None:
        settings["github_token"] = update.github_token.strip()

    if update.relay_server_url is not None:
        settings["relay_server_url"] = update.relay_server_url.strip()
        relay_config_changed = True

    if update.hub_tls_verify is not None:
        settings["hub_tls_verify"] = _normalize_relay_enabled(update.hub_tls_verify)
        relay_config_changed = True

    if update.relay_spoke_name is not None:
        settings["relay_spoke_name"] = update.relay_spoke_name.strip()
        relay_config_changed = True

    if update.relay_tenant_hint is not None:
        tenant_id = update.relay_tenant_hint.strip()
        settings["relay_tenant_hint"] = tenant_id
        settings["relay_tenant_id"] = tenant_id
        relay_config_changed = True

    if update.relay_api_key is not None:
        settings["relay_api_key"] = update.relay_api_key.strip()
        relay_config_changed = True

    if update.relay_spoke_id is not None:
        settings["relay_spoke_id"] = update.relay_spoke_id.strip()
        relay_config_changed = True

    if update.relay_tenant_id is not None:
        tenant_id = update.relay_tenant_id.strip()
        settings["relay_tenant_id"] = tenant_id
        settings["relay_tenant_hint"] = tenant_id
        relay_config_changed = True

    if update.relay_enabled is not None:
        settings["relay_enabled"] = _normalize_relay_enabled(update.relay_enabled)
        relay_config_changed = True

    if update.relay_poll_interval is not None:
        settings["relay_poll_interval"] = _clamp_relay_interval(update.relay_poll_interval)
        relay_config_changed = True

    if update.hub_isolation_timeout is not None:  # Accept timeout edits from the setup UI so operators can tune when stale hub contact pauses pushes.
        settings["hub_isolation_timeout"] = max(300, min(86400, int(update.hub_isolation_timeout)))  # Clamp the safeguard window so operators stay within the supported 5-minute to 24-hour range.
        relay_config_changed = True  # Treat timeout edits as relay changes so isolation status is re-broadcast immediately when the threshold moves.
        _save_settings()  # Persist the new timeout right away so the safeguard survives crashes even before the handler reaches its shared save call.

    if update.admin_password is not None:
        settings["admin_password"] = update.admin_password.strip()
        _spoke_sessions.clear()

    if update.session_timeout_minutes is not None:
        settings["session_timeout_minutes"] = max(5, min(1440, int(update.session_timeout_minutes)))

    if update.auth_provider is not None:
        next_provider = _normalize_spoke_auth_provider(update.auth_provider)
        if next_provider != _normalize_spoke_auth_provider(settings.get("auth_provider", "local")):
            auth_provider_changed = True
        settings["auth_provider"] = next_provider

    for key in (
        "auth_ldap_url",
        "auth_ldap_bind_dn",
        "auth_ldap_bind_password",
        "auth_ldap_user_base",
        "auth_ldap_user_filter",
        "auth_ldap_group_admin",
        "auth_ldap_group_viewer",
        "auth_radius_host",
        "auth_radius_secret",
        "auth_radius_role_attr",
        "auth_radius_admin_val",
        "auth_tacacs_host",
        "auth_tacacs_secret",
    ):
        value = getattr(update, key)
        if value is not None:
            settings[key] = str(value).strip()

    if update.auth_radius_port is not None:
        settings["auth_radius_port"] = max(1, min(65535, int(update.auth_radius_port)))

    if update.auth_tacacs_port is not None:
        settings["auth_tacacs_port"] = max(1, min(65535, int(update.auth_tacacs_port)))

    if update.auth_tacacs_admin_priv is not None:
        settings["auth_tacacs_admin_priv"] = max(0, int(update.auth_tacacs_admin_priv))

    if auth_provider_changed:
        _spoke_sessions.clear()

    if relay_config_changed:
        relay_state.update({
            "enabled": settings.get("relay_enabled") == "on" and bool(settings.get("relay_server_url")),
            "connected": False,
            "error": None,
            "registration_status": _relay_registration_status_from_settings(),
        })
        relay_registration_refresh_needed = bool(relay_state["enabled"])
        _save_relay_state()
        # Kick the relay loop immediately instead of waiting for the next poll interval
        task = background_tasks.get("relay")
        if task and not task.done():
            task.cancel()
        background_tasks["relay"] = asyncio.get_event_loop().create_task(relay_loop())

    if update.central_api is not None:
        merged_api = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
        mode = str(update.central_api.get("mode", merged_api.get("mode", "classic"))).strip().lower()
        if mode not in {"classic", "central"}:
            raise HTTPException(status_code=422, detail="central_api.mode must be 'classic' or 'central'")
        merged_api["mode"] = mode

        classic_update = update.central_api.get("classic")
        if isinstance(classic_update, dict):
            for key in ("url", "username"):
                if key in classic_update:
                    merged_api["classic"][key] = str(classic_update.get(key, "")).strip()
            if "password" in classic_update:
                merged_api["classic"]["password"] = str(classic_update.get("password", ""))

        central_update = update.central_api.get("central")
        if isinstance(central_update, dict):
            for key in ("url", "client_id", "customer_id"):
                if key in central_update:
                    merged_api["central"][key] = str(central_update.get(key, "")).strip()
            if "client_secret" in central_update:
                merged_api["central"]["client_secret"] = str(central_update.get("client_secret", ""))

        settings["central_api"] = merged_api
        _sync_central_runtime_config()

    if update.central_config is not None:
        merged = dict(settings["central_config"])
        # Only update keys that are explicitly provided so omitted secrets are preserved.
        for key in ("cluster_url", "client_id", "customer_id", "api_version"):
            if key in update.central_config:
                merged[key] = update.central_config[key].strip()
        for secret_key in ("client_secret", "access_token", "refresh_token"):
            if secret_key in update.central_config:
                merged[secret_key] = update.central_config.get(secret_key, "").strip()
        # Switching to New Central — clear stale classic tokens from runtime
        if merged.get("api_version") == "new_central":
            central_token["access_token"] = None
            central_token["refresh_token"] = None
            central_token["expires_at"] = 0.0
        settings["central_config"] = merged
        merged_api = _normalize_central_api_settings(settings.get("central_api", {}), merged)
        if merged.get("api_version") == "new_central":
            merged_api["mode"] = "central"
            merged_api["central"].update({
                "url": merged.get("cluster_url", "").strip(),
                "client_id": merged.get("client_id", "").strip(),
                "client_secret": merged.get("client_secret", ""),
                "customer_id": merged.get("customer_id", "").strip(),
            })
        settings["central_api"] = merged_api
        # Classic: load new tokens into runtime state immediately
        if merged.get("api_version", "classic") == "classic":
            central_token["access_token"] = merged.get("access_token") or None
            central_token["refresh_token"] = merged.get("refresh_token") or None
            central_token["expires_at"] = time.time() + 7200 if merged.get("access_token") else 0.0

    if update.site_mappings is not None:
        settings["site_mappings"] = {k.strip(): v.strip() for k, v in update.site_mappings.items() if k.strip()}

    if update.monitored_checks is not None:
        settings["monitored_checks"] = [
            {"type": c.get("type", ""), "id": c.get("id", ""), "name": c.get("name", c.get("id", ""))}
            for c in update.monitored_checks
            if c.get("type") and c.get("id")
        ]

    if update.hardware_checks is not None:
        settings["hardware_checks"] = [
            {
                "id": c.get("id", ""),
                "name": c.get("name") or _HW_FRIENDLY.get(c.get("id", ""), c.get("id", "")),
                "device_type": c.get("device_type") or _auto_device_type(c.get("id", "")),
            }
            for c in update.hardware_checks
            if c.get("id")
        ]

    if update.notifications is not None:
        merged_notif = dict(settings.get("notifications", {}))
        merged_notif.update(update.notifications)
        # Ensure smtp_to is always a list
        if isinstance(merged_notif.get("smtp_to"), str):
            merged_notif["smtp_to"] = [a.strip() for a in merged_notif["smtp_to"].split(",") if a.strip()]
        settings["notifications"] = merged_notif

    if update.repo_sync_interval is not None:
        interval = max(60, min(86400, update.repo_sync_interval))  # clamp 1min–24hr
        settings["repo_sync_interval"] = interval

    if update.usb_vidpids is not None:
        settings["usb_vidpids"] = _ensure_json_list(update.usb_vidpids.strip(), "usb_vidpids")

    if update.usb_missing_timeout is not None:
        settings["usb_missing_timeout"] = str(max(1, int(update.usb_missing_timeout.strip() or "60")))

    if any(value is not None for value in (
        update.usb_template_id,
        update.vm_image_1_template_id,
        update.vm_image_1_template_spec,
        update.vm_image_2_template_id,
        update.vm_image_2_template_spec,
    )):
        spec1_raw = update.vm_image_1_template_spec
        if spec1_raw is None:
            spec1_raw = update.vm_image_1_template_id
        if spec1_raw is None:
            spec1_raw = update.usb_template_id
        spec2_raw = update.vm_image_2_template_spec
        if spec2_raw is None:
            spec2_raw = update.vm_image_2_template_id

        try:
            spec1 = _resolved_template_spec(settings, 1) if spec1_raw is None else _normalize_vmid_spec(spec1_raw, field_name="vm_image_1_template_spec")
            spec2 = _resolved_template_spec(settings, 2) if spec2_raw is None else _normalize_vmid_spec(spec2_raw, field_name="vm_image_2_template_spec")
            _validate_template_specs(spec1, spec2)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        settings["vm_image_1_template_spec"] = spec1
        settings["vm_image_2_template_spec"] = spec2
        settings["vm_image_1_template_id"] = _primary_template_id(spec1, _legacy_template_id(settings, 1))
        settings["vm_image_2_template_id"] = _primary_template_id(spec2, _legacy_template_id(settings, 2))

    if update.vm_image_1_pct is not None:
        settings["vm_image_1_pct"] = str(max(0, min(100, int(update.vm_image_1_pct.strip() or "50"))))

    if update.usb_auto_provision is not None:
        settings["usb_auto_provision"] = _normalize_toggle(update.usb_auto_provision)
        autoprov_disabled = settings["usb_auto_provision"] != "on"

    if update.use_all_dongles is not None:
        settings["use_all_dongles"] = bool(update.use_all_dongles)  # validated: always boolean

    if update.usb_max_slots is not None:
        settings["usb_max_slots"] = str(max(1, min(256, int(update.usb_max_slots.strip() or "24"))))

    def _clamp_pct(val: str, default: str) -> str:
        return str(max(0, min(100, int(val.strip() or default))))

    if update.cpu_provision_threshold is not None:
        settings["cpu_provision_threshold"] = _clamp_pct(update.cpu_provision_threshold, "80")
    if update.cpu_delete_threshold is not None:
        settings["cpu_delete_threshold"] = _clamp_pct(update.cpu_delete_threshold, "90")
    if update.mem_provision_threshold is not None:
        settings["mem_provision_threshold"] = _clamp_pct(update.mem_provision_threshold, "80")
    if update.mem_delete_threshold is not None:
        settings["mem_delete_threshold"] = _clamp_pct(update.mem_delete_threshold, "90")

    if update.vmid_start is not None:
        settings["vmid_start"] = max(0, int(update.vmid_start))

    if update.usb_ignored_vidpids is not None:
        settings["usb_ignored_vidpids"] = _ensure_json_list(update.usb_ignored_vidpids.strip(), "usb_ignored_vidpids")

    if update.ignored_hostnames is not None:
        settings["ignored_hostnames"] = _ensure_json_list(update.ignored_hostnames.strip(), "ignored_hostnames")

    if update.vm_silent_timeout is not None:
        settings["vm_silent_timeout"] = str(max(1, int(update.vm_silent_timeout.strip() or "24")))

    if update.reclone_schedule_enabled is not None:
        settings["reclone_schedule_enabled"] = _normalize_toggle(update.reclone_schedule_enabled)

    if update.reclone_schedule_cron is not None:
        cron_value = update.reclone_schedule_cron.strip().lower() or "sunday 02:00"
        if _parse_reclone_schedule(cron_value) is None:
            raise HTTPException(status_code=422, detail="reclone_schedule_cron must be in '<day> HH:MM' format")
        settings["reclone_schedule_cron"] = cron_value

    if update.reclone_concurrency is not None:
        settings["reclone_concurrency"] = str(max(1, int(update.reclone_concurrency.strip() or "1")))

    if update.protected_vmids is not None:
        # Normalize to a clean comma-separated list of ints and ranges (e.g. "101, 100-90000")
        raw = str(update.protected_vmids or "")
        parsed_strs = []
        for entry in _parse_protected_vmids(raw):
            if isinstance(entry, tuple):
                lo, hi = entry
                parsed_strs.append(f"{lo}-{hi}")
            else:
                parsed_strs.append(str(entry))
        settings["protected_vmids"] = ", ".join(parsed_strs)

    if update.l1_vlan_start is not None:
        settings["l1_vlan_start"] = str(max(1, min(4094, int(update.l1_vlan_start.strip() or "100"))))

    if update.l1_vlan_end is not None:
        settings["l1_vlan_end"] = str(max(1, min(4094, int(update.l1_vlan_end.strip() or "199"))))

    if update.guest_agent_watchdog_enabled is not None:
        settings["guest_agent_watchdog_enabled"] = _normalize_toggle(update.guest_agent_watchdog_enabled)
    if update.guest_agent_grace_minutes is not None:
        settings["guest_agent_grace_minutes"] = str(max(1, int(update.guest_agent_grace_minutes.strip() or "20")))
    if update.guest_agent_check_interval_minutes is not None:
        settings["guest_agent_check_interval_minutes"] = str(max(1, int(update.guest_agent_check_interval_minutes.strip() or "10")))
    if update.guest_agent_reboot_after_minutes is not None:
        settings["guest_agent_reboot_after_minutes"] = str(max(1, int(update.guest_agent_reboot_after_minutes.strip() or "10")))
    if update.guest_agent_reclone_after_minutes is not None:
        settings["guest_agent_reclone_after_minutes"] = str(max(1, int(update.guest_agent_reclone_after_minutes.strip() or "30")))
    if update.watchdog_reboot_enabled is not None:
        settings["watchdog_reboot_enabled"] = _normalize_toggle(update.watchdog_reboot_enabled)

    if update.proxmox_api_token is not None:
        token = update.proxmox_api_token.strip()
        settings["proxmox_api_token"] = token
        _persisted["proxmox_api_token"] = token

    if update.spoke_tls is not None:
        settings["spoke_tls"] = _normalize_toggle(update.spoke_tls)

    _save_settings()

    if autoprov_disabled:
        _clear_provision_halt_state()

    if changed_branch:
        if "sync_repo" in background_tasks:
            background_tasks["sync_repo"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_tasks["sync_repo"]
        background_tasks["sync_repo"] = asyncio.create_task(sync_repo())

    # Re-filter unknown_usb immediately so subsequent proxmox_update broadcasts don't
    # restore devices the user just certified or ignored.
    if update.usb_vidpids is not None or update.usb_ignored_vidpids is not None:
        _new_certified: set[str] = {
            str(item.get("vidpid", "")).strip().lower()
            for item in _parse_json_list(settings.get("usb_vidpids", "[]"))
            if isinstance(item, dict) and item.get("vidpid")
        }
        _new_ignored: set[str] = {
            str(v).strip().lower()
            for v in _parse_json_list(settings.get("usb_ignored_vidpids", "[]"))
            if str(v).strip()
        }
        _exclude = _new_certified | _new_ignored
        proxmox_state["unknown_usb"] = [
            d for d in proxmox_state.get("unknown_usb", [])
            if str(d.get("vidpid", "")).strip()
            and str(d.get("vidpid", "")).strip().lower() not in _exclude
        ]

    payload = await api_settings_get()
    await broadcast({"type": "settings_update", "settings": payload})
    if autoprov_disabled:
        await _broadcast_proxmox_state()
    if relay_config_changed:
        await _broadcast_relay_state()
    return {"status": "ok", "settings": payload}


@app.get("/api/test-github")
async def api_test_github() -> dict[str, Any]:
    """Validate the stored GitHub token against the GitHub API."""
    token = settings.get("github_token", "").strip()
    if not token:
        return {"valid": False, "error": "No GitHub token configured"}
    if httpx is None:
        return {"valid": False, "error": "httpx not available on server"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return {"valid": True, "username": data.get("login", ""), "error": None}
        elif resp.status_code == 401:
            return {"valid": False, "error": "Token is invalid or expired"}
        else:
            return {"valid": False, "error": f"GitHub returned HTTP {resp.status_code}"}
    except Exception as exc:
        return {"valid": False, "error": f"Request failed: {exc}"}


@app.post("/api/settings/clear/{provider}")
async def api_settings_clear(provider: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    provider_key = provider.strip().lower()
    changed_branch = False
    relay_config_changed = False
    relay_payload: dict[str, Any] | None = None

    if provider_key == "github":
        changed_branch = bool(settings.get("repo_branch"))
        settings["repo_branch"] = ""
        settings["github_token"] = ""
    elif provider_key == "relay":
        settings.update({
            "relay_enabled": "off",
            "relay_server_url": "",
            "hub_tls_verify": "off",
            "relay_spoke_name": "",
            "relay_tenant_hint": "",
            "relay_api_key": "",
            "relay_spoke_id": "",
            "relay_tenant_id": "",
            "relay_poll_interval": RELAY_INTERVAL_DEFAULT,
        })
        relay_state.update({
            "enabled": False,
            "connected": False,
            "last_sync": None,
            "error": None,
            "registration_status": "unregistered",
            "api_key_configured": bool(settings.get("relay_api_key")),
        })
        relay_registration_refresh_needed = False
        _save_relay_state()
        relay_config_changed = True
        relay_payload = _relay_status_payload()
    elif provider_key == "central":
        requested_mode = str((payload or {}).get("mode") or settings.get("central_api", {}).get("mode", "classic")).strip().lower()
        if requested_mode not in {"classic", "central"}:
            raise HTTPException(status_code=422, detail="mode must be 'classic' or 'central'")
        central_api_cfg = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
        central_api_cfg["mode"] = requested_mode
        if requested_mode == "classic":
            central_api_cfg["classic"] = {"url": "", "username": "", "password": ""}
        else:
            central_api_cfg["central"] = {"url": "", "client_id": "", "client_secret": "", "customer_id": ""}
        settings["central_api"] = central_api_cfg
        _sync_central_runtime_config()
    else:
        raise HTTPException(status_code=404, detail=f"Unknown settings provider: {provider}")

    _save_settings()

    if changed_branch:
        if "sync_repo" in background_tasks:
            background_tasks["sync_repo"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_tasks["sync_repo"]
        background_tasks["sync_repo"] = asyncio.create_task(sync_repo())

    payload = await api_settings_get()
    await broadcast({"type": "settings_update", "settings": payload})
    if relay_config_changed and relay_payload is not None:
        await _broadcast_relay_state()

    response: dict[str, Any] = {"status": "ok", "provider": provider_key, "settings": payload}
    if relay_payload is not None:
        response["relay_status"] = relay_payload
    return response


@app.post("/api/relay/trigger")
async def api_relay_trigger() -> dict[str, Any]:
    """Manually trigger an immediate relay sync."""
    if settings.get("relay_enabled") != "on":
        raise HTTPException(status_code=400, detail="Relay is not enabled")
    if not settings.get("relay_server_url"):
        raise HTTPException(status_code=400, detail="Relay server URL not configured")
    asyncio.create_task(relay_sync_once())
    return {"status": "ok", "message": "Relay sync triggered"}


@app.post("/api/relay/ingest")
async def api_relay_ingest(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Accept a site snapshot from a remote WebUI acting as a relay agent."""
    site_id = payload.get("site_id")
    if not site_id:
        raise HTTPException(status_code=422, detail="site_id is required")
    tenant_id = payload.get("tenant_id") or "__untenanted__"
    async with state_lock:
        relay_sites.setdefault(tenant_id, {})[site_id] = {**payload, "ingested_at": time.time()}
    await broadcast({
        "type": "relay_ingest",
        "tenant_id": tenant_id,
        "site_id": site_id,
        "client_count": payload.get("client_count", 0),
        "timestamp": payload.get("timestamp"),
    })
    logger.info("Ingested relay snapshot from tenant=%s site=%s (%d clients)", tenant_id, site_id, payload.get("client_count", 0))
    return {"status": "ok", "tenant_id": tenant_id, "site_id": site_id}


@app.get("/api/relay/sites")
async def api_relay_sites(tenant_id: str | None = Query(None)) -> dict[str, Any]:
    """Return ingested site snapshots. Optionally filter by tenant_id."""
    async with state_lock:
        if tenant_id:
            sites = list(relay_sites.get(tenant_id, {}).values())
        else:
            # Return all tenants flattened
            sites = [site for tenant in relay_sites.values() for site in tenant.values()]
    return {"sites": sites, "tenant_id": tenant_id}


@app.get("/api/repo/status")
async def api_repo_status() -> dict[str, Any]:
    return {
        "synced": repo_state.get("synced", False),
        "error": repo_state.get("error"),
        "last_sync": repo_state.get("last_sync"),
        "repo_version": _repo_ver,
    }


@app.get("/api/relay/status")
async def api_relay_status_endpoint() -> dict[str, Any]:  # Serve the enriched relay payload so on-demand status checks match websocket broadcasts exactly.
    return _relay_status_payload()  # Reuse the shared relay payload so the REST endpoint matches broadcasted isolation, spoke, and check-in fields exactly.


@app.get("/api/relay/monitored-items")
async def api_relay_monitored_items() -> dict[str, Any]:
    """Return hub-synced monitored items for this spoke (fetched each relay cycle)."""
    return _hub_monitored_items


@app.post("/api/relay/revert-local")
async def api_relay_revert_local() -> dict[str, Any]:
    """Immediately revert hub_managed to False, restoring local control.

    Use when the hub tenant has been deleted or the hub is permanently unreachable.
    The spoke will stop accepting hub config pushes and allow local settings changes.
    """
    was_managed = bool(settings.get("hub_managed"))
    settings["hub_managed"] = False
    settings["relay_api_key"] = ""
    settings["relay_tenant_id"] = ""
    _save_settings()
    await _broadcast_relay_state()
    await broadcast({"type": "settings_update", "settings": _public_settings()})
    logger.info("hub_managed manually reverted to local control by operator")
    _relay_diag_append("hub_managed_reverted", status_code=None, reason="manual operator revert via /api/relay/revert-local")
    return {"status": "ok", "was_managed": was_managed, "message": "Reverted to local control — hub_managed cleared"}


@app.get("/api/relay/diag")
async def api_relay_diag() -> dict[str, Any]:
    """Return registration diagnostics: config summary, live hub reachability, and registration log."""
    server_url = settings.get("relay_server_url", "").rstrip("/")
    hostname = socket.gethostname()

    # Live reachability check — use just the base URL (scheme+host+port), not the tenant path
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(server_url) if server_url else None
    hub_base_url = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed and _parsed.netloc else server_url
    reachability: dict[str, Any] = {"tested_url": server_url or "(not set)", "ok": False, "detail": ""}
    if server_url:
        try:
            async with httpx.AsyncClient(timeout=8, verify=_hub_tls_verify()) as hc:
                r = await hc.get(f"{hub_base_url}/api/health")
                reachability = {
                    "tested_url": f"{hub_base_url}/api/health",
                    "ok": r.status_code < 400,
                    "http_status": r.status_code,
                    "detail": r.text[:200],
                }
        except Exception as exc:
            reachability = {
                "tested_url": f"{hub_base_url}/api/health",
                "ok": False,
                "detail": str(exc),
            }
    else:
        reachability["detail"] = "Server URL not configured"

    config_check = {
        "relay_enabled": settings.get("relay_enabled", "off"),
        "hub_tls_verify": settings.get("hub_tls_verify", "off"),
        "server_url": server_url or "(not set)",
        "spoke_name": settings.get("relay_spoke_name", "") or "(not set — will use hostname)",
        "hostname": hostname,
        "spoke_id": settings.get("relay_spoke_id", "") or "(none)",
        "api_key_configured": bool(settings.get("relay_api_key")),
        "tenant_id": settings.get("relay_tenant_id", "") or "(none)",
    }

    return {
        "config": config_check,
        "current_state": dict(relay_state),
        "reachability": reachability,
        "log": list(reversed(relay_diag_log)),
    }


@app.get("/api/logs/service")
def api_service_logs(lines: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    timestamp = iso_utcnow()
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                JOURNAL_UNIT,
                "-n",
                str(lines),
                "--no-pager",
                "--output=short",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            log_lines = result.stdout.splitlines()
            return {"lines": log_lines, "count": len(log_lines), "timestamp": timestamp}
    except Exception:
        pass

    fallback = ["journalctl not available"]
    return {"lines": fallback, "count": len(fallback), "timestamp": timestamp}


@app.get("/api/proxmox/usb-config")
async def get_proxmox_usb_config(hostname: str | None = Query(default=None)) -> dict[str, Any]:
    return _proxmox_usb_config_payload(hostname)


@app.post("/api/proxmox/reclone-all")
async def api_proxmox_reclone_all() -> dict[str, Any]:
    if reclone_state.get("status") == "running":
        raise HTTPException(status_code=409, detail="A reclone run is already in progress")
    eligible = _reclone_targets_for_run()
    unassigned_dongles = _proxmox_unassigned_present_usb()
    if not eligible and not unassigned_dongles:
        raise HTTPException(
            status_code=400,
            detail=(
                "No reclone-capable guests or unassigned certified USB devices were found. "
                "Guests without a USB mapping or LXC template source are skipped."
            ),
        )
    asyncio.create_task(_run_rolling_reclone("manual"))
    return {"status": "started", "vm_count": len(eligible), "unassigned_dongles": len(unassigned_dongles)}


@app.post("/api/proxmox/reclone-state/clear")
async def api_proxmox_reclone_state_clear() -> dict[str, Any]:
    """Clear a stale failed/interrupted reclone state, resetting to idle.
    The last_run summary is preserved so the UI can still show what happened."""
    status = reclone_state.get("status", "idle")
    if status == "running":
        raise HTTPException(status_code=409, detail="Cannot clear reclone state while a reclone is running")
    reclone_state.update({
        "status": "idle",
        "type": None,
        "total": 0,
        "completed": 0,
        "failed": 0,
        "current_vm": None,
        "log": [],
        "started_at": None,
        "last_run": None,
        "auto_recovery_log": [],
    })
    await _broadcast_reclone_state()
    return {"cleared": True, "previous_status": status}


async def _authorize_proxmox_agent(hostname: str, api_key: str, client_ip: str, now: float) -> tuple[str | None, JSONResponse | None]:
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is None:
        _upsert_pending_proxmox_agent(hostname, client_ip, now)
        await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
        if api_key:
            return None, JSONResponse({"error": "agent not approved"}, status_code=401)
        return None, JSONResponse({"pending": True}, status_code=202)

    if api_key != approved_proxmox_agents[approved_hostname]:
        return None, JSONResponse({"error": "invalid key"}, status_code=401)

    pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
    if pending_hostname is not None:
        pending_proxmox_agents.pop(pending_hostname, None)
        await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
    return approved_hostname, None


async def _apply_proxmox_telemetry_state(body: dict[str, Any], hostname: str, now: float) -> dict[str, bool]:
    global _proxmox_reseed_in_progress
    async with state_lock:
        client_seen = {client_hostname: client.get("last_seen") for client_hostname, client in clients.items()}

    enriched_vms: list[dict[str, Any]] = []
    configured_template_ids: set[str] = {
        str(settings.get("vm_image_1_template_id", "100")).strip(),
        str(settings.get("vm_image_2_template_id", "200")).strip(),
    } - {""}
    for vm in body.get("vms", []):
        enriched = dict(vm)
        client_last_seen = client_seen.get(str(enriched.get("name", "")))
        if isinstance(client_last_seen, datetime):
            enriched["last_seen"] = client_last_seen.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        # Mark as template if agent flagged it OR if vmid matches a configured template ID
        if enriched.get("is_template") or str(enriched.get("vmid", "")).strip() in configured_template_ids:
            enriched["is_template"] = True
        enriched_vms.append(enriched)

    # Filter unknown_usb against currently certified and ignored vidpids so the device
    # disappears from the UI immediately after a certify/ignore action, even before the
    # Proxmox agent picks up the updated config on its next poll.
    certified_vidpids: set[str] = {
        str(item.get("vidpid", "")).strip().lower()
        for item in _parse_json_list(settings.get("usb_vidpids", "[]"))
        if isinstance(item, dict) and item.get("vidpid")
    }
    ignored_vidpids: set[str] = {
        str(v).strip().lower()
        for v in _parse_json_list(settings.get("usb_ignored_vidpids", "[]"))
        if str(v).strip()
    }
    exclude_vidpids = certified_vidpids | ignored_vidpids
    raw_unknown = body.get("unknown_usb", [])
    proxmox_state["unknown_usb"] = [
        d for d in raw_unknown
        if str(d.get("vidpid", "")).strip()  # skip devices with no VID:PID
        and str(d.get("vidpid", "")).strip().lower() not in exclude_vidpids
    ]

    normalized_present_usb = body.get("present_usb", [])
    normalized_usb_state = _normalize_proxmox_usb_state(body.get("usb_state", []), normalized_present_usb)

    was_connected = bool(proxmox_state.get("connected"))
    proxmox_state["connected"] = True
    proxmox_state["last_seen"] = now
    if not was_connected:
        gap = now - (proxmox_state.get("last_seen") or now)
        _debug_event("proxmox_reconnected", f"agent={hostname} gap={gap:.0f}s")
    _proxmox_reseed_in_progress = bool(body.get("reseed_in_progress", False))
    proxmox_state["node"] = body.get("node", {}) or {}

    # Tag each VM with the reporting agent hostname for client-side per-agent filtering.
    tagged_vms = [{**vm, "_agent_hostname": hostname} for vm in enriched_vms]

    # Tag each USB entry with the reporting agent hostname for client-side per-agent filtering.
    tagged_usb_state   = [{**e, "_agent_hostname": hostname} for e in normalized_usb_state]
    tagged_present_usb = [{**e, "_agent_hostname": hostname} for e in normalized_present_usb]
    tagged_unknown_usb = [{**e, "_agent_hostname": hostname} for e in proxmox_state.get("unknown_usb", [])]

    # Maintain per-agent rolling resource samples (1-hour window) so the detail
    # card can show per-server CPU/mem averages in a multi-Proxmox setup.
    _prev_agent = proxmox_states.get(hostname, {})
    _agent_cpu_samples: list[tuple[float, float]] = _prev_agent.get("_cpu_samples", [])
    _agent_mem_samples: list[tuple[float, float]] = _prev_agent.get("_mem_samples", [])
    _sample_cutoff = now - _RESOURCE_SAMPLE_WINDOW
    _anode = body.get("node", {}) or {}
    _cpu_pct = _anode.get("cpu_percent")
    if _cpu_pct is not None:
        _agent_cpu_samples = [(ts, v) for ts, v in _agent_cpu_samples if ts >= _sample_cutoff]
        _agent_cpu_samples.append((now, float(_cpu_pct)))
    _mem_used  = _anode.get("mem_used_kb")
    _mem_total = _anode.get("mem_total_kb")
    if _mem_used is not None and _mem_total:
        try:
            _mem_pct = float(_mem_used) / float(_mem_total) * 100.0
            _agent_mem_samples = [(ts, v) for ts, v in _agent_mem_samples if ts >= _sample_cutoff]
            _agent_mem_samples.append((now, _mem_pct))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    _agent_cpu_avg = (sum(v for _, v in _agent_cpu_samples) / len(_agent_cpu_samples)) if _agent_cpu_samples else None
    _agent_mem_avg = (sum(v for _, v in _agent_mem_samples) / len(_agent_mem_samples)) if _agent_mem_samples else None
    reported_provision_halt = body.get("provision_halt") if _autoprov_enabled() else None

    # Update per-agent state for multi-server list UI.
    # Preserve vmid_range from this telemetry cycle (or from prior state if not yet sent).
    _vmid_range_raw = body.get("vmid_range") or {}
    _vmid_range: dict[str, int] | None = None
    try:
        _vr_start = int(_vmid_range_raw.get("start", 0) or 0)
        _vr_end   = int(_vmid_range_raw.get("end",   0) or 0)
        if _vr_start > 0 and _vr_end >= _vr_start:
            _vmid_range = {"start": _vr_start, "end": _vr_end}
    except (TypeError, ValueError):
        pass
    if _vmid_range is None:
        # Fall back to previously stored range if the agent hasn't sent it yet
        _vmid_range = (_prev_agent or {}).get("vmid_range")

    proxmox_states[hostname] = {
        "connected": True,
        "last_seen": now,
        "agent_version": str(body.get("agent_version", "")).strip() or None,
        "pve_version": str(body.get("pve_version", "")).strip() or None,
        "vm_count": sum(1 for vm in enriched_vms if not vm.get("is_template")),
        "usb_count": len(normalized_usb_state),
        "node": body.get("node", {}) or {},
        "provision_halt": reported_provision_halt,
        "_cpu_samples": _agent_cpu_samples,
        "_mem_samples": _agent_mem_samples,
        "cpu_1h_avg": _agent_cpu_avg,
        "mem_1h_avg": _agent_mem_avg,
        "vmid_range": _vmid_range,
        "vm_set_override": _sanitize_vm_set_override(body.get("vm_set_override", 0)),
        "effective_vm_set": max(1, int(body.get("effective_vm_set", _hostname_vm_set_number(hostname)) or _hostname_vm_set_number(hostname))),
        "vms": tagged_vms,
        "usb_state": tagged_usb_state,
        "present_usb": tagged_present_usb,
        "unknown_usb": tagged_unknown_usb,
    }

    # Rebuild merged VM list from all approved agents so the VMs tab shows
    # all agents' VMs (not just the most recently reporting one).
    all_vms: list[dict[str, Any]] = []
    all_usb_state: list[dict[str, Any]] = []
    all_present_usb: list[dict[str, Any]] = []
    all_unknown_usb: list[dict[str, Any]] = []
    for st in proxmox_states.values():
        all_vms.extend(st.get("vms", []))
        all_usb_state.extend(st.get("usb_state", []))
        all_present_usb.extend(st.get("present_usb", []))
        all_unknown_usb.extend(st.get("unknown_usb", []))
    proxmox_state["vms"] = all_vms

    # Update the vmid→hostname routing map so delete/reclone commands target the right node.
    reported_vmids = {int(vm["vmid"]) for vm in enriched_vms if vm.get("vmid") is not None}
    # Remove stale entries owned by this agent (VMs it no longer reports).
    stale = [vmid for vmid, owner in _proxmox_agent_vm_map.items() if owner == hostname and vmid not in reported_vmids]
    for vmid in stale:
        del _proxmox_agent_vm_map[vmid]
    # Register/update all VMs reported by this agent.
    for vmid in reported_vmids:
        _proxmox_agent_vm_map[vmid] = hostname

    proxmox_state["reseed_in_progress"] = _proxmox_reseed_in_progress
    proxmox_state["usb_state"] = all_usb_state
    proxmox_state["present_usb"] = all_present_usb
    proxmox_state["unknown_usb"] = all_unknown_usb
    proxmox_state["missing_timeout_mins"] = int(body.get("missing_timeout_mins", 60) or 60)
    proxmox_state["vm_set_override"] = _sanitize_vm_set_override(body.get("vm_set_override", 0))
    proxmox_state["effective_vm_set"] = max(1, int(body.get("effective_vm_set", _hostname_vm_set_number(hostname)) or _hostname_vm_set_number(hostname)))
    proxmox_state["agent_version"] = str(body.get("agent_version", "")).strip() or None
    proxmox_state["pve_version"] = str(body.get("pve_version", "")).strip() or None
    proxmox_state["template_lock"] = str(body.get("template_lock", "") or "").strip()
    proxmox_state["vh_devices"] = body.get("vh_devices", {})

    # Record rolling resource samples for 1-hour average threshold checks.
    # Called after agent_version/pve_version are set so _save_resource_cache persists them.
    _record_resource_samples(proxmox_state["node"], now)

    # T3 PCI devices — store the raw list from the agent and compute a filtered list
    # of devices matching the T3 target VID:PIDs (currently just 168c:0034).
    # T3_VIDPIDS defines which PCI vendor:device IDs qualify a node as a T3 host.
    T3_VIDPIDS: set[str] = {"168c:0034"}
    raw_pci: list[dict[str, Any]] = body.get("t3_pci_devices") or []
    # Normalize: keep only dicts with a vidpid field, lower-case for consistent matching.
    proxmox_state["t3_pci_devices"] = [
        d for d in raw_pci
        if isinstance(d, dict) and str(d.get("vidpid", "")).strip().lower() in T3_VIDPIDS
    ]

    # Hardware watchdog fault log + last reset reason (set by hw_watchdog_loop in agent)
    if "hw_faults" in body:
        proxmox_state["hw_faults"] = body["hw_faults"]
    if "hw_last_reset" in body and body["hw_last_reset"]:
        existing = proxmox_state.get("hw_last_reset") or {}
        incoming = body["hw_last_reset"]
        # Only overwrite if this is a newer reset record
        if not existing or incoming.get("ts", 0) > existing.get("ts", 0):
            proxmox_state["hw_last_reset"] = incoming
            # Broadcast a dedicated alert so the hub hears about it in real-time
            await broadcast({
                "type": "proxmox_hw_reset",
                "hostname": str((body.get("node") or {}).get("hostname", "") or ""),
                "reason": incoming.get("reason", ""),
                "ts": incoming.get("ts"),
                "agent_version": incoming.get("agent_version", ""),
            })

    # Persist provision_halt from the agent's telemetry so the hub can display it.
    # When auto-provisioning is disabled, force the state clear even if the agent
    # has not yet refreshed its local cache.
    if "provision_halt" in body or not _autoprov_enabled():
        proxmox_state["provision_halt"] = reported_provision_halt

    # Clear pending-delete VMIDs that the agent has confirmed are gone.
    # intersection_update keeps only IDs still in the telemetry report;
    # any VMID that has disappeared from the agent has been successfully deleted.
    if _pending_delete_vmids:
        telemetry_vmids = {int(v.get("vmid")) for v in enriched_vms if v.get("vmid") is not None}
        confirmed_deleted = _pending_delete_vmids - telemetry_vmids
        _pending_delete_vmids.intersection_update(telemetry_vmids)
        # Cancel any pending auto-recovery reclone commands for confirmed-deleted VMIDs
        if confirmed_deleted:
            global _delete_gate_cooldown_until
            # Start the post-delete cooldown now that the VM is actually gone so the
            # fleet has time to stabilise before the gate may fire again.
            _delete_gate_cooldown_until = time.time() + DELETE_GATE_COOLDOWN_S
            logger.info(
                "Auto-delete gate: %d VM(s) confirmed deleted — cooldown active for %ds",
                len(confirmed_deleted), DELETE_GATE_COOLDOWN_S,
            )
            for cmd in commands:
                if (cmd.get("action") == "reclone_vm"
                        and cmd.get("type") == "auto-recovery"
                        and cmd.get("status") in {"pending", "delivered"}
                        and int(cmd.get("args", {}).get("vmid", -1)) in confirmed_deleted):
                    cmd["status"] = "cancelled"
                    cmd["error"] = "VM was deleted — auto-recovery cancelled"

    # Detect provisioning/teardown completions for summary tracking
    global _prev_usb_by_vmid
    new_usb: list[dict] = proxmox_state["usb_state"]
    new_by_vmid = {str(e["vmid"]): e.get("prov_status", "active") for e in new_usb if e.get("vmid") is not None}
    if _prev_usb_by_vmid:
        newly_provisioned = [
            vmid for vmid, st in new_by_vmid.items()
            if st == "active" and _prev_usb_by_vmid.get(vmid) == "provisioning"
        ]
        torn_down = [
            vmid for vmid, st in _prev_usb_by_vmid.items()
            if vmid not in new_by_vmid and st in ("tearing_down", "missing")
        ]
        if newly_provisioned:
            proxmox_state["prov_summary"] = {"action": "provisioned", "count": len(newly_provisioned), "at": now}
            for vmid in newly_provisioned:
                vm = next((item for item in enriched_vms if str(item.get("vmid")) == vmid), {})
                _record_vm_watchdog_clone_completed(vmid, vm.get("name"))
            await _async_save_vm_watchdog()
        elif torn_down:
            proxmox_state["prov_summary"] = {"action": "deleted", "count": len(torn_down), "at": now}
    _update_provision_run_state(proxmox_state["vms"], new_usb, now)
    _prev_usb_by_vmid = new_by_vmid

    # Auto-reset a stale reclone run to idle when:
    #   • The run is in a non-running terminal/interrupted state
    #     (interrupted, failed — "completed" is already reset in _run_rolling_reclone)
    #   • The Proxmox agent now reports zero reclone-eligible VMs, which means
    #     any VMs that were part of the interrupted run have since been deleted.
    # This prevents the Fleet Reclone tile from staying stuck at "3/9" indefinitely
    # after an operator cleans up VMs outside the normal reclone flow.
    _stale_reclone_statuses = {"interrupted", "failed"}
    if reclone_state.get("status") in _stale_reclone_statuses:
        eligible_after_update = _reclone_targets_for_run()
        # Also clear if every VM that previously failed is now running — the
        # operator may have fixed them outside the reclone flow (e.g. by starting
        # them manually) and the stale "Failed" badge is no longer meaningful.
        failed_vmids_in_log = {
            str(e.get("vmid"))
            for e in (reclone_state.get("log") or [])
            if e.get("status") == "failed" and e.get("vmid") is not None
        }
        running_vmids = {
            str(v.get("vmid"))
            for v in enriched_vms
            if str(v.get("status", "")).lower() == "running" and v.get("vmid") is not None
        }
        all_failed_now_running = bool(failed_vmids_in_log) and failed_vmids_in_log.issubset(running_vmids)
        if not eligible_after_update or all_failed_now_running:
            reason = "0 eligible VMs" if not eligible_after_update else "all previously failed VMs are now running"
            logger.info(
                "Fleet Reclone: detected stale '%s' run (%s) — auto-resetting to idle",
                reclone_state["status"], reason,
            )
            saved_last_run = reclone_state.get("last_run")
            saved_auto_log = reclone_state.get("auto_recovery_log") or []
            reclone_state.update({
                "status": "idle",
                "type": None,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "current_vm": None,
                "log": [],
                "started_at": None,
                "last_run": saved_last_run,
                "auto_recovery_log": saved_auto_log,
            })
            # Persist the reset and push a reclone_update WS message so any
            # connected browser sees the tile clear immediately without waiting
            # for the next proxmox_update broadcast.
            await _async_save_reclone_state()
            await _broadcast_reclone_state()

    # Auto-trigger provision_unassigned when usb_auto_provision is enabled and
    # certified unassigned dongles are physically present.  Resource (CPU/memory)
    # thresholds gate provisioning and can also trigger deletion of the newest sim VM.
    _ap_enabled = settings.get("usb_auto_provision") == "on"
    _reclone_running = reclone_state.get("status") == "running"
    if not _ap_enabled:
        _autoprov_gate_log("disabled", "usb_auto_provision=off — skipping all provision/delete checks")
    elif _reclone_running:
        _autoprov_gate_log("reclone_running", "reclone job is running (status=%s) — skipping provision checks", reclone_state.get("status"))
    if _ap_enabled and not _reclone_running:
        def _pct_setting(key: str, default: str) -> int:
            try:
                return max(0, min(100, int(str(settings.get(key, default)).strip() or default)))
            except (TypeError, ValueError):
                return int(default)

        cpu_prov_thr  = _pct_setting("cpu_provision_threshold", "80")
        cpu_del_thr   = _pct_setting("cpu_delete_threshold",   "90")
        cpu_prov_ceil = _pct_setting("cpu_provision_ceiling",  "90")
        mem_prov_thr  = _pct_setting("mem_provision_threshold", "80")
        mem_del_thr   = _pct_setting("mem_delete_threshold",   "90")
        cpu_avg = _resource_1h_average(_agent_cpu_samples)
        mem_avg = _resource_1h_average(_agent_mem_samples)
        # Most-recent instantaneous CPU reading (updated every ~30 s by telemetry).
        # Used as a hard ceiling to block provisioning during ramp-up before the
        # 1-hour average catches up.
        cpu_instant = _agent_cpu_samples[-1][1] if _agent_cpu_samples else None

        # Delete gate: if either metric exceeds its delete threshold and no delete is
        # already in flight, remove the newest sim VM (highest VMID) to shed load.
        #
        # The check and enqueue are performed atomically under state_lock to prevent
        # a TOCTOU race where multiple concurrent telemetry calls each see
        # delete_queued=False and each independently queue a delete for the same VM.
        delete_queued = False  # initialise; set True inside the atomic lock section below
        _threshold_exceeded = (
            (cpu_avg is not None and cpu_avg >= cpu_del_thr) or
            (mem_avg is not None and mem_avg >= mem_del_thr)
        )
        if _threshold_exceeded:
            usb_vmids_int: set[int] = set()
            _usb_prov_status: dict[int, str] = {}
            for _e in normalized_usb_state:
                try:
                    _evmid = int(_e["vmid"])
                    usb_vmids_int.add(_evmid)
                    _usb_prov_status[_evmid] = str(_e.get("prov_status") or "active").strip().lower()
                except (KeyError, TypeError, ValueError):
                    pass
            # Correct stale "provisioning" status: the bash agent's usb_state lags by
            # one telemetry cycle after the spoke's prov_run finishes configuring a VM.
            # Without this correction newly-configured or failed VMs remain stuck in
            # "provisioning" and are excluded from delete candidates.
            # NOTE: do NOT guard on `not running` — if a parallel clone was killed mid-run
            # (stuck >120s), the overall run stays running=True indefinitely but individual
            # items already have status="done" or "failed". We must correct those too.
            _prov_run_snap = proxmox_state.get("prov_run") or {}
            for _pr_item in (_prov_run_snap.get("items") or []):
                if isinstance(_pr_item, dict) and str(_pr_item.get("status") or "").strip().lower() in {"done", "failed"}:
                    try:
                        _pr_vid = int(_pr_item.get("vmid") or 0)
                        if _pr_vid and _usb_prov_status.get(_pr_vid) == "provisioning":
                            _usb_prov_status[_pr_vid] = "active"
                    except (TypeError, ValueError):
                        pass
            # Exclude VMs that are mid-clone (provisioning) or already being torn down
            # by the USB-missing timeout handler (tearing_down) — both are transient
            # states where a second delete command causes wasted work or race conditions.
            _skip_statuses = {"provisioning", "tearing_down"}
            candidates: list[int] = []
            for _vm in enriched_vms:
                try:
                    _vid = int(_vm.get("vmid", 0) or 0)
                    if (
                        _vm.get("type") == "qemu"
                        and not _vm.get("is_template")
                        and _vid in usb_vmids_int
                        and _vid not in _pending_delete_vmids
                        and _usb_prov_status.get(_vid, "active") not in _skip_statuses
                    ):
                        candidates.append(_vid)
                except (TypeError, ValueError):
                    pass
            if candidates:
                target_vmid = max(candidates)  # newest = highest VMID
                _del_args = _prepare_delete_vm_args({"vmid": target_vmid})
                # Re-check and enqueue atomically under state_lock to close the TOCTOU
                # window between the threshold check above and the actual queue operation.
                async with state_lock:
                    # Respect the post-delete cooldown so consecutive auto-deletes are
                    # separated by at least DELETE_GATE_COOLDOWN_S (set after the prior
                    # delete is confirmed, not at enqueue time — see confirmed_deleted block).
                    if time.time() < _delete_gate_cooldown_until:
                        _remaining_cd = int(_delete_gate_cooldown_until - time.time())
                        logger.info(
                            "Auto-delete gate: cooldown active (%ds remaining) — skipping delete of VMID %d",
                            _remaining_cd, target_vmid,
                        )
                    else:
                        delete_queued = any(
                            c.get("action") == "delete_vm"
                            and c.get("status") not in {"completed", "failed", "expired"}
                            for c in commands
                        )
                        if not delete_queued:
                            _enqueue_command_locked(
                                _resolve_proxmox_vm_target(target_vmid),
                                "delete_vm",
                                _del_args,
                                command_type="auto-provision",
                            )
                            _pending_delete_vmids.add(target_vmid)
                            # Also start the cooldown at enqueue time so the gate cannot
                            # fire a second time during the window between "delete command
                            # executed by agent" and "telemetry confirms VM gone".
                            # The confirmed_deleted block will refresh the cooldown once
                            # the deletion is confirmed, giving the full window from that
                            # later point.
                            _delete_gate_cooldown_until = time.time() + DELETE_GATE_COOLDOWN_S
                            logger.info(
                                "Auto-provision resource gate: delete threshold exceeded "
                                "(cpu_avg=%.1f%% mem_avg=%.1f%%) — queued delete_vm for VMID %d; "
                                "cooldown active for %ds",
                                cpu_avg or 0.0, mem_avg or 0.0, target_vmid, DELETE_GATE_COOLDOWN_S,
                            )
            else:
                logger.info(
                    "Auto-provision resource gate: delete threshold exceeded "
                    "(cpu_avg=%.1f%% mem_avg=%.1f%%) — no eligible candidates "
                    "(all USB VMs are provisioning, tearing_down, or pending delete)",
                    cpu_avg or 0.0, mem_avg or 0.0,
                )

        # Provision gate: skip new provisioning when either resource exceeds its threshold.
        # Also skip for this cycle if we just queued a delete, to avoid churn.
        # Also skip for the full delete-gate cooldown window — prevents the dongle that
        # was just freed by a resource-triggered delete from being immediately re-provisioned
        # (which would otherwise create a delete→reprovision→delete loop).
        # cpu_prov_ceil is a hard ceiling on the *instantaneous* CPU reading so that
        # provisioning is suppressed during ramp-up before the 1-hour average catches up.
        _in_delete_cooldown = time.time() < _delete_gate_cooldown_until
        if _in_delete_cooldown:
            _remaining_prov_cd = int(_delete_gate_cooldown_until - time.time())
            logger.info(
                "Auto-provision gate: delete cooldown active (%ds remaining) — suppressing provision_unassigned",
                _remaining_prov_cd,
            )
        _ceil_hit = cpu_instant is not None and cpu_instant >= cpu_prov_ceil
        if _ceil_hit:
            logger.info(
                "Auto-provision ceiling: instantaneous CPU %.1f%% >= ceiling %d%% — suppressing provision_unassigned",
                cpu_instant, cpu_prov_ceil,
            )
        resource_ok = (
            not delete_queued
            and not _in_delete_cooldown
            and not _ceil_hit
            and cpu_avg is not None and cpu_avg < cpu_prov_thr
            and mem_avg is not None and mem_avg < mem_prov_thr
        )
        # Log resource state periodically so the journal shows what the gate sees
        _autoprov_gate_log(
            "resource_state",
            "cpu_avg=%.1f%% (thr=%d%%) mem_avg=%.1f%% (thr=%d%%) cpu_instant=%.1f%% (ceil=%d%%) "
            "delete_queued=%s in_delete_cooldown=%s ceil_hit=%s resource_ok=%s",
            cpu_avg or 0.0, cpu_prov_thr,
            mem_avg or 0.0, mem_prov_thr,
            cpu_instant or 0.0, cpu_prov_ceil,
            delete_queued, _in_delete_cooldown, _ceil_hit, resource_ok,
        )
        if not resource_ok and not _ceil_hit:
            if delete_queued:
                _autoprov_gate_log("delete_queued", "delete_vm already in queue — suppressing provision_unassigned")
            elif _in_delete_cooldown:
                pass  # already logged above
            elif cpu_avg is None or mem_avg is None:
                _autoprov_gate_log("no_telemetry", "waiting for CPU/mem telemetry (cpu_avg=%s mem_avg=%s) — suppressing provision_unassigned", cpu_avg, mem_avg)
            elif cpu_avg >= cpu_prov_thr:
                _autoprov_gate_log("cpu_threshold", "cpu_avg=%.1f%% >= threshold=%d%% — suppressing provision_unassigned", cpu_avg, cpu_prov_thr)
            elif mem_avg >= mem_prov_thr:
                _autoprov_gate_log("mem_threshold", "mem_avg=%.1f%% >= threshold=%d%% — suppressing provision_unassigned", mem_avg, mem_prov_thr)
        prov_run = proxmox_state.get("prov_run") or {}
        if resource_ok and prov_run.get("running"):
            _autoprov_gate_log(
                "prov_run_active",
                "prov_run.running=True — provision loop already active, skipping trigger "
                "(vmids=%s status=%s)",
                [i.get("vmid") for i in (prov_run.get("items") or [])],
                [i.get("status") for i in (prov_run.get("items") or [])],
            )
        if resource_ok and not prov_run.get("running"):
            unassigned = _proxmox_unassigned_present_usb()
            if not unassigned:
                _autoprov_gate_log("no_unassigned", "no unassigned USB dongles present — nothing to provision")
            if unassigned:
                certified_set = {
                    (str(v.get("vidpid", "")).strip().lower() if isinstance(v, dict) else str(v).strip().lower())
                    for v in _parse_json_list(settings.get("usb_vidpids", "[]"))
                    if (str(v.get("vidpid", "") if isinstance(v, dict) else v)).strip()
                }
                certified_unassigned = [
                    u for u in unassigned
                    if str(u.get("vidpid", "")).strip().lower() in certified_set
                ]
                if not certified_unassigned:
                    _autoprov_gate_log(
                        "not_certified",
                        "unassigned dongles present but none match certified VIDPIDs — "
                        "unassigned=%s certified_vidpids=%s",
                        [u.get("vidpid") for u in unassigned],
                        sorted(certified_set),
                    )
                if certified_unassigned:
                    has_pending = any(
                        c.get("action") == "provision_unassigned"
                        and c.get("status") not in {"completed", "failed", "expired"}
                        for c in commands
                    )
                    if has_pending:
                        pending_cmd = next(
                            (c for c in commands if c.get("action") == "provision_unassigned"
                             and c.get("status") not in {"completed", "failed", "expired"}), None
                        )
                        _autoprov_gate_log(
                            "already_pending",
                            "provision_unassigned already pending (id=%s status=%s) — not queuing again",
                            pending_cmd.get("id") if pending_cmd else "?",
                            pending_cmd.get("status") if pending_cmd else "?",
                        )
                    if not has_pending:
                        await _queue_proxmox_command("provision_unassigned", {}, command_type="auto-provision")
                        logger.info(
                            "Auto-provisioning: detected %d unassigned certified dongle(s) — queued provision_unassigned",
                            len(certified_unassigned),
                        )

    # ── VMID gap audit ────────────────────────────────────────────────────────
    # If the auto-provision loop previously deleted/re-provisioned VMs out of
    # order, VMIDs can develop gaps (e.g. …90030, 90032 with 90031 missing).
    # This audit detects such gaps and queues a delete for the highest VMID
    # above the lowest gap so the provision loop can fill the hole on the next
    # cycle.  It bypasses the normal delete-gate cooldown because it is a
    # corrective bookkeeping action, not a resource-pressure shedding action.
    # It does respect its own per-host interval to avoid hammering the queue.
    if _ap_enabled and not _reclone_running and _vmid_range:
        _audit_due = (now - _vmid_gap_audit_last_run.get(hostname, 0.0)) >= VMID_AUDIT_INTERVAL_S
        if _audit_due:
            _vmid_gap_audit_last_run[hostname] = now
            _gap_start: int = _vmid_range["start"]
            _gap_end:   int = _vmid_range["end"]

            # Build map of VMID → prov_status for VMs in this host's range.
            _gap_prov_status: dict[int, str] = {}
            for _ge in normalized_usb_state:
                try:
                    _gvid = int(_ge["vmid"])
                    if _gap_start <= _gvid <= _gap_end:
                        _gap_prov_status[_gvid] = str(_ge.get("prov_status") or "active").strip().lower()
                except (KeyError, TypeError, ValueError):
                    pass
            # Apply the same stale-provisioning correction used by the delete gate.
            _gap_prov_snap = proxmox_state.get("prov_run") or {}
            for _gpr in (_gap_prov_snap.get("items") or []):
                if isinstance(_gpr, dict) and str(_gpr.get("status") or "").strip().lower() in {"done", "failed"}:
                    try:
                        _gpvid = int(_gpr.get("vmid") or 0)
                        if _gap_prov_status.get(_gpvid) == "provisioning":
                            _gap_prov_status[_gpvid] = "active"
                    except (TypeError, ValueError):
                        pass

            # Active (stable) VMIDs only — skip anything in-flight.
            _gap_skip = {"provisioning", "tearing_down"}
            _gap_active = sorted(
                vid for vid, st in _gap_prov_status.items()
                if st not in _gap_skip and vid not in _pending_delete_vmids
            )

            if len(_gap_active) >= 2:
                _gap_max = _gap_active[-1]
                _gap_active_set = set(_gap_active)
                _lowest_gap: int | None = None
                for _chk in range(_gap_start, _gap_max):
                    if _chk not in _gap_active_set:
                        _lowest_gap = _chk
                        break

                if _lowest_gap is not None:
                    # Find highest active VMID above the gap.
                    _above_gap = [v for v in _gap_active if v > _lowest_gap]
                    if _above_gap:
                        _gap_target = max(_above_gap)
                        _gap_del_args = _prepare_delete_vm_args({"vmid": _gap_target})
                        async with state_lock:
                            _gap_already_pending = any(
                                c.get("action") == "delete_vm"
                                and c.get("status") not in {"completed", "failed", "expired"}
                                for c in commands
                            )
                            if not _gap_already_pending:
                                _enqueue_command_locked(
                                    _resolve_proxmox_vm_target(_gap_target),
                                    "delete_vm",
                                    _gap_del_args,
                                    command_type="auto-provision",
                                )
                                _pending_delete_vmids.add(_gap_target)
                                _gap_msg = (
                                    f"VMID gap audit [{hostname}]: gap detected at {_lowest_gap} "
                                    f"(range {_gap_start}-{_gap_end}, active={_gap_active}) — "
                                    f"queued delete_vm for VMID {_gap_target} to restore sequential order"
                                )
                                logger.info(_gap_msg)
                                proxmox_log_buffer.append(_gap_msg)
                                if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
                                    del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
                                await broadcast({"type": "proxmox_log_update", "lines": [_gap_msg]})
                            else:
                                logger.info(
                                    "VMID gap audit [%s]: gap at %d would target VMID %d "
                                    "but a delete_vm is already pending — skipping",
                                    hostname, _lowest_gap, _gap_target,
                                )
                else:
                    logger.debug(
                        "VMID gap audit [%s]: no gaps in active VMIDs %s (range %d-%d)",
                        hostname, _gap_active, _gap_start, _gap_end,
                    )

    # Append new log lines to ring buffer and broadcast if any arrived
    new_lines = [str(ln) for ln in (body.get("log_lines") or []) if ln]
    if new_lines:
        # Prefix log lines with the agent hostname so multi-agent logs are distinguishable
        if len(approved_proxmox_agents) > 1:
            new_lines = [f"[{hostname}] {ln}" if not ln.startswith(f"[{hostname}]") else ln for ln in new_lines]
        proxmox_log_buffer.extend(new_lines)
        if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
            del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
        await broadcast({"type": "proxmox_log_update", "lines": new_lines})

    await _broadcast_proxmox_state()
    return {"ok": True}


@app.post("/api/proxmox/telemetry", response_model=None)
async def proxmox_telemetry(request: Request, body: dict = Body(...)) -> dict[str, bool] | JSONResponse:
    """Receive telemetry from the Proxmox host agent."""
    node = body.get("node", {}) or {}
    hostname = str(node.get("hostname", "") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)

    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, now)
    if response is not None:
        return response
    # Use the canonical key from approved_proxmox_agents so proxmox_states entries
    # are keyed consistently regardless of case or minor hostname format differences.
    canonical_hostname = _approved_hostname or hostname
    try:
        return await _apply_proxmox_telemetry_state(body, canonical_hostname, now)
    except Exception:
        tb = traceback.format_exc()
        logger.error("TELEMETRY HANDLER CRASH for %s:\n%s", hostname, tb)
        last_line = tb.splitlines()[-1] if tb.splitlines() else "unknown"
        proxmox_log_buffer.append(f"[SPOKE ERROR] telemetry crash: {last_line}")
        try:
            _trace("telemetry_crash", hostname=hostname, error=last_line)
        except Exception:
            pass
        raise



@app.get("/api/proxmox/logs")
async def get_proxmox_logs() -> dict[str, Any]:
    """Return the in-memory agent log ring buffer."""
    return {"lines": proxmox_log_buffer}


@app.post("/api/proxmox/logs/clear")
async def clear_proxmox_logs() -> dict[str, bool]:
    """Clear the in-memory agent log buffer."""
    proxmox_log_buffer.clear()
    await broadcast({"type": "proxmox_log_update", "lines": [], "cleared": True})
    return {"ok": True}


@app.post("/api/proxmox/log-push", response_model=None)
async def proxmox_log_push(request: Request, body: dict = Body(...)) -> dict[str, bool] | JSONResponse:
    """Lightweight HTTP log-push endpoint — agent sends log lines here even when WS is unavailable.
    Accepts: {"hostname": "...", "log_lines": ["line1", ...]}
    Auth: X-API-Key header (same as telemetry endpoint).
    """
    hostname = str(body.get("hostname") or body.get("node", {}).get("hostname") or "").strip()
    api_key = request.headers.get("X-API-Key", "")
    client_ip = request.client.host if request.client else "unknown"
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    _approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, client_ip, time.time())
    if response is not None:
        return response
    canonical_hn = _approved_hostname or hostname
    new_lines = [str(ln) for ln in (body.get("log_lines") or []) if ln]
    if new_lines:
        if len(approved_proxmox_agents) > 1:
            new_lines = [f"[{canonical_hn}] {ln}" if not ln.startswith(f"[{canonical_hn}]") else ln for ln in new_lines]
        proxmox_log_buffer.extend(new_lines)
        if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
            del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
        await broadcast({"type": "proxmox_log_update", "lines": new_lines})
    return {"ok": True, "accepted": len(new_lines)}


@app.post("/api/proxmox/watchdog_event")
async def proxmox_watchdog_event(body: dict = Body(...)) -> dict[str, bool]:
    event = str(body.get("event", "") or "").strip()
    service = str(body.get("service", "") or "").strip()
    hostname = str(body.get("hostname", "") or "").strip()
    timestamp = str(body.get("timestamp", "") or "").strip()
    detail_raw = body.get("detail", "") or ""
    detail = json.dumps(detail_raw) if isinstance(detail_raw, dict) else str(detail_raw).strip()
    try:
        failure_count = max(0, int(body.get("failure_count", 0) or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="failure_count must be an integer")

    if not all((event, hostname, timestamp)):
        raise HTTPException(status_code=400, detail="event, hostname, and timestamp are required")

    entry = {
        "event": event,
        "service": service,
        "hostname": hostname,
        "timestamp": timestamp,
        "failure_count": failure_count,
    }
    if detail:
        entry["detail"] = detail
    proxmox_watchdog_log.append(entry)
    if len(proxmox_watchdog_log) > PROXMOX_WATCHDOG_LOG_MAX:
        del proxmox_watchdog_log[:len(proxmox_watchdog_log) - PROXMOX_WATCHDOG_LOG_MAX]

    detail_suffix = f" detail={detail[:120]}" if detail else ""
    log_line = (
        f"[{timestamp}] WATCHDOG event={event} service={service} "
        f"hostname={hostname} failure_count={failure_count}{detail_suffix}"
    )
    proxmox_log_buffer.append(log_line)
    if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
        del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]

    # For network-related events and startup, also store in hw_faults so they surface in the hub panel
    if event in {"net_reboot", "net_down", "watchdog_started"}:
        hw_faults = proxmox_state.get("hw_faults") or {"faults": []}
        hw_faults.setdefault("faults", []).append({
            "type": event,
            "check": "network_watchdog" if event != "watchdog_started" else "watchdog_startup",
            "message": detail or f"Gateway unreachable — {event}" if event != "watchdog_started" else f"Watchdog started — boot_time={detail_raw.get('boot_time','?') if isinstance(detail_raw, dict) else '?'}",
            "hostname": hostname,
            "ts": timestamp,
        })
        hw_faults["faults"] = hw_faults["faults"][-100:]
        proxmox_state["hw_faults"] = hw_faults

    await broadcast({"type": "proxmox_log_update", "lines": [log_line]})
    return {"ok": True}


@app.post("/api/proxmox/hw_reset_event")
async def proxmox_hw_reset_event(body: dict = Body(...)) -> dict[str, bool]:
    """Called by the proxmox agent immediately before triggering a hard reset.
    Stores the event so the hub learns about it even if the agent never sends
    another telemetry post after rebooting."""
    hostname  = str(body.get("hostname", "") or "").strip()
    reason    = str(body.get("reason", "") or "").strip()
    tier      = str(body.get("tier", "") or "").strip()
    ts        = body.get("ts") or time.time()
    patterns  = body.get("patterns") or []
    agent_ver = str(body.get("agent_version", "") or "").strip()

    record = {
        "ts": ts,
        "hostname": hostname,
        "reason": reason,
        "tier": tier,
        "patterns": patterns,
        "agent_version": agent_ver,
        "source": "pre_reboot_notification",
    }

    # Store as last reset so the relay includes it immediately
    existing = proxmox_state.get("hw_last_reset") or {}
    if not existing or float(ts) >= existing.get("ts", 0):
        proxmox_state["hw_last_reset"] = record

    # Append to fault log
    hw_faults = proxmox_state.get("hw_faults") or {"faults": []}
    hw_faults.setdefault("faults", []).append({**record, "type": "pre_reboot_notification"})
    hw_faults["faults"] = hw_faults["faults"][-100:]
    proxmox_state["hw_faults"] = hw_faults

    log_line = (
        f"[HW-RESET] {hostname} initiating hard reset — tier={tier} reason={reason[:160]}"
    )
    proxmox_log_buffer.append(log_line)
    if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
        del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]

    await broadcast({
        "type": "proxmox_hw_reset",
        "hostname": hostname,
        "reason": reason,
        "tier": tier,
        "patterns": patterns,
        "ts": ts,
        "agent_version": agent_ver,
    })
    await broadcast({"type": "proxmox_log_update", "lines": [log_line]})
    return {"ok": True}


@app.get("/api/proxmox/status")
async def get_proxmox_status() -> dict[str, Any]:
    return _proxmox_status_payload()


@app.get("/api/proxmox/config/{hostname}")
async def get_proxmox_host_config(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    host_config = _get_proxmox_host_config(resolved_hostname)
    return {
        "hostname": resolved_hostname,
        "vm_set_override": _sanitize_vm_set_override(host_config.get("vm_set_override", 0)),
    }


@app.put("/api/proxmox/config/{hostname}")
async def save_proxmox_host_config(
    hostname: str,
    body: dict[str, Any] = Body(...),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    try:
        vm_set_override = int(body.get("vm_set_override", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="vm_set_override must be an integer") from exc
    if vm_set_override < 0 or vm_set_override > 99:
        raise HTTPException(status_code=422, detail="vm_set_override must be between 0 and 99")
    config_entry = _save_proxmox_host_config(resolved_hostname, {"vm_set_override": vm_set_override})
    logger.info("Proxmox VM set override saved for host %s: %s", resolved_hostname, config_entry.get("vm_set_override", 0) or 0)
    settings_payload = _public_settings()
    await broadcast({"type": "settings_update", "settings": settings_payload})
    return {
        "ok": True,
        "hostname": resolved_hostname,
        "vm_set_override": _sanitize_vm_set_override(config_entry.get("vm_set_override", 0)),
    }


@app.get("/api/proxmox/token/{hostname}")
async def get_proxmox_host_token_status(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    per_host = str((settings.get("proxmox_tokens") or {}).get(resolved_hostname, "") or "").strip()
    global_tok = _get_proxmox_token_for_host(None)
    return {
        "hostname": resolved_hostname,
        "configured": bool(per_host),
        "global_configured": bool(global_tok),
    }


@app.put("/api/proxmox/token/{hostname}")
async def save_proxmox_host_token(
    hostname: str,
    body: dict[str, Any] = Body(...),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    token = str(body.get("proxmox_token") or body.get("proxmox_api_token") or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="proxmox_token is required")
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    _save_proxmox_token_for_host(resolved_hostname, token)
    logger.info("Proxmox API token saved for host %s", resolved_hostname)
    return {"ok": True, "hostname": resolved_hostname, "configured": True}


@app.post("/api/proxmox/token/{hostname}/auto-provision")
async def auto_provision_proxmox_host_token(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname.strip(), approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    TOKEN_ID = "cs-hub"
    USER = "root@pam"
    request_id = str(uuid.uuid4())

    pvesh_candidates = [
        shutil.which("pvesh"),
        "/usr/bin/pvesh",
        "/usr/sbin/pvesh",
        "/usr/local/bin/pvesh",
        "/usr/share/pve-manager/bin/pvesh",
        "/opt/proxmox/bin/pvesh",
    ]
    pvesh_path = next((c for c in pvesh_candidates if c and os.path.isfile(c)), None)
    local_candidates = [socket.gethostname(), socket.getfqdn(), os.environ.get("HOSTNAME", "")]
    use_local_pvesh = bool(
        pvesh_path and any(_proxmox_hostnames_match(resolved_hostname, candidate) for candidate in local_candidates if candidate)
    )

    if not use_local_pvesh:
        if resolved_hostname not in approved_proxmox_agents:
            raise HTTPException(status_code=404, detail="Proxmox agent not approved")
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        _proxmox_token_provision_queues[request_id] = q
        try:
            await _queue_proxmox_command(
                "create_proxmox_token",
                {"request_id": request_id},
                command_type="token-provision",
                target=resolved_hostname,
            )
            result = await asyncio.wait_for(q.get(), timeout=30.0)
            if result.get("ok"):
                token = str(result.get("token") or "").strip()
                if not token:
                    raise HTTPException(status_code=500, detail="Agent returned an empty token")
                _save_proxmox_token_for_host(resolved_hostname, token)
                logger.info("Proxmox API token auto-provisioned via agent for host %s", resolved_hostname)
                return {"ok": True, "hostname": resolved_hostname, "token_id": f"{USER}!{TOKEN_ID}"}
            raise HTTPException(status_code=500, detail=str(result.get("error") or "Agent failed to provision token"))
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Proxmox agent did not respond within 30 seconds") from exc
        finally:
            _proxmox_token_provision_queues.pop(request_id, None)

    try:
        del_proc = await asyncio.create_subprocess_exec(
            pvesh_path, "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10.0)
    except Exception:
        pass

    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh_path, "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0", "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="pvesh timed out after 15 seconds") from exc

    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pvesh failed: {stderr.decode().strip()[:200]}")

    try:
        data = json.loads(stdout.decode().strip())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse pvesh output: {exc}") from exc
    secret = str(data.get("value") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="pvesh returned no token value")

    full_token = f"{USER}!{TOKEN_ID}={secret}"
    _save_proxmox_token_for_host(resolved_hostname, full_token)
    logger.info("Proxmox API token auto-provisioned locally for host %s", resolved_hostname)
    return {"ok": True, "hostname": resolved_hostname, "token_id": f"{USER}!{TOKEN_ID}"}


@app.post("/api/proxmox/console/{vmid}")
async def api_create_console_session(
    vmid: int,
    vmtype: str = Query("qemu"),
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Create a direct Proxmox VNC console session for the spoke's own VM Server view."""
    proxmox_host = str(_proxmox_agent_vm_map.get(vmid) or proxmox_ws_hostname or "").strip()
    api_token = _get_proxmox_token_for_host(proxmox_host)
    if not proxmox_host:
        raise HTTPException(status_code=503, detail="Proxmox host unknown — no agent connected")
    if not api_token:
        raise HTTPException(status_code=503, detail="Proxmox API token not configured on spoke")
    normalized_vmtype = str(vmtype or "qemu").strip().lower()
    if normalized_vmtype not in {"qemu", "lxc"}:
        raise HTTPException(status_code=400, detail="vmtype must be qemu or lxc")
    node = proxmox_host.split(".")[0]
    vncproxy_url = f"https://{proxmox_host}:8006/api2/json/nodes/{node}/{normalized_vmtype}/{vmid}/vncproxy"
    auth_header = {"Authorization": f"PVEAPIToken={api_token}"}
    if httpx is None:
        raise HTTPException(status_code=503, detail="httpx not installed")
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(vncproxy_url, headers=auth_header, json={"websocket": 1}, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Proxmox vncproxy returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        ticket = body["data"]["ticket"]
        port = int(body["data"]["port"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Proxmox vncproxy call failed: {exc}") from exc
    session_id = str(uuid.uuid4())
    _direct_console_sessions[session_id] = {
        "proxmox_host": proxmox_host,
        "node": node,
        "vmid": vmid,
        "vmtype": normalized_vmtype,
        "api_token": api_token,
        "ticket": ticket,
        "port": port,
        "expires": time.time() + _DIRECT_CONSOLE_TTL,
    }
    return {"session_id": session_id, "expires_in": _DIRECT_CONSOLE_TTL}


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



@app.post("/api/proxmox/update-agent")
async def api_proxmox_update_agent(hostname: str | None = None) -> dict[str, Any]:
    cmd = await _queue_proxmox_agent_update(target=hostname or None)
    return {
        "queued": 1,
        "id": cmd["id"],
        "target": cmd["target"],
        "branch": cmd["args"].get("branch"),
        "source": cmd["args"].get("repo_raw"),
    }


@app.post("/api/proxmox/unlock-template")
async def api_proxmox_unlock_template() -> dict[str, Any]:
    cmd = await _queue_unlock_template_command()
    return {
        "queued": True,
        "id": cmd["id"],
        "target": cmd["target"],
        "action": cmd.get("action"),
    }


@app.delete("/api/proxmox/vms/{vmid}")
async def api_proxmox_delete_vm(vmid: int) -> dict[str, Any]:
    try:
        args = _prepare_delete_vm_args({"vmid": vmid})
    except HTTPException as exc:
        if exc.status_code == 404:
            # VM not in current Proxmox inventory — it may have been manually removed
            # from Proxmox directly.  Queue the delete anyway with a safe default so the
            # agent can confirm it is gone (idempotent) and update its state files.
            args = {"vmid": vmid, "vm_type": "qemu"}
        else:
            raise
    cmd = await _queue_proxmox_command("delete_vm", args, target=_resolve_proxmox_vm_target(vmid))
    _pending_delete_vmids.add(vmid)
    await _broadcast_proxmox_state()
    return {
        "queued": 1,
        "ids": [cmd["id"]],
        "vmid": vmid,
        "vm_type": args.get("vm_type"),
        "vm_name": args.get("vm_name"),
    }


@app.post("/api/proxmox/register")
async def proxmox_register(request: Request, body: dict = Body(...)) -> JSONResponse:
    """Called by agent with no key. Adds to pending if not approved."""
    hostname = str(body.get("hostname", "") or request.headers.get("X-Hostname", "")).strip()
    if not hostname:
        return JSONResponse({"error": "hostname required"}, status_code=400)
    client_ip = request.client.host if request.client else "unknown"

    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        if pending_hostname is not None:
            pending_proxmox_agents.pop(pending_hostname, None)
            await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})

    now = time.time()
    _upsert_pending_proxmox_agent(hostname, client_ip, now)
    await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
    return JSONResponse({"pending": True}, status_code=202)


@app.get("/api/proxmox/key")
async def proxmox_get_key(hostname: str = Query(...)) -> JSONResponse:
    """Agent polls this until approved. Returns key when ready."""
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is not None:
        return JSONResponse({"approved": True, "key": approved_proxmox_agents[approved_hostname]})
    if _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) is not None:
        return JSONResponse({"pending": True}, status_code=202)
    return JSONResponse({"error": "unknown hostname"}, status_code=404)


@app.get("/api/proxmox/pending")
async def proxmox_pending_list() -> list[dict[str, Any]]:
    return _pending_proxmox_payload()


@app.post("/api/proxmox/approve/{hostname}")
async def proxmox_approve(hostname: str) -> dict[str, Any]:
    pending_payload: list[dict[str, Any]] | None = None
    should_broadcast_state = False
    async with state_lock:
        pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
        approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
        if approved_hostname is not None:
            if pending_hostname is not None:
                pending_proxmox_agents.pop(pending_hostname, None)
                pending_payload = _pending_proxmox_payload()
                should_broadcast_state = True
            result = {"approved": True, "hostname": approved_hostname, "key": approved_proxmox_agents[approved_hostname], "existing": True}
        else:
            resolved_hostname = pending_hostname or _normalize_proxmox_hostname(hostname)
            if not resolved_hostname:
                raise HTTPException(status_code=400, detail="hostname is required")

            key = str(uuid.uuid4())
            approved_proxmox_agents[resolved_hostname] = key
            pending_proxmox_agents.pop(pending_hostname or resolved_hostname, None)
            settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
            _save_settings()
            pending_payload = _pending_proxmox_payload()
            should_broadcast_state = True
            result = {"approved": True, "hostname": resolved_hostname, "key": key}

    if pending_payload is not None:
        await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    if should_broadcast_state:
        await _broadcast_proxmox_state()
    return result


@app.post("/api/proxmox/reject/{hostname}")
async def proxmox_reject(hostname: str) -> dict[str, Any]:
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        pending_proxmox_agents.pop(resolved_hostname, None)
        pending_payload = _pending_proxmox_payload()
    await broadcast({"type": "proxmox_pending_update", "pending": pending_payload})
    await _broadcast_proxmox_state()
    return {"rejected": True, "hostname": resolved_hostname}


@app.delete("/api/proxmox/approved/{hostname}")
async def proxmox_revoke(hostname: str) -> dict[str, Any]:
    """Revoke an approved agent's key."""
    async with state_lock:
        resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents) or _normalize_proxmox_hostname(hostname)
        approved_proxmox_agents.pop(resolved_hostname, None)
        settings["proxmox_approved_agents"] = dict(approved_proxmox_agents)
        _save_settings()
    await _broadcast_proxmox_state()
    return {"revoked": True, "hostname": resolved_hostname}


@app.get("/api/proxmox/approved")
async def proxmox_approved_list() -> list[dict[str, Any]]:
    return _approved_proxmox_payload()


# ── Aruba Central API endpoints ───────────────────────────────────────────────
@app.post("/api/central/test-connection")
async def api_central_test() -> dict[str, Any]:
    mode = settings.get("central_api", {}).get("mode", "classic")
    async with httpx.AsyncClient() as client:
        if mode == "classic":
            ok, detail_msg = await _test_classic_central_connection(client)
            if ok:
                return {"status": "ok", "message": detail_msg}
            raise HTTPException(status_code=502 if "HTTP" in detail_msg or "rejected" in detail_msg else 422, detail=detail_msg)

        if not _central_ready():
            raise HTTPException(
                status_code=422,
                detail="Central API not configured — enter URL, Client ID, and Client Secret in Setup.",
            )
        ok, detail_msg = await _fetch_central_token(client)
    if ok:
        return {
            "status": "ok",
            "message": "Connected to Central API successfully.",
        }
    raise HTTPException(status_code=502, detail=detail_msg)


@app.get("/api/central/available")
async def api_central_available() -> dict[str, Any]:
    """Return available alert types and insight categories from Central. Always returns 200."""
    if not _central_ready():
        return {"alerts": [], "insights": [], "warning": "Central not configured."}

    # New Central v1alpha1 has no alerts/insights endpoints — return static synthetic checks.
    # These correspond to the metrics _poll_central_once() derives from sites-health, /aps, and /devices.
    # No live token is required to return this static list.
    if _is_new_central_api():
        return {
            "alerts": [
                {"id": "SITE_HEALTH",    "name": "Site Health Score (0–100)"},
                {"id": "AP_COUNT",       "name": "Total AP Count"},
                {"id": "AP_DOWN",        "name": "APs Down / Offline"},
                {"id": "SWITCH_DOWN",    "name": "Switches Down / Offline"},
                {"id": "GATEWAY_DOWN",   "name": "Gateways Down / Offline"},
                {"id": "CLIENT_COUNT",   "name": "Connected Client Count"},
            ],
            "insights": [],
            "warning": None,
        }

    if not central_token.get("access_token"):
        return {"alerts": [], "insights": [], "warning": "No valid token — save & test connection first."}

    # Static fallback list of well-known Aruba Central alert types (used when no live alerts exist)
    KNOWN_ALERT_TYPES: dict[str, str] = {
        "AP_DOWN": "AP Down",
        "AP_UP": "AP Up",
        "ACCESS_POINT_DOWN": "Access Point Down",
        "CLIENT_ASSOCIATION_FAILURE": "Client Association Failure",
        "CLIENT_DHCP_FAILURE": "Client DHCP Failure",
        "CLIENT_DISCONNECTED": "Client Disconnected",
        "DHCP_POOL_EXHAUSTED": "DHCP Pool Exhausted",
        "IDS_AP_SPOOFED": "IDS AP Spoofed",
        "PORTAL_DOWN": "Portal Down",
        "RADIO_INTERFERENCE": "Radio Interference",
        "ROGUE_AP_DETECTED": "Rogue AP Detected",
        "SWITCH_DOWN": "Switch Down",
        "SWITCH_PORT_DOWN": "Switch Port Down",
        "TUNNEL_DOWN": "Tunnel Down",
        "UPLINK_FAILURE": "Uplink Failure",
        "VPN_TUNNEL_DOWN": "VPN Tunnel Down",
        "WIRELESS_CLIENT_ROAM": "Wireless Client Roam",
        "WIRELESS_INTERFERENCE": "Wireless Interference",
    }
    KNOWN_INSIGHT_CATEGORIES: dict[str, str] = {
        "CONNECTIVITY": "Connectivity",
        "PERFORMANCE": "Performance",
        "RELIABILITY": "Reliability",
        "SECURITY": "Security",
    }

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    alert_types: dict[str, str] = {}
    insight_categories: dict[str, str] = {}
    warnings: list[str] = []

    # 30-day lookback window to catch historical alert types even when none are active now
    thirty_days_ago = int(time.time()) - 30 * 86400

    async with httpx.AsyncClient() as client:
        # Alerts — try v1 then v2 (v2 is 404 on some clusters)
        for alerts_path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
            try:
                resp = await client.get(
                    f"{base_url}{alerts_path}",
                    headers=headers,
                    params={"limit": 1000, "from_timestamp": thirty_days_ago},
                    timeout=20,
                )
                logger.info("Central available alerts %s → %s", alerts_path, resp.status_code)
                if resp.status_code == 200:
                    for alert in resp.json().get("alerts", []):
                        atype = alert.get("alert_type") or alert.get("type", "")
                        aname = alert.get("alert_type_name") or atype.replace("_", " ").title()
                        if atype:
                            alert_types[atype] = aname
                    break  # success — stop trying
                if resp.status_code == 404:
                    continue  # try next path
                if resp.status_code == 401:
                    warnings.append("Token rejected (401) fetching alerts.")
                    break
            except Exception as exc:
                logger.warning("Could not fetch alert types from %s: %s", alerts_path, exc)
                warnings.append(f"Network error fetching alerts: {exc}")
                break

        # Insights
        try:
            resp = await client.get(
                f"{base_url}/aiops/v1/insights",
                headers=headers,
                params={"limit": 1000, "from_timestamp": thirty_days_ago},
                timeout=20,
            )
            logger.info("Central available insights → %s", resp.status_code)
            if resp.status_code == 200:
                for insight in resp.json().get("insights", []):
                    cat = insight.get("category") or insight.get("type", "")
                    cat_name = insight.get("category_name") or cat.replace("_", " ").title()
                    if cat:
                        insight_categories[cat] = cat_name
            elif resp.status_code not in (404,):
                warnings.append(f"Insights endpoint returned HTTP {resp.status_code}.")
        except Exception as exc:
            logger.warning("Could not fetch insight categories: %s", exc)
            warnings.append(f"Network error fetching insights: {exc}")

    # If live API returned nothing, fall back to the known static list
    using_fallback = False
    if not alert_types:
        alert_types = dict(KNOWN_ALERT_TYPES)
        using_fallback = True
    if not insight_categories:
        insight_categories = dict(KNOWN_INSIGHT_CATEGORIES)
        using_fallback = True
    if using_fallback:
        warnings.append("No live checks returned by Central — showing standard Aruba Central check types.")

    return {
        "alerts": [{"id": k, "name": v} for k, v in sorted(alert_types.items())],
        "insights": [{"id": k, "name": v} for k, v in sorted(insight_categories.items())],
        "warning": "; ".join(warnings) if warnings else None,
    }


@app.get("/api/central/status")
async def api_central_status() -> dict[str, Any]:
    """Current check status for all mapped sites."""
    return {
        "status": _central_status_payload(),
        "wireless_clients": dict(central_wireless_clients),
        "hardware_alerts": _hw_alerts_payload(),
        "client_count_status": _client_count_payload(),
        "site_mappings": settings.get("site_mappings", {}),
        "monitored_checks": settings.get("monitored_checks", []),
        "central_api": _public_central_api_settings(),
        "token_valid": bool(central_token.get("access_token") and time.time() < central_token["expires_at"]),
        "token_state": _central_token_state(),
    }


@app.get("/api/central/history")
async def api_central_history(
    site: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=24),
) -> dict[str, Any]:
    """Return history records, optionally filtered by wsite."""
    cutoff = time.time() - hours * 3600
    async with history_lock:
        records = [
            r for r in central_history
            if r["ts"] >= cutoff and (site is None or r["wsite"] == site)
        ]
    return {"records": records, "count": len(records)}


@app.get("/api/central/site-alerts")
async def api_central_site_alerts(site: str = Query(...)) -> dict[str, Any]:
    """Fetch current alerts from Central for a specific site name. Always returns 200."""
    if not _central_ready() or not central_token.get("access_token"):
        return {"alerts": [], "warning": "Central not configured or no valid token."}

    if _is_new_central_api():
        # New Central has no alerts endpoint — derive device-status alerts from /sites-health + /devices
        headers = _central_headers()
        base_url = _central_cfg()["cluster_url"].rstrip("/")
        alerts: list[dict[str, Any]] = []
        warning: str | None = None
        ts_now = int(time.time())

        async with httpx.AsyncClient() as client:
            # 1. Find site_id from sites-health so we can filter devices by site
            site_id: str | None = None
            health_score: int | None = None
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            health_score = int(item.get("healthScore", item.get("health_score", 100)))
                            break
                elif resp.status_code == 401:
                    warning = "Token rejected (401) — re-save settings."
            except Exception as exc:
                warning = f"Network error fetching site health: {exc}"

            if warning:
                return {"alerts": alerts, "count": 0, "warning": warning}

            # 2. Add site health alert if score is degraded
            if health_score is not None and health_score < 100:
                severity = "CRITICAL" if health_score < 50 else "MAJOR" if health_score < 80 else "MINOR"
                alerts.append({
                    "type": "SITE_HEALTH",
                    "name": "Site Health Score",
                    "severity": severity,
                    "state": "active",
                    "site": site,
                    "device": site,
                    "ts": ts_now,
                    "message": f"Site health score is {health_score}/100",
                })

            # 3. Fetch devices for this site and add down devices as alerts
            try:
                params: dict[str, Any] = {"limit": 500}
                if site_id:
                    params["filter"] = f"siteId eq '{site_id}'"
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=20,
                )
                if resp.status_code == 200:
                    _TYPE_MAP = {
                        "ACCESS_POINT": ("AP_DOWN", "AP Down"),
                        "SWITCH": ("SWITCH_DOWN", "Switch Down"),
                        "GATEWAY": ("GATEWAY_DOWN", "Gateway Down"),
                    }
                    for dev in resp.json().get("items", []):
                        # Post-filter by siteId in case the API ignored the OData filter param
                        if site_id and dev.get("siteId") and dev.get("siteId") != site_id:
                            continue
                        status = (dev.get("status") or "").upper()
                        if status in ("UP", "ONLINE"):
                            continue
                        dtype = (dev.get("deviceType") or "").upper()
                        atype, aname = _TYPE_MAP.get(dtype, ("DEVICE_DOWN", "Device Down"))
                        alerts.append({
                            "type": atype,
                            "name": aname,
                            "severity": "CRITICAL",
                            "state": "active",
                            "site": site,
                            "device": dev.get("deviceName") or dev.get("id") or "—",
                            "ts": ts_now,
                            "message": f"{dev.get('model', dtype)} — status: {dev.get('status', 'Unknown')} | IP: {dev.get('ipv4') or dev.get('ip', '—')}",
                        })
            except Exception as exc:
                logger.warning("CNX devices fetch failed for site-alerts: %s", exc)
                warning = f"Could not fetch device status: {exc}"

        if not alerts and not warning:
            warning = "All devices are up and site health is 100% — no issues detected."

        return {"alerts": alerts, "count": len(alerts), "warning": warning}

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    alerts: list[dict[str, Any]] = []
    warning: str | None = None
    thirty_days_ago = int(time.time()) - 30 * 86400

    async with httpx.AsyncClient() as client:
        for path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
            try:
                resp = await client.get(
                    f"{base_url}{path}",
                    headers=headers,
                    params={"site": site, "limit": 500, "from_timestamp": thirty_days_ago},
                    timeout=20,
                )
                logger.info("site-alerts %s for '%s' → %s", path, site, resp.status_code)
                if resp.status_code == 200:
                    for alert in resp.json().get("alerts", []):
                        alert_site = alert.get("site_name") or alert.get("site") or ""
                        if alert_site and site and alert_site.lower() != site.lower():
                            continue
                        alerts.append({
                            "type":     alert.get("alert_type") or alert.get("type", ""),
                            "name":     alert.get("alert_type_name") or alert.get("alert_type", ""),
                            "severity": alert.get("severity", ""),
                            "state":    alert.get("state", ""),
                            "site":     alert.get("site_name") or site,
                            "device":   alert.get("device_name") or alert.get("hostname", ""),
                            "ts":       alert.get("timestamp") or alert.get("raised_at", ""),
                            "message":  alert.get("details") or alert.get("description", ""),
                        })
                    break
                if resp.status_code == 404:
                    continue
                if resp.status_code == 401:
                    warning = "Token rejected (401)."
                    break
            except Exception as exc:
                logger.warning("site-alerts fetch error: %s", exc)
                warning = str(exc)
                break

    if not alerts and not warning:
        warning = "No alerts in the last 30 days for this site."

    return {"alerts": alerts, "count": len(alerts), "warning": warning}


@app.post("/api/central/poll")
async def api_central_poll() -> dict[str, Any]:
    """Trigger an immediate Central poll cycle."""
    if not _central_ready():
        raise HTTPException(status_code=422, detail="Central not configured.")
    async def _poll_with_client() -> None:
        async with httpx.AsyncClient() as client:
            await _poll_central_once(client)
    asyncio.create_task(_poll_with_client())
    return {"status": "ok", "message": "Poll started."}


@app.get("/api/central/sites")
async def api_central_sites() -> dict[str, Any]:
    """Fetch site list from Aruba Central API. Always returns 200 with sites[] and optional warning."""
    if not _central_ready():
        return {"sites": [], "warning": "Central not configured — enter Cluster URL and token in Setup first."}
    if not central_token.get("access_token"):
        return {"sites": [], "warning": "No valid token — click 'Save & Test Connection' in Setup first."}

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    sites: list[str] = []
    warning: str | None = None

    # Classic Central — try multiple known site endpoints
    CLASSIC_SITE_PATHS = [
        ("/monitoring/v2/sites", {"limit": 1000, "offset": 0}),
        ("/monitoring/v1/sites", {"limit": 1000, "offset": 0}),
        ("/central/v2/sites", {"limit": 1000, "offset": 0}),
    ]

    async with httpx.AsyncClient() as client:
        if _is_new_central_api():
            # New Central: sites come from sites-health
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers,
                    timeout=20,
                )
                logger.info("New Central sites-health → %s", resp.status_code)
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        name = item.get("siteName") or item.get("site_name") or item.get("name", "")
                        if name:
                            sites.append(name)
                elif resp.status_code == 401:
                    warning = "Token rejected (401) — re-save settings to refresh."
                else:
                    warning = f"sites-health returned HTTP {resp.status_code}."
            except Exception as exc:
                logger.warning("Could not fetch New Central sites-health: %s", exc)
                warning = f"Network error fetching sites: {exc}"
        else:
            # Classic Central: try each known path, stop on first 200
            last_status: int | None = None
            tried: list[str] = []
            for path, params in CLASSIC_SITE_PATHS:
                tried.append(path)
                try:
                    resp = await client.get(
                        f"{base_url}{path}",
                        headers=headers,
                        params=params,
                        timeout=20,
                    )
                    last_status = resp.status_code
                    logger.info("Classic Central sites %s → %s: %s", path, resp.status_code, resp.text[:200])
                    if resp.status_code == 200:
                        data = resp.json()
                        # Response may use "sites", "items", or root list
                        raw = data.get("sites") or data.get("items") or (data if isinstance(data, list) else [])
                        for site in raw:
                            if isinstance(site, str):
                                sites.append(site)
                            else:
                                name = site.get("site_name") or site.get("siteName") or site.get("name", "")
                                if name:
                                    sites.append(name)
                        break
                    elif resp.status_code == 401:
                        warning = "Token rejected (401) — re-save settings."
                        break
                    # 404 = path doesn't exist on this cluster, try next
                except Exception as exc:
                    logger.warning("Could not fetch Classic Central sites from %s: %s", path, exc)
                    warning = f"Network error fetching sites: {exc}"
                    break

            if not sites and not warning:
                warning = f"No sites found — tried {', '.join(tried)} (last HTTP {last_status}). Your cluster may not expose a sites list API."

    return {"sites": sorted(set(sites)), "warning": warning}


@app.get("/api/central/browse")
async def api_central_browse(force: bool = False) -> dict[str, Any]:
    """Return aggregated Central browse data for the spoke Central Monitoring tab.

    Serves a 5-minute server-side cache (same TTL as the hub) to avoid hammering
    the Central API on every tab open.  Pass ?force=true to bypass the cache.
    If the background browse cache is empty (first load before any poll cycle),
    trigger a live on-demand fetch so the caller always gets fresh data.
    """
    global _central_browse_response_cache, _central_browse_response_cached_at, _central_browse_fetching

    now = time.time()
    # Serve the cached response if it's still within TTL and not a forced refresh.
    if not force and _central_browse_response_cache and (now - _central_browse_response_cached_at) < NC_BROWSE_SERVER_CACHE_TTL_S:
        return _central_browse_response_cache

    # If the background loop hasn't populated browse data yet, do an on-demand fetch.
    # Guard with a flag so concurrent requests don't each spawn their own fetch.
    if not central_browse_alerts and not central_browse_insights and not central_browse_clients and not central_browse_clients_by_site and _central_ready():
        if not _central_browse_fetching:
            _central_browse_fetching = True
            try:
                async with httpx.AsyncClient() as _browse_client:
                    await _fetch_nc_browse_for_spoke(_browse_client)
            except Exception as exc:
                logger.warning("api_central_browse: on-demand fetch failed: %s", exc)
            finally:
                _central_browse_fetching = False

    sites_resp = await api_central_sites()
    site_names = list(sites_resp.get("sites") or [])
    sites_with_health: list[dict[str, Any]] = []

    for site_name in site_names:
        site_alerts = [a for a in central_browse_alerts if str(a.get("site") or "").strip().lower() == str(site_name).strip().lower()]
        severities = {str(a.get("severity") or "").strip().lower() for a in site_alerts}
        critical = bool(severities & {"critical", "major", "poor", "red", "orange", "error"})
        fair = bool(severities & {"minor", "warning", "yellow"})
        health_label = "Poor" if critical else ("Fair" if fair else "Healthy")
        health_score = 30 if critical else (60 if fair else 90)
        clients_info = central_browse_clients_by_site.get(site_name, {}) or {}
        wireless_count = clients_info.get("wireless_clients")
        if wireless_count is None:
            wireless_count = clients_info.get("wireless")
        if wireless_count is None:
            wireless_count = clients_info.get("count")
        sites_with_health.append({
            "name": site_name,
            "health_label": health_label,
            "health_score": health_score,
            "wireless_clients": wireless_count,
            "central_site": site_name,
        })

    result: dict[str, Any] = {
        "mode": settings.get("central_api", {}).get("mode") or ("central" if _is_new_central_api() else "classic"),
        "cached_at": now,
        "sites": sites_with_health,
        "alerts": list(central_browse_alerts),
        "insights": list(central_browse_insights),
        "clients": list(central_browse_clients),
        "clients_by_site": dict(central_browse_clients_by_site),
        "devices_by_site": dict(central_browse_devices_by_site),
        "warning": sites_resp.get("warning"),
    }
    _central_browse_response_cache = result
    _central_browse_response_cached_at = now
    return result


@app.post("/api/central/monitor-site")
async def api_central_monitor_site(body: dict[str, Any] = Body(...), _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Add or remove a Central site from the spoke site mappings."""
    action = str(body.get("action") or "add").strip().lower()
    central_site = str(body.get("central_site") or "").strip()
    if not central_site:
        raise HTTPException(status_code=422, detail="central_site required")

    mappings = dict(settings.get("site_mappings") or {})
    if action == "add":
        wsite = str(body.get("wsite") or central_site).strip() or central_site
        mappings[wsite] = central_site
    elif action == "remove":
        target = central_site.lower()
        to_remove = [k for k, v in mappings.items() if str(v or "").strip().lower() == target or str(k or "").strip().lower() == target]
        for key in to_remove:
            mappings.pop(key, None)
    else:
        raise HTTPException(status_code=422, detail="action must be add or remove")

    settings["site_mappings"] = mappings
    _persisted["site_mappings"] = mappings
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True, "action": action, "central_site": central_site, "site_mappings": mappings}


@app.post("/api/central/monitored-items")
async def api_central_add_monitored_item(body: dict[str, Any] = Body(...), _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Add an item to the spoke's local Central monitored-items list."""
    item_type = str(body.get("type") or "").strip()
    name = str(body.get("name") or "").strip()
    identifier = str(body.get("identifier") or body.get("name") or "").strip()
    if not item_type or not identifier:
        raise HTTPException(status_code=422, detail="type and identifier required")

    items = list(settings.get("spoke_monitored_items") or [])
    site = str(body.get("site") or "").strip()
    for existing in items:
        if str(existing.get("type") or "") != item_type:
            continue
        if str(existing.get("identifier") or existing.get("name") or "").strip().lower() != identifier.lower():
            continue
        if str(existing.get("site") or "").strip().lower() != site.lower():
            continue
        return {"ok": True, "item": existing}

    item = {
        "id": str(uuid.uuid4()),
        "type": item_type,
        "name": name,
        "site": site,
        "identifier": identifier,
        "ts": time.time(),
    }
    items.append(item)
    settings["spoke_monitored_items"] = items
    _persisted["spoke_monitored_items"] = items
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True, "item": item}


@app.delete("/api/central/monitored-items/{item_id}")
async def api_central_remove_monitored_item(item_id: str, _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Remove an item from the spoke's local Central monitored-items list."""
    items = list(settings.get("spoke_monitored_items") or [])
    items = [item for item in items if str(item.get("id") or "") != item_id]
    settings["spoke_monitored_items"] = items
    _persisted["spoke_monitored_items"] = items
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True}


@app.get("/api/central/devices")
async def api_central_devices(site: str | None = Query(default=None)) -> dict[str, Any]:
    """Return device inventory from New Central v1alpha1. Always returns 200.
    Optional ?site= filters to a specific Central site name.
    Classic Central: returns empty (use monitoring/v1/devices instead).
    """
    if not _central_ready() or not central_token.get("access_token"):
        return {"devices": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"devices": [], "count": 0, "warning": "Device inventory endpoint only available in Central (CNX) mode."}

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    devices: list[dict[str, Any]] = []
    warning: str | None = None

    async with httpx.AsyncClient() as client:
        # Resolve site_id if a site name was provided
        site_id: str | None = None
        if site:
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            break
            except Exception as exc:
                logger.warning("CNX devices: sites-health lookup failed: %s", exc)

        # Fetch devices, optionally filtered by site
        try:
            params: dict[str, Any] = {"limit": 500}
            if site_id:
                params["filter"] = f"siteId eq '{site_id}'"
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/devices",
                headers=headers, params=params, timeout=30,
            )
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=30,
                )
            if resp.status_code == 200:
                for dev in resp.json().get("items", []):
                    devices.append({
                        "id":         dev.get("id") or dev.get("deviceId") or dev.get("serialNumber", ""),
                        "name":       dev.get("deviceName") or dev.get("name", "—"),
                        "type":       dev.get("deviceType") or dev.get("type", "—"),
                        "model":      dev.get("model", "—"),
                        "serial":     dev.get("serialNumber", "—"),
                        "mac":        dev.get("macAddress", "—"),
                        "ip":         dev.get("ipv4") or dev.get("ip") or dev.get("ipAddress", "—"),
                        "status":     dev.get("status", "—"),
                        "site":       dev.get("siteId", "—"),
                        "version":    dev.get("softwareVersion") or dev.get("firmwareVersion", "—"),
                        "uptime_ms":  dev.get("uptimeInMillis"),
                        "deployment": dev.get("deployment", "—"),
                    })
                # Post-filter by siteId in case the API ignored the OData filter param
                if site_id:
                    devices = [d for d in devices if d["site"] == site_id]
            elif resp.status_code == 401:
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"Devices endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            logger.warning("CNX devices fetch failed: %s", exc)
            warning = f"Network error fetching devices: {exc}"

    return {"devices": devices, "count": len(devices), "warning": warning}


@app.get("/api/central/wlans")
async def api_central_wlans() -> dict[str, Any]:
    """Return WLAN/SSID list from New Central v1alpha1. Always returns 200."""
    if not _central_ready() or not central_token.get("access_token"):
        return {"wlans": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"wlans": [], "count": 0, "warning": "WLAN endpoint only available in Central (CNX) mode."}

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    wlans: list[dict[str, Any]] = []
    warning: str | None = None

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/wlans",
                headers=headers, params={"limit": 500}, timeout=20,
            )
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/wlans",
                    headers=headers, params={"limit": 500}, timeout=20,
                )
            if resp.status_code == 200:
                for w in resp.json().get("items", []):
                    wlans.append({
                        "id":       w.get("wlanId") or w.get("id", ""),
                        "ssid":     w.get("ssid", "—"),
                        "type":     w.get("type", "—"),
                        "security": w.get("security", "—"),
                        "enabled":  w.get("enabled", True),
                        "band":     w.get("band") or w.get("radioType", "—"),
                    })
            elif resp.status_code == 401:
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"WLANs endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            logger.warning("CNX wlans fetch failed: %s", exc)
            warning = f"Network error fetching WLANs: {exc}"

    return {"wlans": wlans, "count": len(wlans), "warning": warning}


@app.get("/api/central/clients-detail")
async def api_central_clients_detail(site: str | None = Query(default=None)) -> dict[str, Any]:
    """Return connected client list from New Central v1alpha1. Always returns 200.
    Optional ?site= filters to a specific Central site name.
    """
    if not _central_ready() or not central_token.get("access_token"):
        return {"clients": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"clients": [], "count": 0, "warning": "Client detail endpoint only available in Central (CNX) mode."}

    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    result_clients: list[dict[str, Any]] = []
    warning: str | None = None

    async with httpx.AsyncClient() as client:
        site_id: str | None = None
        if site:
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            break
            except Exception as exc:
                logger.warning("CNX clients-detail: sites-health lookup failed: %s", exc)

        try:
            params: dict[str, Any] = {}
            if site_id:
                params["site-id"] = site_id
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/clients",
                headers=headers, params=params, timeout=20,
            )
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/clients",
                    headers=headers, params=params, timeout=20,
                )
            if resp.status_code == 200:
                for c in resp.json().get("items", []):
                    result_clients.append({
                        "mac":             c.get("macAddress", "—"),
                        "ip":              c.get("ipAddress", "—"),
                        "username":        c.get("username") or c.get("name", "—"),
                        "device":          c.get("deviceName") or c.get("hostname", "—"),
                        "connection_type": c.get("connectionType", "—"),
                        "ssid":            c.get("ssid", "—"),
                        "ap":              c.get("apName") or c.get("accessPoint", "—"),
                        "connected":       c.get("connected", True),
                        "signal":          c.get("signalStrength") or c.get("signal"),
                    })
            elif resp.status_code == 401:
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"Clients endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            logger.warning("CNX clients-detail fetch failed: %s", exc)
            warning = f"Network error fetching clients: {exc}"

    return {"clients": result_clients, "count": len(result_clients), "warning": warning}


@app.get("/api/local-wsites")
async def api_local_wsites() -> dict[str, Any]:
    """Extract unique wsite values from simulation.conf in the repo."""
    import configparser
    config_path = repo_path("configs", "simulation.conf")
    if not config_path.exists():
        return {"wsites": []}
    parser = configparser.ConfigParser()
    try:
        parser.read_string(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not parse simulation.conf: %s", exc)
        return {"wsites": []}
    wsites: set[str] = set()
    for section in parser.sections():
        if parser.has_option(section, "wsite"):
            val = parser.get(section, "wsite").strip()
            if val:
                wsites.add(val)
    return {"wsites": sorted(wsites)}


@app.get("/api/simulations")
async def api_simulations() -> dict[str, Any]:
    """Return simulation groups with client membership and Central PASS/FAIL status.

    Reads configs/simulation.conf for bucket profiles and proxmox/client-setup.conf
    for VMID→username mappings. Matches configured clients against live heartbeats
    and looks up Central alert status per simulation wsite + central_check.
    """
    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    client_conf_path = REPO_DIR / "proxmox" / "client-setup.conf"

    sim_mtime = sim_conf_path.stat().st_mtime if sim_conf_path.exists() else -1.0
    client_mtime = client_conf_path.stat().st_mtime if client_conf_path.exists() else -1.0

    if (sim_mtime == _sim_conf_cache["sim_mtime"] and
            client_mtime == _sim_conf_cache["client_mtime"]):
        simulations: dict[str, dict[str, Any]] = copy.deepcopy(_sim_conf_cache["simulations"])
    else:
        simulations = {}

        # ── Parse simulation.conf ─────────────────────────────────────
        if sim_conf_path.exists():
            try:
                parser = configparser.ConfigParser()
                parser.read_string(sim_conf_path.read_text(encoding="utf-8"))
                # Apply hub-managed override on top (hub-connected mode only)
                _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")

                # Per-bucket test keys (read from [sN] sections)
                _BUCKET_TEST_KEYS = [
                    "dns_fail", "assoc_fail", "dhcp_fail", "port_flap",
                    "iperf", "www_traffic", "download", "ping_test",
                ]
                # Global test keys (read from [simulation] section, applied to all buckets)
                # WHY: ssidpw_fail and auth_fail are global settings in simulation.conf
                # but simulation.sh/dashboard.sh include them in active_simulations POSTs.
                _GLOBAL_TEST_KEYS = ["ssidpw_fail", "auth_fail"]
                global_tests = {
                    k: parser.get("simulation", k, fallback="off").strip().lower() == "on"
                    for k in _GLOBAL_TEST_KEYS
                }

                sim_section_re = re.compile(r"^s\d$")
                for section in parser.sections():
                    if not sim_section_re.match(section):
                        continue
                    bucket_tests = {
                        k: parser.get(section, k, fallback="off").strip().lower() == "on"
                        for k in _BUCKET_TEST_KEYS
                    }
                    simulations[section] = {
                        "id": section,
                        "wsite": parser.get(section, "wsite", fallback=""),
                        "central_check": parser.get(section, "central_check", fallback="").strip(),
                        "tests": {**bucket_tests, **global_tests},
                        "configured_clients": [],
                        "active_client_count": 0,
                        "central_pass_fail": None,
                    }
            except Exception as exc:
                logger.warning("api_simulations: could not parse simulation.conf: %s", exc)

        # ── Parse client-setup.conf — build VMID→hostname mapping ────
        if client_conf_path.exists():
            try:
                client_parser = configparser.ConfigParser()
                client_parser.read_string(client_conf_path.read_text(encoding="utf-8"))

                vmid_section_re = re.compile(r"^c(\d+)$")
                for section in client_parser.sections():
                    m = vmid_section_re.match(section)
                    if not m:
                        continue
                    vmid_str = m.group(1)
                    vmid = int(vmid_str)
                    vm_name = client_parser.get(section, "vm_name", fallback="").strip()
                    if not vm_name:
                        continue

                    # Hash the vm_name to assign bucket — matches zlib.crc32 used by clients.
                    sim_id = f"s{zlib.crc32(vm_name.encode()) % 10}"

                    if sim_id in simulations:
                        simulations[sim_id]["configured_clients"].append({
                            "hostname": vm_name,
                            "vmid": vmid,
                            "username": vm_name,
                            "reporting": False,
                            "online": False,
                            "last_seen": None,
                        })
            except Exception as exc:
                logger.warning("api_simulations: could not parse client-setup.conf: %s", exc)

        _sim_conf_cache.update({
            "sim_mtime": sim_mtime,
            "client_mtime": client_mtime,
            "simulations": copy.deepcopy(simulations),
        })

    # ── Match active clients + compute Central PASS/FAIL ─────────
    async with state_lock:
        active_snap = {h: dict(c) for h, c in clients.items()}

    for sim in simulations.values():
        active_count = 0

        # Primary: count any live client whose simulation_id matches this bucket
        for h, c in active_snap.items():
            if c.get("simulation_id", "") == sim["id"]:
                online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
                if online:
                    active_count += 1

        # Secondary: update configured_clients reporting flags (for detail panel)
        for client_info in sim["configured_clients"]:
            h = client_info["hostname"]
            if h in active_snap:
                c = active_snap[h]
                online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
                last_seen_dt = c.get("last_seen")
                client_info["reporting"] = True
                client_info["online"] = online
                client_info["last_seen"] = last_seen_dt.isoformat() if last_seen_dt else None

        sim["active_client_count"] = active_count
        sim["central_client_count"] = central_wireless_clients.get(sim["wsite"], None)

        # Central PASS/FAIL — look up wsite + central_check in polled status
        wsite = sim["wsite"]
        check_id = sim["central_check"]
        if wsite and check_id:
            site_checks = central_status.get(wsite, {})
            if check_id in site_checks:
                info = site_checks[check_id]
                sim["central_pass_fail"] = {
                    "firing": info["status"] == "OK",
                    "count": info["count"],
                    "check_name": info["check_name"],
                    "ts": info["ts"],
                }
            else:
                sim["central_pass_fail"] = {"firing": False, "count": 0, "check_name": check_id, "ts": None}

    return {
        "simulations": list(simulations.values()),
    }


# Cache: (wsite, central_site) → (timestamp, [client_name, ...])
_central_client_cache: dict[str, tuple[float, list[str]]] = {}
_CENTRAL_CLIENT_CACHE_TTL = 60  # seconds


async def _fetch_central_client_names(wsite: str, central_site: str) -> list[str]:
    """Fetch wireless client hostnames from Central for a given site (cached 60 s)."""
    cache_key = f"{wsite}:{central_site}"
    now = time.time()
    if cache_key in _central_client_cache:
        ts, names = _central_client_cache[cache_key]
        if now - ts < _CENTRAL_CLIENT_CACHE_TTL:
            return names

    cfg = _central_cfg()
    if not cfg.get("access_token") and not cfg.get("client_id"):
        return []

    base_url = cfg["cluster_url"].rstrip("/")
    headers = _central_headers()
    names: list[str] = []

    async with httpx.AsyncClient() as client:
        for path in ["/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"]:
            for site_param in ["site", "site_name"]:
                try:
                    resp = await asyncio.wait_for(
                        client.get(
                            f"{base_url}{path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1000},
                            timeout=10,
                        ),
                        timeout=12,
                    )
                    if resp.status_code == 401 and _can_refresh():
                        ok, _ = await _refresh_central_token(client)
                        if ok:
                            headers = _central_headers()
                        resp = await client.get(
                            f"{base_url}{path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1000},
                            timeout=10,
                        )
                    if resp.status_code == 200:
                        body = resp.json()
                        for c in body.get("clients", []):
                            n = (c.get("name") or c.get("client_name") or
                                 c.get("username") or "").strip().lower()
                            if n:
                                names.append(n)
                        _central_client_cache[cache_key] = (now, names)
                        return names
                    if resp.status_code == 404:
                        continue
                except Exception:
                    pass

    return names


@app.get("/api/simulations/{sim_id}/clients")
async def api_sim_clients(sim_id: str) -> dict[str, Any]:
    """Return per-client status for one simulation bucket.

    Each client entry includes:
      - api_online / api_last_seen — from live heartbeats
      - central_connected — matched by hostname from Central wireless client list
    """
    import configparser as _cp

    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    client_conf_path = REPO_DIR / "proxmox" / "client-setup.conf"

    # --- Load simulation profile ---
    wsite = ""
    central_site = ""
    if sim_conf_path.exists():
        try:
            p = _cp.ConfigParser()
            p.read_string(sim_conf_path.read_text(encoding="utf-8"))
            _merge_ini_override(p, REPO_DIR / "configs" / "hub-sim-overrides.conf")
            if p.has_section(sim_id):
                wsite = p.get(sim_id, "wsite", fallback="")
        except Exception:
            pass

    central_site = settings.get("site_mappings", {}).get(wsite, "")

    # --- Build configured client list from client-setup.conf ---
    configured: dict[str, dict[str, Any]] = {}  # hostname → info
    if client_conf_path.exists():
        try:
            cp = _cp.ConfigParser()
            cp.read_string(client_conf_path.read_text(encoding="utf-8"))
            vmid_re = re.compile(r"^c(\d+)$")
            for section in cp.sections():
                m = vmid_re.match(section)
                if not m:
                    continue
                vmid_str = m.group(1)
                vm_name = cp.get(section, "vm_name", fallback="").strip()
                if not vm_name:
                    continue
                if f"s{zlib.crc32(vm_name.encode()) % 10}" != sim_id:
                    continue
                configured[vm_name] = {
                    "hostname": vm_name,
                    "vmid": int(vmid_str),
                    "api_online": False,
                    "api_last_seen": None,
                    "central_connected": None,
                    "source": "configured",
                }
        except Exception:
            pass

    # --- Overlay live heartbeat data ---
    async with state_lock:
        active_snap = {h: dict(c) for h, c in clients.items()}

    for h, c in active_snap.items():
        if c.get("simulation_id", "") != sim_id:
            continue
        online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
        last_seen_dt = c.get("last_seen")
        active_sims = list(c.get("active_simulations", []))
        if h in configured:
            configured[h]["api_online"] = online
            configured[h]["api_last_seen"] = last_seen_dt.isoformat() if last_seen_dt else None
            configured[h]["active_simulations"] = active_sims
        else:
            configured[h] = {
                "hostname": h,
                "vmid": None,
                "api_online": online,
                "api_last_seen": last_seen_dt.isoformat() if last_seen_dt else None,
                "active_simulations": active_sims,
                "central_connected": None,
                "source": "heartbeat",
            }

    # --- Match against Central client list ---
    central_names: list[str] = []
    if central_site:
        try:
            central_names = await asyncio.wait_for(
                _fetch_central_client_names(wsite, central_site), timeout=15
            )
        except Exception:
            pass

    central_set = {n.lower() for n in central_names}
    for info in configured.values():
        if central_set:
            info["central_connected"] = info["hostname"].lower() in central_set
        # else leave None (not configured / fetch failed)

    return {
        "sim_id": sim_id,
        "wsite": wsite,
        "central_site": central_site,
        "central_total": central_wireless_clients.get(wsite, None),
        "clients": sorted(configured.values(), key=lambda x: x["hostname"]),
    }


@app.get("/api/hardware-alerts")
async def api_hardware_alerts() -> dict[str, Any]:
    """Return configured hardware checks merged with current alert device data."""
    return {"hardware_alerts": _hw_alerts_payload()}



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


@app.post("/api/sync-now")
async def api_sync_now() -> dict[str, Any]:
    """Trigger an immediate GitHub sync outside the normal interval."""
    if "sync_repo" in background_tasks:
        background_tasks["sync_repo"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_tasks["sync_repo"]
    repo_state["synced"] = False
    repo_state["error"] = None
    background_tasks["sync_repo"] = asyncio.create_task(sync_repo())
    await broadcast({"type": "repo_status", "synced": False, "error": None, "last_sync": repo_state["last_sync"]})
    return {"status": "ok", "message": "GitHub sync started"}


@app.get("/api/version")
async def api_version() -> dict[str, Any]:
    """Return installed and available installer versions."""
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "current_version": update_state["current_version"],
        "available_version": update_state["available_version"],
        "update_available": update_state["update_available"],
        "last_checked": update_state["last_checked"],
        "update_in_progress": update_state["update_in_progress"],
        "cswebui_current": update_state.get("cswebui_current") or APP_VERSION,
        "cswebui_available": update_state.get("cswebui_available"),
    }


@app.post("/api/update-all")
async def api_update_all() -> dict[str, Any]:
    """Queue the shared Proxmox update command, then self-update the WebUI."""
    if update_all_state["running"]:
        raise HTTPException(status_code=409, detail="Update All already in progress")
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="WebUI update already in progress")
    has_approved_agents = bool(approved_proxmox_agents)
    update_all_state.update({
        "running": True,
        "phase": "agents" if has_approved_agents else "webui",
        "total_agents": 1 if has_approved_agents else 0,
        "completed_agents": 0,
        "failed_agents": 0,
        "agent_cmds": [],
        "started_at": time.time(),
        "error": None,
    })
    await broadcast({"type": "update_all_progress", **update_all_state})
    asyncio.create_task(_run_update_all())
    return {"status": "ok", "message": "Update All started"}


@app.post("/api/self-update")
async def api_self_update() -> dict[str, Any]:
    """Manually trigger a self-update check and apply if a new version is available."""
    if update_state["update_in_progress"]:
        raise HTTPException(status_code=409, detail="Update already in progress")
    # Sync from GitHub first so version check reflects the latest repo state
    try:
        await _sync_repo_now()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub sync failed: {exc}") from exc
    # Now check version against freshly synced repo
    available = await asyncio.to_thread(_get_repo_version)
    import datetime
    update_state["available_version"] = available
    update_state["last_checked"] = datetime.datetime.now().isoformat(timespec="seconds")
    update_state["update_available"] = (
        available is not None and available != update_state["current_version"]
    )
    await _broadcast_update_state()
    if not update_state["update_available"]:
        return {"status": "ok", "message": f"Already up to date (v{update_state['current_version']})"}
    asyncio.create_task(_run_self_update())
    return {"status": "ok", "message": f"Update to v{available} started — service will restart shortly"}


@app.post("/api/refresh-webui")
async def api_refresh_webui() -> dict[str, Any]:
    """Download and apply the latest cs-webui frontend files (app.js, style.css, index.html)
    without a full reinstall or service restart.  The browser just needs a hard-refresh
    (Ctrl+Shift+R) after this returns to pick up the new files."""
    try:
        await asyncio.wait_for(refresh_webui_frontend(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Frontend refresh timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    local_ver: str | None = None
    try:
        local_ver = (STATIC_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return {"status": "ok", "version": local_ver, "message": f"Frontend updated to v{local_ver} — do a hard-refresh (Ctrl+Shift+R)"}




@app.get("/api/acme")
async def api_acme_get() -> dict[str, Any]:
    cfg = spoke_acme.load_acme_config()
    data = _public_acme_settings(cfg)
    data["cert_info"] = spoke_acme.get_cert_info()
    data["spoke_tls"] = settings.get("spoke_tls", "off")
    return data


@app.post("/api/acme")
async def api_acme_update(payload: dict[str, Any]) -> dict[str, Any]:
    existing = spoke_acme.load_acme_config()
    incoming_credentials = payload.get("dns_credentials") or {}
    merged_credentials = dict(existing.dns_credentials or {})
    for key, value in incoming_credentials.items():
        if value in (None, "***"):
            continue
        merged_credentials[key] = value
    cfg = spoke_acme.AcmeConfig(
        enabled=bool(payload.get("enabled", existing.enabled)),
        domain=str(payload.get("domain", existing.domain) or "").strip(),
        email=str(payload.get("email", existing.email) or "").strip(),
        challenge=str(payload.get("challenge", existing.challenge) or existing.challenge),
        ca=str(payload.get("ca", existing.ca) or existing.ca),
        dns_provider=str(payload.get("dns_provider", existing.dns_provider) or "").strip(),
        dns_credentials=merged_credentials,
        last_renewed=existing.last_renewed,
        last_error=existing.last_error,
        cert_expiry=existing.cert_expiry,
    )
    spoke_acme.save_acme_config(cfg)
    if "spoke_tls" in payload:
        settings["spoke_tls"] = _normalize_toggle(payload.get("spoke_tls"))
        _save_settings()
    data = _public_acme_settings(cfg)
    data["cert_info"] = spoke_acme.get_cert_info()
    data["spoke_tls"] = settings.get("spoke_tls", "off")
    return data


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


@app.post("/api/acme/request")
async def api_acme_request() -> dict[str, Any]:
    if _acme_status.get("running"):
        return {"status": "running"}
    asyncio.create_task(_run_acme_request())
    return {"status": "started"}


@app.get("/api/acme/status")
async def api_acme_status() -> dict[str, Any]:
    return dict(_acme_status)


@app.get("/api/config", response_class=PlainTextResponse)
async def api_config(hostname: str | None = Query(default=None)) -> str:
    config_path = repo_path("configs", "simulation.conf")
    config_text = config_path.read_text(encoding="utf-8")

    # Apply hub-managed override (hub-connected mode) by serialising the merged parser back to text
    hub_override_path = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    if hub_override_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(config_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            config_text = buf.getvalue()
        except Exception as exc:
            logger.warning("Could not apply hub-sim-overrides for /api/config: %s", exc)

    if not hostname:
        return config_text

    async with state_lock:
        client = clients.get(hostname)
        if not client or not client.get("overrides"):
            return config_text
        return apply_overrides(config_text, client)


@app.get("/api/config/overrides", response_class=PlainTextResponse)
async def api_config_overrides() -> str:
    overrides_path = repo_path("configs", "user-overrides.conf")
    base_text = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    # Apply hub-managed user-overrides on top
    hub_override_path = REPO_DIR / "configs" / "hub-user-overrides.conf"
    if hub_override_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(base_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            return buf.getvalue()
        except Exception as exc:
            logger.warning("Could not apply hub-user-overrides for /api/config/overrides: %s", exc)
    return base_text


@app.get("/api/config/parsed")
async def api_config_parsed() -> dict[str, dict[str, str]]:
    config_path = repo_path("configs", "simulation.conf")
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(config_path, encoding="utf-8")
    _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")
    return {section: dict(parser.items(section)) for section in parser.sections()}


@app.post("/api/config/simulation")
async def api_config_simulation(update: SimulationConfigUpdate) -> dict[str, Any]:
    section = update.section.strip()
    if section not in ALLOWED_CONFIG_SECTIONS:
        raise HTTPException(status_code=422, detail="Invalid section name")

    config_path = repo_path("configs", "simulation.conf")
    updates = {str(key).strip(): str(value) for key, value in update.updates.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, config_path, section, updates)

    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/simulation.conf"],
                f"WebUI: update [{section}] settings",
            )
    except ValueError:
        pushed = False

    # When the kill switch is turned OFF, immediately push the change down to
    # all clients via the command inbox so they don't stay stuck in the
    # kill-switch loop waiting for their next exec-restart cycle (up to 5 min).
    # IMPORTANT: expand "all" to per-client commands at creation time — the
    # inbox filter matches exact hostname, so a single target="all" command
    # would never be delivered to any client.
    if section == "simulation" and updates.get("kill_switch") == "off":
        async with state_lock:
            known = list(clients.keys())
            targets = known or ["all"]
            for hostname in targets:
                _enqueue_command_locked(hostname, "kill_switch", {"value": "off"})
            serialized = _serialize_commands()
        await broadcast({"type": "commands_update", "commands": serialized})
        await _push_pending_commands_for_targets(targets)

    return {"status": "ok", "pushed": pushed}


@app.post("/api/config/overrides/save")
async def api_config_overrides_save(update: OverridesSaveRequest) -> dict[str, Any]:
    ensure_repo_ready()
    username = update.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")

    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    section = "simulation" if username == "__global__" else username
    flags = {str(key).strip(): str(value) for key, value in update.flags.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, overrides_path, section, flags)

    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/user-overrides.conf"],
                f"WebUI: update overrides for {username}",
            )
    except ValueError:
        pushed = False

    return {"status": "ok", "pushed": pushed}


@app.get("/api/config/user-overrides-conf")
async def api_get_user_overrides_conf() -> dict[str, str]:
    """Return user-overrides.conf content as JSON {content, mode}."""
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    content = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    return {
        "content": content,
        "mode": "local",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


class ConfOverrideBody(BaseModel):
    content: str  # Raw INI text, same format as the target .conf file


@app.put("/api/config/user-overrides-conf")
async def api_put_user_overrides_conf(body: ConfOverrideBody) -> dict[str, Any]:
    """Write the entire user-overrides.conf and push to GitHub."""
    ensure_repo_ready()
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = overrides_path.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(overrides_path)
    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/user-overrides.conf"],
                "WebUI: update user-overrides.conf",
            )
    except ValueError:
        pushed = False
    return {"ok": True, "pushed": pushed}


@app.get("/api/config/hub-sim-override", response_class=PlainTextResponse)
async def api_get_hub_sim_override() -> str:
    """Return the current hub-managed simulation.conf override, or empty string."""
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@app.put("/api/config/hub-sim-override")
async def api_set_hub_sim_override(body: ConfOverrideBody) -> dict[str, Any]:
    """Write hub-managed simulation.conf override locally (standalone mode).

    In hub-connected mode this file is managed by the hub via config_update;
    this endpoint supports direct editing from the spoke UI when disconnected.
    """
    ensure_repo_ready()
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(p)
    _sim_conf_cache["sim_mtime"] = -1.0  # Invalidate cache
    return {"ok": True}


@app.delete("/api/config/hub-sim-override")
async def api_clear_hub_sim_override() -> dict[str, Any]:
    """Remove hub-managed simulation.conf override — reverts to GitHub file."""
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    if p.exists():
        p.unlink()
    _sim_conf_cache["sim_mtime"] = -1.0
    return {"ok": True, "cleared": True}


@app.get("/api/config/hub-user-override", response_class=PlainTextResponse)
async def api_get_hub_user_override() -> str:
    """Return the current hub-managed user-overrides.conf override, or empty string."""
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@app.put("/api/config/hub-user-override")
async def api_set_hub_user_override(body: ConfOverrideBody) -> dict[str, Any]:
    """Write hub-managed user-overrides.conf override locally (standalone mode)."""
    ensure_repo_ready()
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(p)
    return {"ok": True}


@app.delete("/api/config/hub-user-override")
async def api_clear_hub_user_override() -> dict[str, Any]:
    """Remove hub-managed user-overrides.conf override — reverts to GitHub file."""
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    if p.exists():
        p.unlink()
    return {"ok": True, "cleared": True}


@app.get("/api/scripts/list")
async def api_scripts_list(platform: str = Query(...)) -> list[str]:
    platform = validate_platform(platform)
    scripts_dir = repo_path(platform)
    if not scripts_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Script directory not found for {platform}")
    return sorted(path.name for path in scripts_dir.iterdir() if path.is_file())


@app.get("/api/scripts/{platform}/{filename}")
async def api_scripts_get(platform: str, filename: str) -> FileResponse:
    platform = validate_platform(platform)
    scripts_dir = repo_path(platform).resolve()
    candidate = (scripts_dir / filename).resolve()

    if candidate.parent != scripts_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Script file not found")

    return FileResponse(candidate)


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


@app.post("/api/status")
async def api_status(status: ClientStatus) -> dict[str, Any]:
    payload, watchdog_changed, ignored = await _apply_client_status(status)
    if ignored is not None:
        return ignored
    await broadcast({"type": "status_update", "client": payload})
    if watchdog_changed:
        await _broadcast_proxmox_state()
    asyncio.create_task(_sync_sim_tags_for_client(status.hostname))
    return {"status": "ok", "client": payload, "throttle_interval": _server_pressure["throttle_interval"]}


@app.get("/api/client/key")
async def api_client_key() -> dict[str, str]:
    """Return the shared client API key so agents can authenticate to /ws/client.
    This endpoint is intentionally public — agents need the key before they can connect.
    The spoke URL itself acts as the first factor of access control."""
    return {"client_api_key": str(settings.get("client_api_key", "") or "")}


@app.get("/api/clients")
async def api_clients() -> list[dict[str, Any]]:
    return await current_clients()


@app.delete("/api/clients/history")
async def api_purge_client_history() -> dict[str, Any]:
    """Purge all persisted client records (in-memory and on disk)."""
    async with state_lock:
        clients.clear()
    await asyncio.to_thread(_save_client_history)
    await broadcast({"type": "clients_purged"})
    logger.info("Client history purged by user request")
    return {"status": "ok", "message": "Client history cleared"}


@app.post("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control(hostname: str, overrides: dict[str, str]) -> dict[str, Any]:
    normalized = {key: str(value) for key, value in overrides.items()}
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname].setdefault("overrides", {}).update(normalized)
        payload = serialize_client(hostname, clients[hostname])

    await broadcast({"type": "overrides_update", "client": payload})
    return {"hostname": hostname, "overrides": payload["overrides"], "client": payload}


@app.delete("/api/clients/{hostname}/control", response_model=ClientControlResponse)
async def api_client_control_clear(hostname: str) -> dict[str, Any]:
    async with state_lock:
        if hostname not in clients:
            raise HTTPException(status_code=404, detail="Client not found")
        clients[hostname]["overrides"] = {}
        payload = serialize_client(hostname, clients[hostname])

    await broadcast({"type": "overrides_cleared", "client": payload})
    return {"hostname": hostname, "overrides": {}, "client": payload}


@app.post("/api/clients/all/control")
async def api_all_clients_control(overrides: dict[str, str]) -> dict[str, Any]:
    normalized = {key: str(value) for key, value in overrides.items()}
    async with state_lock:
        for client in clients.values():
            client.setdefault("overrides", {}).update(normalized)
        updated = len(clients)

    await broadcast_full_state()
    return {"status": "ok", "updated": updated, "overrides": normalized}


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


class DemoScenarioRequest(BaseModel):
    scenario: str  # e.g. "dns_fail", "dhcp_fail", "normal"


@app.post("/api/demo/client/{hostname}/scenario")
async def api_demo_set_scenario(
    hostname: str,
    body: DemoScenarioRequest,
    user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Trigger a named demo scenario on a client.

    Called via hub WebSocket relay or directly by an admin.
    The override is in-memory and expires after 120 minutes or on reboot.
    """
    payload = await _apply_demo_scenario(hostname, body.scenario, triggered_by=user.username)
    entry = _demo_active.get(hostname)
    return {
        "ok": True,
        "hostname": hostname,
        "scenario": body.scenario,
        "minutes_remaining": round(entry["minutes_remaining"] if entry and "minutes_remaining" in entry else 0),
        "client": payload,
    }


@app.delete("/api/demo/client/{hostname}/scenario")
async def api_demo_clear_scenario(
    hostname: str,
    _user: SpokeUser = Depends(require_auth),
) -> dict[str, Any]:
    """Clear the demo scenario override for a specific client."""
    payload = await _clear_demo_scenario(hostname)
    return {"ok": True, "hostname": hostname, "cleared": True, "client": payload}


@app.get("/api/demo/active")
async def api_demo_active(_user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Return all currently active demo scenario overrides."""
    return {"active": _demo_active_summary()}


@app.get("/api/demo/scenarios")
async def api_demo_scenarios(_user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Return the available scenario names and their flag definitions."""
    return {"scenarios": DEMO_SCENARIOS}


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


@app.get("/api/logs/history")
async def api_logs_history(
    lines: int = Query(default=300, ge=10, le=2000),
    source: str = Query(default="journal"),
):
    """Return the last N lines from the selected log source."""
    source = _normalize_log_source(source)
    try:
        if source == "agent":
            log_lines = proxmox_log_buffer[-lines:]
            if not log_lines:
                log_lines = [_log_source_hint("agent")]
            return PlainTextResponse("\n".join(log_lines))

        if source in {"install", "watchdog"}:
            log_path = INSTALL_LOG_PATH if source == "install" else WATCHDOG_LOG_PATH
            if not log_path.exists():
                return PlainTextResponse(_log_source_hint(source))
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(log_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", JOURNAL_UNIT, "--no-pager", "-n", str(lines),
                "--output=short-iso",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not text:
            detail = stderr.decode("utf-8", errors="replace").strip() or None
            return PlainTextResponse(_log_source_hint(source, detail))
        return PlainTextResponse(text or _log_source_hint(source))
    except HTTPException:
        raise
    except Exception as exc:
        return PlainTextResponse(_log_source_hint(source, str(exc)))


@app.get("/api/logs/stream")
async def api_logs_stream(source: str = Query(default="journal")):
    """Server-Sent Events stream of live log output."""
    source = _normalize_log_source(source)

    async def generate():
        yield "retry: 5000\n\n"

        if source == "agent":
            last_index = len(proxmox_log_buffer)
            hinted_empty = False
            while True:
                current_len = len(proxmox_log_buffer)
                if current_len < last_index:
                    last_index = 0
                if current_len > last_index:
                    for line in proxmox_log_buffer[last_index:current_len]:
                        yield _encode_sse_line(str(line))
                    last_index = current_len
                    hinted_empty = False
                    continue
                if current_len == 0 and not hinted_empty:
                    hinted_empty = True
                    yield _encode_sse_line(_log_source_hint("agent"))
                yield await _stream_keepalive()
                await asyncio.sleep(LOG_STREAM_POLL_SECS)

        if source == "install" and not INSTALL_LOG_PATH.exists():
            yield _encode_sse_line(_log_source_hint("install"))

        proc = None
        idle_deadline = time.monotonic() + 30
        try:
            if source == "install":
                proc = await asyncio.create_subprocess_exec(
                    "tail", "-n", "0", "-F", str(INSTALL_LOG_PATH),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "journalctl", "-u", JOURNAL_UNIT, "-f", "--no-pager", "-n", "0",
                    "--output=short-iso",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except Exception:
            yield "event: error\ndata: Log stream failed\n\n"
            return

        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=LOG_STREAM_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    if time.monotonic() >= idle_deadline:
                        yield "event: end\ndata: Log stream idle timeout\n\n"
                        return
                    yield ": keepalive\n\n"
                    continue
                if line:
                    idle_deadline = time.monotonic() + 30
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    if text:
                        yield _encode_sse_line(text)
                    continue
                detail = None
                if source == "journal" and proc.stderr is not None:
                    detail = (await proc.stderr.read()).decode("utf-8", errors="replace").strip() or None
                terminal = _log_source_hint(source, detail)
                yield f"event: end\ndata: {terminal}\n\n"
                return
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/init")
async def api_init() -> dict[str, Any]:
    """Single endpoint that returns all state needed for initial page render.
    Replaces 5+ separate REST calls made on page load."""
    cfg = dict(settings["central_config"])
    for secret_key in ("client_secret", "access_token", "refresh_token"):
        cfg.pop(secret_key, None)
    cfg["access_token_configured"] = bool(settings["central_config"].get("access_token") or central_token.get("access_token"))
    cfg["refresh_token_configured"] = bool(settings["central_config"].get("refresh_token") or central_token.get("refresh_token"))
    cfg["client_secret_configured"] = bool(settings["central_config"].get("client_secret"))
    return {
        "mode": "spoke",
        "proxmox": _proxmox_status_payload(),
        "settings": {
            "central_api": _public_central_api_settings(),
            "central_config": cfg,
            "relay_enabled": settings.get("relay_enabled", "off"),
            "relay_server_url": settings.get("relay_server_url", ""),
            "hub_tls_verify": settings.get("hub_tls_verify", "off"),
            "hub_managed": bool(settings.get("hub_managed", False)),
            "hub_isolation_timeout": int(settings.get("hub_isolation_timeout", 3600)),  # Include the timeout in init settings so the setup form has the correct safeguard value before a separate settings fetch finishes.
            "proxmox_config": copy.deepcopy(settings.get("proxmox_config") or {}),
        },
        "reclone": dict(reclone_state),
        "update_all": dict(update_all_state),
        "central": {
            "status": _central_status_payload(),
            "wireless_clients": dict(central_wireless_clients),
            "hardware_alerts": _hw_alerts_payload(),
            "client_count_status": _client_count_payload(),
            "token_valid": bool(central_token.get("access_token") and time.time() < central_token.get("expires_at", 0)),
            "token_state": _central_token_state(),
        },
        "relay": _relay_status_payload(),
        "installer_version": INSTALLER_VERSION,
        "app_version": APP_VERSION,
        "hostname": socket.gethostname(),
        "kill_switch": gkill_switch_state["value"],
        "local_kill_switch": _read_local_kill_switch(),
    }


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return await _api_health_payload()


@app.get("/api/debug")
async def api_debug() -> dict[str, Any]:
    """Server-side debug event log for diagnosing connectivity issues."""
    import datetime as _dt
    return {
        "server_start": _server_start_time,
        "server_uptime_s": round(time.time() - _server_start_time),
        "proxmox_connected": proxmox_state.get("connected"),
        "proxmox_last_seen": proxmox_state.get("last_seen"),
        "relay_connected": relay_state.get("connected"),
        "relay_last_sync": relay_state.get("last_sync"),
        "relay_error": relay_state.get("error"),
        "events": list(reversed(_debug_log)),
    }


@app.get("/api/debug/command-trace")
async def api_debug_command_trace() -> dict[str, Any]:
    """Returns the last 300 command relay events for diagnosing hub→spoke→agent pipeline issues."""
    async with state_lock:
        cmds_snapshot = list(_serialize_commands())
    agent_connected = proxmox_ws_connection is not None
    return {
        "agent_connected": agent_connected,
        "agent_hostname": proxmox_ws_hostname,
        "command_queue": cmds_snapshot,
        "trace": list(reversed(_command_trace)),
    }


@app.get("/api/services/status")
async def api_services_status() -> dict[str, Any]:
    return {
        "tasks": service_health,
        "task_names": list(background_tasks.keys()),
    }


# ── System health & service control ───────────────────────────────────────────

@app.get("/api/system/health")
async def api_system_health(request: Request) -> dict[str, Any]:
    """LXC host resource snapshot + service status + Proxmox install command."""
    import shutil as _shutil

    # Disk
    try:
        disk = _shutil.disk_usage(BASE_DIR)
        disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
    except Exception:
        disk_info = {"total": 0, "used": 0, "free": 0}

    # Memory via /proc/meminfo
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                try:
                    mem[k.strip()] = int(v.strip().split()[0])
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_info = {"total_kb": mem_total, "available_kb": mem_avail,
                "used_kb": mem_total - mem_avail}

    # Load average
    try:
        load_parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        load = load_parts[:3]
    except Exception:
        load = ["?", "?", "?"]

    # Uptime seconds
    try:
        uptime_secs = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        uptime_secs = 0.0

    # Service active state
    try:
        proc = await asyncio.create_subprocess_shell(
            "systemctl is-active client-sim-dashboard 2>/dev/null || echo inactive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        svc_status = stdout.decode().strip()
    except Exception:
        svc_status = "unknown"

    # Pre-built Proxmox agent install command
    base = str(request.base_url).rstrip("/")
    raw_base = REPO_URL.replace(".git", "").replace(
        "github.com", "raw.githubusercontent.com"
    )
    branch = os.environ.get("REPO_BRANCH", "main")
    install_cmd = (
        f"bash <(curl -sSL {raw_base}/{branch}/proxmox/install-proxmox-agent.sh)"
        f" --server {base}"
    )

    return {
        "disk": disk_info,
        "memory": mem_info,
        "load": load,
        "uptime_secs": uptime_secs,
        "service_status": svc_status,
        "proxmox_install_cmd": install_cmd,
    }


@app.get("/api/qa/summary")
async def api_qa_summary() -> dict[str, Any]:
    """Spoke-level QA summary: dongles, VMs, reporting clients, and pass/fail.

    Cross-references USB dongle count against provisioned VMs and actively
    reporting clients so Copilot (or any automated check) can assert the full
    auto-provisioning pipeline is healthy on this spoke.
    """
    async with state_lock:
        proxmox_connected = bool(proxmox_state.get("connected", False))
        present_usb: list[Any] = list(proxmox_state.get("present_usb") or [])
        usb_state: list[Any] = list(proxmox_state.get("usb_state") or [])
        vms: list[Any] = list(proxmox_state.get("vms") or [])
        reporting_clients = len(clients)

    dongle_count = len(present_usb) if present_usb else len(usb_state)
    # Only count sim-client VMs — those with a USB dongle assigned (in usb_state).
    # Templates, IoT VMs, and other non-sim VMs must not be included in this total.
    usb_vmids = {str(e.get("vmid")) for e in usb_state if e.get("vmid") is not None}
    sim_vm_count = sum(1 for vm in vms if str(vm.get("vmid", "")) in usb_vmids)
    auto_provision = _normalize_toggle(settings.get("usb_auto_provision", "off")) == "on"

    issues: list[str] = []
    if not proxmox_connected:
        issues.append("Proxmox agent is not connected")
    if auto_provision and dongle_count > 0 and sim_vm_count != dongle_count:
        issues.append(
            f"VM count ({sim_vm_count}) does not match dongle count ({dongle_count})"
        )
    if dongle_count > 0 and reporting_clients != dongle_count:
        issues.append(
            f"reporting clients ({reporting_clients}) does not match dongle count ({dongle_count})"
        )

    return {
        "proxmox_agent_connected": proxmox_connected,
        "dongle_count": dongle_count,
        "vm_count": sim_vm_count,
        "total_vm_count": len(vms),
        "reporting_clients": reporting_clients,
        "auto_provision": auto_provision,
        "pass": len(issues) == 0,
        "issues": issues,
    }


@app.post("/api/service/{action}")
async def api_service_control(action: str) -> dict[str, Any]:
    """Start, stop, or restart the client-sim-dashboard service."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action must be start, stop, or restart")
    try:
        proc = await asyncio.create_subprocess_shell(
            f"sudo -n systemctl {action} client-sim-dashboard",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        return {"status": "timeout",
                "message": f"systemctl {action} timed out — service may be restarting"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    if rc != 0:
        return {"status": "error",
                "message": stderr.decode().strip() or f"exit code {rc}"}
    return {"status": "ok", "message": f"Service {action} sent"}


# ── Cache-clear endpoints ──────────────────────────────────────────────────────

@app.post("/api/server/clear-cache")
async def api_server_clear_cache() -> dict[str, Any]:
    """Reset all server-side in-memory state (Proxmox, reclone, commands, update-all).
    Does not restart the service — the UI will receive fresh empty state via WS broadcast."""
    global _prev_usb_by_vmid
    async with state_lock:
        proxmox_state.update({
            "connected": False, "last_seen": None, "node": {}, "vms": [],
            "unknown_usb": [], "usb_state": [], "present_usb": [],
            "agent_version": None, "pve_version": None, "template_lock": "",
            "prov_summary": None, "prov_run": _default_provision_run_state(),
        })
        _prev_usb_by_vmid = {}  # clear transition-detection snapshot so no phantom "failed" on next telemetry
        proxmox_log_buffer.clear()
        pending_proxmox_agents.clear()
        commands.clear()
        await _async_save_commands()
        reclone_state.update({
            "status": "idle", "type": None, "total": 0, "completed": 0,
            "failed": 0, "current_vm": None, "log": [], "auto_recovery_log": [],
            "last_run": None, "started_at": None,
        })
        update_all_state.update({
            "running": False, "phase": "idle", "total_agents": 0,
            "completed_agents": 0, "failed_agents": 0, "agent_cmds": [],
            "started_at": None, "error": None,
        })

    await broadcast({"type": "proxmox_update", **_proxmox_status_payload()})
    await _broadcast_reclone_state()
    await broadcast({"type": "update_all_progress", **update_all_state})
    await broadcast({"type": "commands_update", "commands": []})
    logger.info("Server cache cleared by user request")
    return {"status": "ok", "message": "Server cache cleared"}


@app.post("/api/proxmox/autoprov/reset")
async def api_autoprov_reset() -> dict[str, Any]:
    """Reset auto-provisioning run state and summary without clearing all server state.
    Use when the provisioning panel is stuck showing in-progress after completion."""
    async with state_lock:
        proxmox_state["prov_run"] = _default_provision_run_state()
        proxmox_state["prov_summary"] = None
    logger.info("Auto-provisioning status manually reset via API")
    return {"ok": True}


@app.post("/api/setup/clear-cache")
async def api_setup_clear_cache() -> dict[str, Any]:
    """Wipe all cached files, re-clone the repo, clear in-memory client/central state,
    then restart the WebUI service so it starts completely fresh."""
    import shutil

    # 1. Delete cached data files
    for path in [
        CLIENT_HISTORY_FILE,
        STATE_CACHE_FILE,
        COMMAND_QUEUE_FILE,
        RECLONE_STATE_FILE,
        RELAY_STATE_FILE,
        UPDATE_STATE_FILE,
        HISTORY_FILE,
        CLIENT_COUNT_BASELINE_FILE,
    ]:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # 2. Clear in-memory state
    async with state_lock:
        clients.clear()
    async with history_lock:
        central_history.clear()
    central_wireless_clients.clear()

    # 3. Remove any stale git lock and wipe + re-clone the repo
    async with _git_lock:
        lock_file = REPO_DIR / ".git" / "index.lock"
        lock_file.unlink(missing_ok=True)
        try:
            shutil.rmtree(REPO_DIR, ignore_errors=True)
        except Exception as exc:
            logger.warning("clear-cache: could not remove REPO_DIR: %s", exc)
        try:
            await asyncio.to_thread(sync_repo_once)
            repo_state["synced"] = True
            repo_state["error"] = None
            repo_state["last_sync"] = time.time()
        except Exception as exc:
            logger.warning("clear-cache: re-clone failed: %s", exc)
            repo_state["error"] = str(exc)

    logger.info("Setup cache cleared by user request — restarting service")
    await broadcast({"type": "notification", "level": "info",
                     "message": "Cache cleared — service restarting in 2 seconds…"})

    # 4. Restart the service after a short delay so the response can be sent
    async def _delayed_restart() -> None:
        await asyncio.sleep(2)
        try:
            await asyncio.create_subprocess_shell(
                "sudo -n systemctl restart client-sim-dashboard",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.error("clear-cache: restart failed: %s", exc)

    asyncio.create_task(_delayed_restart())
    return {"status": "ok", "message": "Cache cleared — service restarting"}


@app.get("/api/kill-switch", response_class=PlainTextResponse)
async def api_kill_switch() -> str:
    """Return the current global kill switch value ('on' or 'off').
    Clients should poll this as their primary source — always fetched from
    solutions-hpe/main so no fork can override it."""
    return gkill_switch_state["value"]


@app.get("/api/kill-switch/status")
async def api_kill_switch_status() -> dict[str, Any]:
    """Return full gkill_switch state for the WebUI dashboard."""
    return {
        "value": gkill_switch_state["value"],
        "last_fetched": gkill_switch_state["last_fetched"],
        "error": gkill_switch_state["error"],
    }


@app.post("/api/commands")
async def create_command(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Queue a command for one device, all clients, or the proxmox agent."""
    target = str(body.get("target", "")).strip()
    action = _normalize_command_action(body.get("action", ""))
    args = body.get("args", {})
    command_type = _normalize_command_type(body.get("type"))

    if not target or not action:
        raise HTTPException(status_code=422, detail="target and action are required")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")

    new_cmds: list[dict[str, Any]] = []
    deduped = 0

    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        if target == "all":
            known = list(clients.keys())
            if not known:
                raise HTTPException(status_code=400, detail="No clients registered yet")
            for hostname in known:
                cmd, created, _expired, _purged = _enqueue_command_locked(hostname, action, args, command_type=command_type)
                if created:
                    new_cmds.append(cmd)
                else:
                    deduped += 1
        elif target == "proxmox":
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        else:
            if target not in clients:
                raise HTTPException(status_code=404, detail="Client not found")
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        serialized = _serialize_commands()

    if new_cmds or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    if new_cmds:
        await _push_pending_commands_for_targets([cmd["target"] for cmd in new_cmds])
    return {"queued": len(new_cmds), "deduped": deduped, "ids": [c["id"] for c in new_cmds]}


@app.get("/api/commands")
async def list_commands() -> list[dict[str, Any]]:
    """Return the current in-memory command queue plus short-lived terminal results."""
    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        serialized = _serialize_commands()
    if expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return serialized


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


@app.get("/api/inbox")
async def poll_inbox(request: Request, hostname: str) -> list[dict[str, Any]]:
    """Device polls for pending commands addressed to it. Marks them delivered."""
    if not hostname:
        raise HTTPException(status_code=422, detail="hostname is required")
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    # Proxmox agents send X-API-Key; simulation clients send X-Client-Key.
    api_key = request.headers.get("X-API-Key", "")
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    is_approved_proxmox = (
        approved_hostname is not None
        and api_key == approved_proxmox_agents[approved_hostname]
    )
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox")
    elif api_key != approved_proxmox_agents[approved_hostname]:
        raise HTTPException(status_code=401, detail="invalid key")
    return await _poll_agent_inbox(hostname, approved_hostname)


async def _ack_command_internal(body: dict[str, Any]) -> dict[str, bool]:
    cmd_id = str(body.get("id", "")).strip()
    status = str(body.get("status", "completed")).strip().lower()
    message = body.get("message", "")

    if status not in ("completed", "failed"):
        raise HTTPException(status_code=422, detail="status must be 'completed' or 'failed'")

    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        cmd = next((c for c in commands if c["id"] == cmd_id), None)
        if not cmd:
            raise HTTPException(status_code=404, detail="Command not found")

        cmd["status"] = status
        cmd["message"] = str(message) if message is not None else ""
        cmd["updated_at"] = time.time()
        cmd["purge_after"] = cmd["updated_at"] + COMMAND_RESULT_RETENTION_SECS
        _trace("agent_ack", cmd_id=cmd_id, action=cmd.get("action"), target=cmd.get("target"),
               args={k: v for k, v in (cmd.get("args") or {}).items() if k in {"vmid", "vm_type"}},
               status=status, message=str(message)[:200] if message else "")
        await _async_save_commands()
        serialized = _serialize_commands()

    await broadcast({"type": "commands_update", "commands": serialized})
    return {"ok": True}


@app.post("/api/inbox/ack")
async def ack_command(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    """Device reports command result."""
    # Accept either a valid simulation client key OR a valid proxmox agent key.
    api_key = request.headers.get("X-API-Key", "")
    ack_hostname = request.headers.get("X-Hostname", "") or body.get("hostname", "")
    is_approved_proxmox = any(
        api_key == v for v in approved_proxmox_agents.values()
    ) if api_key else False
    if not is_approved_proxmox:
        _require_shared_client_key(request.headers.get("X-Client-Key", ""), "/api/inbox/ack")
        ack_hostname = ack_hostname or "(sim-client)"
    result = await _ack_command_internal(body)
    _trace("inbox_ack_received", hostname=ack_hostname or "(unknown)",
           cmd_id=str(body.get("id", "")), status=body.get("status", ""),
           message=str(body.get("message", ""))[:200])
    return result


@app.delete("/api/commands/pending")
async def expire_pending_for_target(target: str = Query(...)) -> dict[str, int]:
    """Expire active commands for a given target hostname before replacing a VM."""
    count = 0
    now = time.time()
    async with state_lock:
        expired, purged = _cleanup_commands_locked(now)
        for cmd in commands:
            if cmd["target"] == target and cmd["status"] in {"pending", "delivered"}:
                cmd["status"] = "expired"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                count += 1
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    if count:
        logger.info("Expired %d active command(s) for target %s before VM destroy", count, target)
        await broadcast({"type": "commands_update", "commands": serialized})
    elif expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"expired": count}


@app.post("/api/commands/cancel-all")
async def cancel_all_queued_commands() -> dict[str, int]:
    """Cancel all pending/delivered commands (troubleshooting — stops queued work without deleting history)."""
    now = time.time()
    count = 0
    async with state_lock:
        for cmd in commands:
            if cmd.get("status") in {"pending", "delivered"}:
                cmd["status"] = "cancelled"
                cmd["updated_at"] = now
                cmd["error"] = "Manually cancelled via Cancel All"
                count += 1
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    if count:
        logger.info("Cancel-all: %d queued command(s) cancelled by user", count)
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"cancelled": count}


@app.delete("/api/commands/{cmd_id}")
async def delete_command(cmd_id: str) -> dict[str, bool]:
    """Remove a command from history."""
    async with state_lock:
        before = len(commands)
        commands[:] = [c for c in commands if c["id"] != cmd_id]
        if len(commands) == before:
            raise HTTPException(status_code=404, detail="Command not found")
        await _async_save_commands()
        serialized = _serialize_commands()
    await broadcast({"type": "commands_update", "commands": serialized})
    return {"ok": True}


@app.post("/api/notifications/test")
async def api_notifications_test(body: dict[str, Any]) -> dict[str, Any]:
    """Send a test notification via email or Teams."""
    channel = body.get("channel", "")  # "email" | "teams"
    notif = dict(settings.get("notifications", {}))
    # Allow overriding with posted values (for unsaved fields)
    notif.update({k: v for k, v in body.items() if k != "channel"})

    test_transition = [{
        "check_type": "sim",
        "check_id": "test",
        "check_name": "Test Notification",
        "site": "test-site",
        "old": "ok",
        "new": "error",
        "ts": time.time(),
    }]

    try:
        if channel == "email":
            if not notif.get("smtp_host") or not notif.get("smtp_to"):
                raise HTTPException(status_code=422, detail="smtp_host and smtp_to are required")
            await asyncio.to_thread(_send_email_notifications, notif, test_transition)
        elif channel == "teams":
            url = notif.get("teams_webhook_url", "")
            if not url:
                raise HTTPException(status_code=422, detail="teams_webhook_url is required")
            await _send_teams_notifications(url, test_transition)
        else:
            raise HTTPException(status_code=422, detail="channel must be 'email' or 'teams'")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "ok", "channel": channel}


@app.get("/.well-known/acme-challenge/{token}", include_in_schema=False)
async def acme_challenge(token: str):
    key_authorization = _acme_challenges.get(token)
    if not key_authorization:
        raise HTTPException(404)
    return PlainTextResponse(key_authorization)


async def _proxmox_disconnect_grace(expected_hostname: str | None) -> None:
    await asyncio.sleep(PROXMOX_WS_GRACE_SECS)
    if proxmox_ws_connection is not None:
        return
    if expected_hostname and proxmox_ws_hostname and _proxmox_hostnames_match(expected_hostname, proxmox_ws_hostname):
        return
    if proxmox_state.get("last_seen") and (time.time() - float(proxmox_state.get("last_seen") or 0)) <= PROXMOX_WS_GRACE_SECS:
        return
    proxmox_state["connected"] = False
    proxmox_state["vms"] = []
    await _broadcast_proxmox_state()


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
    global proxmox_ws_connection, proxmox_ws_hostname, proxmox_ws_disconnect_task
    if not hostname:
        await websocket.close(code=4400, reason="hostname is required")
        return
    approved_hostname, response = await _authorize_proxmox_agent(hostname, api_key, websocket.client.host if websocket.client else "unknown", time.time())
    if response is not None or approved_hostname is None:
        detail = response.body.decode("utf-8", errors="ignore") if isinstance(response, JSONResponse) else "agent not approved"
        await websocket.close(code=4401, reason=detail[:120])
        return
    await websocket.accept()
    proxmox_ws_connection = websocket
    proxmox_ws_hostname = approved_hostname
    if proxmox_ws_disconnect_task is not None:
        proxmox_ws_disconnect_task.cancel()
        proxmox_ws_disconnect_task = None
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
        if proxmox_ws_connection is websocket:
            proxmox_ws_connection = None
            proxmox_ws_hostname = approved_hostname
            _trace("agent_ws_disconnect", hostname=approved_hostname)
            proxmox_ws_disconnect_task = asyncio.create_task(_proxmox_disconnect_grace(approved_hostname))


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
        global _repo_ver
        _repo_ver = await asyncio.to_thread(_get_repo_version)
        await websocket.send_text(json.dumps({"type": "repo_status", "synced": repo_state["synced"], "error": repo_state["error"], "last_sync": repo_state["last_sync"], "repo_version": _repo_ver}, default=str))
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
        await websocket.send_text(json.dumps({"type": "update_all_progress", **dict(update_all_state)}, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.error("WS on-connect update_all_progress error: %s", exc)
    try:
        await websocket.send_text(json.dumps({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()}, default=str))
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
