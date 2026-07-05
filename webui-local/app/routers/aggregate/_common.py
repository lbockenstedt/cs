"""Shared helpers, models, and module state for the aggregate router package."""
from __future__ import annotations


import json
import base64
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ... import auth, store
from ...aruba import (
    ArubaClient,
    DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS,
    DEFAULT_NEW_CENTRAL_MONITORED_CHECKS,
    validate_cluster_url,
)
from ...crypto import decrypt_dict, encrypt_dict
from ...data_models import AuditEntry, Command, Spoke, Tenant, User
logger = logging.getLogger(__name__)

FAIL_STATUSES = {"error", "fail", "failed", "degraded", "critical"}
PASS_STATUSES = {"ok", "pass", "passed", "healthy", "connected"}
WARNING_STATUSES = {"warn", "warning", "unknown", "no_data", "stale"}
MODE_VALUES = {"centralized", "distributed"}
CENTRAL_WEBHOOK_HOST = "cs-hub.westus3.azurecontainer.io:8443"
_central_browse_cache: dict[str, dict[str, Any]] = {}
_central_browse_cache_ts: dict[str, float] = {}
_CENTRAL_BROWSE_TTL = 300


def _browse_cache_path(tenant_id: str) -> Path:
    return store._data_dir() / tenant_id / "central_browse_cache.json"


def _load_browse_disk_cache(tenant_id: str) -> dict[str, Any] | None:
    try:
        p = _browse_cache_path(tenant_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_browse_disk_cache(tenant_id: str, data: dict[str, Any]) -> None:
    try:
        p = _browse_cache_path(tenant_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.warning("central_browse: could not write disk cache: %s", exc)


def _is_individual_browse_client(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return any(key in item for key in ("mac", "hostname", "ip", "ap", "ssid", "status", "os", "vlan"))


def _has_legacy_client_summary_rows(data: dict[str, Any] | None) -> bool:
    clients = data.get("clients") if isinstance(data, dict) else None
    if not isinstance(clients, list) or not clients:
        return False
    has_individual = any(_is_individual_browse_client(item) for item in clients)
    has_summary = any(
        isinstance(item, dict) and any(key in item for key in ("total", "wired", "wireless"))
        for item in clients
    )
    return has_summary and not has_individual


def _normalize_browse_cache(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    normalized = dict(data)
    clients = normalized.get("clients")
    if not _has_legacy_client_summary_rows(normalized):
        if not isinstance(clients, list):
            normalized["clients"] = []
        return normalized

    clients_by_site = normalized.get("clients_by_site")
    if not isinstance(clients_by_site, dict):
        derived: dict[str, dict[str, Any]] = {}
        for item in clients:
            if not isinstance(item, dict):
                continue
            site_name = str(item.get("site") or "—").strip() or "—"
            derived[site_name] = {
                "total": int(item.get("total") or 0),
                "wired": int(item.get("wired") or 0),
                "wireless": int(item.get("wireless") or 0),
            }
        normalized["clients_by_site"] = derived

    normalized["clients"] = []
    return normalized


def _central_webhook_endpoint_url(tenant_id: str) -> str:
    return f"https://{CENTRAL_WEBHOOK_HOST}/api/{tenant_id}/webhook/central"


def _load_aruba_config(tenant: Tenant) -> dict[str, Any]:
    if not tenant.aruba_config_enc:
        raise HTTPException(status_code=400, detail="Aruba Central credentials are not configured for this tenant.")
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt Aruba Central credentials: {exc}") from exc
    cfg["cluster_url"] = _validated_cluster_url_or_400(cfg.get("cluster_url", ""))
    return cfg


def _persist_aruba_config(tenant: Tenant, cfg: dict[str, Any]) -> None:
    tenant.aruba_cid = cfg.get("customer_id") or tenant.aruba_cid
    tenant.aruba_config_enc = (
        encrypt_dict(cfg)
        if any(str(value).strip() for key, value in cfg.items() if key != "api_version") or cfg.get("client_secret")
        else None
    )
    store.save_tenant(tenant)


class ConfigPushRequest(BaseModel):
    tenant_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class CentralConfigPayload(BaseModel):
    api_version: str = "classic"
    cluster_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    customer_id: str = ""
    workspace_id: str = ""


class CentralUpdateRequest(BaseModel):
    tenant_id: str = ""
    mode: str = "distributed"
    hub_central_config: CentralConfigPayload = Field(default_factory=CentralConfigPayload)
    central_browse_interval_minutes: int = 5


class CentralSitesConfigPayload(BaseModel):
    site_mappings: dict[str, str] = Field(default_factory=dict)
    monitored_checks: list[dict[str, Any]] = Field(default_factory=list)
    hardware_checks: list[dict[str, Any]] = Field(default_factory=list)
    excluded_sites: list[str] = Field(default_factory=list)


class SimulationConfUpdateRequest(BaseModel):
    content: str = ""


def _resolve_tenant_id(tenant_id: Optional[str], current_user: User) -> str:
    if tenant_id:
        auth.require_tenant_access(tenant_id, current_user)
        return tenant_id

    tenant_ids = [tenant.id for tenant in store.list_tenants()] if current_user.is_superadmin else current_user.tenant_ids()
    if not tenant_ids:
        raise HTTPException(status_code=404, detail="No tenant available")
    if len(tenant_ids) == 1:
        return tenant_ids[0]
    raise HTTPException(status_code=400, detail="tenant_id query parameter is required")



def _require_tenant_admin(tenant_id: str, current_user: User) -> str:
    auth.require_tenant_access(tenant_id, current_user)
    role = current_user.get_role(tenant_id)
    if current_user.is_superadmin or role == "admin":
        return tenant_id
    raise HTTPException(status_code=403, detail="Admin role required")


def _require_tenant_access(tenant_id: str, current_user: User) -> str:
    """Allow any authenticated user with tenant access (viewer or admin)."""
    auth.require_tenant_access(tenant_id, current_user)
    return tenant_id


def _require_tenant_demo_or_above(tenant_id: str, current_user: User) -> str:
    """Allow demo, viewer, and admin roles (any authenticated tenant member)."""
    auth.require_tenant_access(tenant_id, current_user)
    return tenant_id


def _approved_spokes(tenant_id: str) -> list[Spoke]:
    return [spoke for spoke in store.list_spokes(tenant_id) if spoke.status == "approved"]



def _get_tenant(tenant_id: str) -> Tenant:
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant



def _is_online(spoke: Spoke) -> bool:
    if not spoke.last_seen:
        return False
    last_seen = spoke.last_seen
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    # Use 300s to match the frontend isOnline() threshold — eliminates the
    # red/green flicker caused by the old 600s backend vs 300s frontend mismatch.
    return (datetime.now(timezone.utc) - last_seen).total_seconds() < 300



def _telemetry_clients(spoke: Spoke) -> list[dict[str, Any]]:
    clients = (spoke.telemetry or {}).get("clients")
    return clients if isinstance(clients, list) else []



def _telemetry_dict(spoke: Spoke, key: str) -> dict[str, Any]:
    value = (spoke.telemetry or {}).get(key)
    return value if isinstance(value, dict) else {}



def _telemetry_list(spoke: Spoke, *keys: str) -> list[dict[str, Any]]:
    telemetry = spoke.telemetry or {}
    for key in keys:
        value = telemetry.get(key)
        if isinstance(value, list):
            return value
    return []



def _coerce_int(value: Any, default: int = 0, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (AttributeError, TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number



def _setting_toggle(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}



def _spoke_usb_capacity(spoke: Spoke) -> tuple[int, int, int, bool]:
    """Return (used_slots, total_slots, dongle_count, auto_provision).

    used_slots   — provisioned VMs (active entries in usb_state)
    total_slots  — effective capacity = min(dongle_count, usb_max_slots)
    dongle_count — raw USB dongles physically present
    auto_provision — whether auto-provision is enabled on this spoke
    """
    proxmox = _telemetry_dict(spoke, "proxmox")
    api_server = _telemetry_dict(spoke, "api_server")
    usb_devices = _telemetry_list(spoke, "usb_devices") or (proxmox.get("usb_state") if isinstance(proxmox.get("usb_state"), list) else [])
    # Slots in use = provisioned VMs (active entries in usb_state), not raw USB dongle count
    usb_state = proxmox.get("usb_state") if isinstance(proxmox.get("usb_state"), list) else usb_devices
    used_slots = sum(
        1 for entry in usb_state
        if isinstance(entry, dict) and entry.get("prov_status") in ("active", "provisioning", "tearing_down", "missing")
    ) if usb_state else 0
    # Dongle count = physically present USB devices reported by the agent
    present_usb = proxmox.get("present_usb")
    if isinstance(present_usb, list):
        dongle_count = len(present_usb)
    else:
        dongle_count = _coerce_int(proxmox.get("usb_count") or 0, 0, minimum=0)
    spoke_config = spoke.config or {}
    usb_max_slots = _coerce_int(
        spoke_config.get("usb_max_slots")
        or api_server.get("usb_max_slots")
        or proxmox.get("usb_max_slots")
        or 0,
        0,
        minimum=0,
    )
    # Effective capacity: if max_slots is configured, cap at min(dongles, max_slots);
    # otherwise fall back to raw dongle count.
    if usb_max_slots > 0:
        total_slots = min(dongle_count, usb_max_slots)
    else:
        total_slots = dongle_count
    # Use explicit priority: spoke.config > api_server telemetry > proxmox telemetry.
    # Cannot use `or` because False (disabled) is falsy and would be skipped.
    _ap_sources = [
        spoke_config.get("usb_auto_provision"),
        api_server.get("usb_auto_provision"),
        proxmox.get("usb_auto_provision"),
    ]
    _ap_val = next((v for v in _ap_sources if v is not None), None)
    auto_provision = _setting_toggle(_ap_val)
    return used_slots, total_slots, dongle_count, auto_provision



def _central_telemetry(spoke: Spoke) -> dict[str, Any]:
    telemetry = spoke.telemetry or {}
    central = telemetry.get("central")
    return central if isinstance(central, dict) else telemetry



def _hardware_type(client: dict[str, Any]) -> str:
    return str(client.get("hw_type") or client.get("platform") or "Unknown").strip() or "Unknown"



def _normalize_lookup_value(value: Any) -> str:
    return str(value or "").strip().lower()



def _spoke_usb_lookup(spoke: Spoke) -> tuple[set[str], set[str], dict[str, str]]:
    proxmox = _telemetry_dict(spoke, "proxmox")
    usb_devices = _telemetry_list(spoke, "usb_devices")
    if not usb_devices and isinstance(proxmox.get("usb_state"), list):
        usb_devices = proxmox.get("usb_state")
    proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
    if not proxmox_vms and isinstance(proxmox.get("vms"), list):
        proxmox_vms = proxmox.get("vms")

    usb_vmids = {
        str(device.get("vmid")).strip()
        for device in usb_devices
        if isinstance(device, dict) and device.get("vmid") is not None and str(device.get("vmid")).strip()
    }
    usb_hostnames = {
        _normalize_lookup_value(device.get("hostname") or device.get("vm_name"))
        for device in usb_devices
        if isinstance(device, dict)
    }
    usb_hostnames.discard("")

    vmids_by_hostname: dict[str, str] = {}
    for vm in proxmox_vms:
        if not isinstance(vm, dict):
            continue
        hostname = _normalize_lookup_value(vm.get("name") or vm.get("hostname"))
        vmid = str(vm.get("vmid")).strip() if vm.get("vmid") is not None else ""
        if hostname and vmid:
            vmids_by_hostname[hostname] = vmid
        # VMs with has_usb_config or reclone_bus_path have USB passthrough in Proxmox config
        if vmid and (vm.get("has_usb_config") or vm.get("reclone_bus_path")):
            usb_vmids.add(vmid)

    return usb_vmids, usb_hostnames, vmids_by_hostname



def _spoke_t3_lookup(spoke: Spoke) -> set[str]:
    proxmox = _telemetry_dict(spoke, "proxmox")
    proxmox_vms = _telemetry_list(spoke, "proxmox_vms")
    if not proxmox_vms and isinstance(proxmox.get("vms"), list):
        proxmox_vms = proxmox.get("vms")
    t3_pci_devices = proxmox.get("t3_pci_devices") if isinstance(proxmox.get("t3_pci_devices"), list) else []
    t3_addrs = {
        _normalize_lookup_value(device.get("id"))
        for device in t3_pci_devices
        if isinstance(device, dict)
    }
    t3_addrs.discard("")
    if not t3_addrs:
        return set()

    t3_vmids: set[str] = set()
    for vm in proxmox_vms:
        if not isinstance(vm, dict):
            continue
        vmid = str(vm.get("vmid")).strip() if vm.get("vmid") is not None else ""
        pci_passthrough_addrs = vm.get("pci_passthrough_addrs") if isinstance(vm.get("pci_passthrough_addrs"), list) else []
        if vmid and any(_normalize_lookup_value(addr) in t3_addrs for addr in pci_passthrough_addrs):
            t3_vmids.add(vmid)
    return t3_vmids



def _client_has_usb(client: dict[str, Any], usb_vmids: set[str], usb_hostnames: set[str], vmids_by_hostname: dict[str, str]) -> bool:
    vmid = str(client.get("vmid") or client.get("proxmox_vmid") or "").strip()
    hostname = _normalize_lookup_value(client.get("hostname"))
    if not vmid and hostname:
        vmid = vmids_by_hostname.get(hostname, "")
    if vmid and vmid in usb_vmids:
        return True
    return bool(hostname and hostname in usb_hostnames)



def _client_has_t3_pci(client: dict[str, Any], t3_vmids: set[str], vmids_by_hostname: dict[str, str]) -> bool:
    vmid = str(client.get("vmid") or client.get("proxmox_vmid") or "").strip()
    hostname = _normalize_lookup_value(client.get("hostname"))
    if not vmid and hostname:
        vmid = vmids_by_hostname.get(hostname, "")
    return bool(vmid and vmid in t3_vmids)



def _record_check(summary: dict[str, int], raw_status: Any) -> None:
    status = str(raw_status or "unknown").strip().lower()
    if status in PASS_STATUSES:
        summary["pass"] += 1
    elif status in FAIL_STATUSES:
        summary["fail"] += 1
    else:
        summary["warning"] += 1



def _build_checks_summary(spokes: list[Spoke]) -> dict[str, int]:
    summary = {"pass": 0, "fail": 0, "warning": 0}
    for spoke in spokes:
        central = _central_telemetry(spoke)
        status_map = central.get("status") or {}
        if isinstance(status_map, dict):
            for checks in status_map.values():
                if not isinstance(checks, dict):
                    continue
                for info in checks.values():
                    if isinstance(info, dict):
                        _record_check(summary, info.get("status"))

        hw_alerts = central.get("hardware_alerts") or []
        if isinstance(hw_alerts, list):
            for alert in hw_alerts:
                if not isinstance(alert, dict):
                    continue
                total = alert.get("total")
                try:
                    affected = int(total)
                except (TypeError, ValueError):
                    affected = 0
                summary["fail" if affected > 0 else "pass"] += 1

        client_count_status = central.get("client_count_status") or {}
        if isinstance(client_count_status, dict):
            for info in client_count_status.values():
                if isinstance(info, dict):
                    _record_check(summary, info.get("status"))
    return summary



def _serialize_hub_central_config(tenant: Tenant) -> dict[str, Any]:
    if not tenant.aruba_config_enc:
        return {"configured": False, "api_version": "classic", "central_browse_interval_minutes": tenant.central_browse_interval_minutes}
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception:
        return {"configured": True, "error": "unreadable", "api_version": "classic", "central_browse_interval_minutes": tenant.central_browse_interval_minutes}
    return {
        "configured": True,
        "cluster_url": cfg.get("cluster_url", ""),
        "client_id": cfg.get("client_id", ""),
        "customer_id": cfg.get("customer_id", ""),
        "workspace_id": cfg.get("workspace_id", ""),
        "api_version": cfg.get("api_version", "classic"),
        "client_secret_configured": bool(cfg.get("client_secret")),
        "access_token_configured": bool(cfg.get("access_token")),
        "refresh_token_configured": bool(cfg.get("refresh_token")),
        "webhook_registered": bool(cfg.get("webhook_id")),
        "central_browse_interval_minutes": tenant.central_browse_interval_minutes,
    }



def _github_repo_settings(tenant: Tenant) -> dict[str, str]:
    if not tenant.github_config_enc:
        return {"github_token": "", "sim_repo_url": "", "sim_repo_branch": "main"}
    try:
        cfg = decrypt_dict(tenant.github_config_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="GitHub settings could not be read")
    return {
        "github_token": str(cfg.get("github_token") or "").strip(),
        "sim_repo_url": str(cfg.get("sim_repo_url") or "").strip(),
        "sim_repo_branch": str(cfg.get("sim_repo_branch") or "main").strip() or "main",
    }



def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    normalized = str(repo_url or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Simulation repo URL is not configured")
    parsed = urlparse(normalized)
    path = parsed.path if parsed.scheme else normalized
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="Simulation repo URL must be a GitHub owner/repo URL")
    owner = parts[0]
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="Simulation repo URL must include owner and repo")
    return owner, repo



def _github_api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }



def _require_sim_repo_config(tenant: Tenant) -> tuple[str, str, str, str]:
    cfg = _github_repo_settings(tenant)
    github_token = cfg.get("github_token", "")
    repo_url = cfg.get("sim_repo_url", "")
    branch = cfg.get("sim_repo_branch", "main")
    if not github_token:
        raise HTTPException(status_code=400, detail="GitHub token is not configured for this tenant. Open Setup to add it.")
    if not repo_url:
        raise HTTPException(status_code=400, detail="Simulation repo URL is not configured. Open Setup to add it.")
    owner, repo = _parse_github_repo(repo_url)
    return github_token, owner, repo, branch



def _github_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception as exc:
        logger.warning("Failed to parse GitHub error payload (%s): %s", response.status_code, exc)
        payload = {}
    return payload.get("message") or response.text or f"GitHub API error ({response.status_code})"



async def _fetch_simulation_conf_from_github(tenant: Tenant) -> tuple[str, str, str]:
    github_token, owner, repo, branch = _require_sim_repo_config(tenant)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/configs/simulation.conf"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=_github_api_headers(github_token), params={"ref": branch})
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=f"configs/simulation.conf was not found in {owner}/{repo} on branch {branch}.")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_github_error_detail(response))
    payload = response.json()
    encoded = str(payload.get("content") or "").replace("\n", "")
    try:
        content = base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub returned unreadable simulation.conf content: {exc}") from exc
    return content, str(payload.get("sha") or ""), branch


async def _fetch_user_overrides_conf_from_github(tenant: Tenant) -> tuple[str, str, str]:
    github_token, owner, repo, branch = _require_sim_repo_config(tenant)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/configs/user-overrides.conf"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=_github_api_headers(github_token), params={"ref": branch})
    if response.status_code == 404:
        # File missing is non-fatal — return empty content
        return "", "", branch
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_github_error_detail(response))
    payload = response.json()
    encoded = str(payload.get("content") or "").replace("\n", "")
    try:
        content = base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub returned unreadable user-overrides.conf content: {exc}") from exc
    return content, str(payload.get("sha") or ""), branch



def _validated_cluster_url_or_400(cluster_url: str) -> str:
    try:
        return validate_cluster_url(cluster_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cluster_url: {exc}") from exc


def _queue_repo_sync_for_all_spokes(tenant_id: str, current_user: User) -> int:
    queued = 0
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    for spoke in _approved_spokes(tenant_id):
        store.enqueue_command(
            Command(
                spoke_id=spoke.id,
                tenant_id=tenant_id,
                type="repo_sync",
                payload={},
                expires_at=expires_at,
            )
        )
        store.append_audit(
            AuditEntry(
                spoke_id=spoke.id,
                tenant_id=tenant_id,
                task_type="repo_sync",
                execution_mode=spoke.processing_mode.resolve("repo_sync"),
                status="pending",
                detail="Queued repo sync after simulation.conf update",
                initiated_by=current_user.username,
                result={"target": "spoke"},
            )
        )
        queued += 1
    return queued



def _central_mode(tenant: Tenant) -> str:
    mode = tenant.default_processing_mode.resolve("aruba_polling")
    return mode if mode in MODE_VALUES else "distributed"



def _normalize_central_sites_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = config if isinstance(config, dict) else {}
    site_mappings = raw.get("site_mappings") if isinstance(raw.get("site_mappings"), dict) else {}
    monitored_checks = raw.get("monitored_checks") if isinstance(raw.get("monitored_checks"), list) else []
    hardware_checks = raw.get("hardware_checks") if isinstance(raw.get("hardware_checks"), list) else []
    excluded_sites = raw.get("excluded_sites") if isinstance(raw.get("excluded_sites"), list) else []
    result: dict[str, Any] = {
        "site_mappings": {
            str(wsite).strip(): str(site_name).strip()
            for wsite, site_name in site_mappings.items()
            if str(wsite).strip() and str(site_name).strip()
        },
        "monitored_checks": [check for check in monitored_checks if isinstance(check, dict)],
        "hardware_checks": [check for check in hardware_checks if isinstance(check, dict)],
        "excluded_sites": [str(s).strip().casefold() for s in excluded_sites if str(s).strip()],
    }
    # Preserve any extra fields (e.g. monitored_items) stored in the raw config
    for key, value in raw.items():
        if key not in result:
            result[key] = value
    return result


def _aggregate_central_payload(tenant_id: str) -> dict[str, Any]:
    from ...tasks import _cache_updated_at, _hub_central_status

    tenant = _get_tenant(tenant_id)
    spokes = _approved_spokes(tenant_id)
    mode = _central_mode(tenant)
    central_sites_config = _normalize_central_sites_config(store.get_tenant_central_sites_config(tenant_id))

    # Pull live client_count_status and per-spoke data from hub's in-memory cache (centralized mode).
    agg_client_count_status: dict[str, Any] = {}
    hub_spokes_data: dict[str, Any] = {}
    if mode == "centralized":
        is_stale = time.time() - _cache_updated_at.get(tenant_id, 0) > 300
        if not is_stale:
            tenant_data = _hub_central_status.get(tenant_id, {})
            ccs = tenant_data.get("client_count_status")
            if isinstance(ccs, dict):
                agg_client_count_status = ccs
            spokes_cache = tenant_data.get("spokes")
            if isinstance(spokes_cache, dict):
                hub_spokes_data = spokes_cache

    spokes_out = []
    for spoke in spokes:
        central = _central_telemetry(spoke)
        spoke_ccs: dict[str, Any] = {}
        if mode == "distributed":
            ccs = central.get("client_count_status")
            if isinstance(ccs, dict):
                spoke_ccs = ccs
                for wsite, info in spoke_ccs.items():
                    if wsite not in agg_client_count_status and isinstance(info, dict):
                        agg_client_count_status[wsite] = info
        else:
            # Centralized: spoke shares the tenant-level client count status
            spoke_ccs = agg_client_count_status

        # Build sites list from hub cache (centralized) or spoke telemetry (distributed)
        spoke_hub_data = hub_spokes_data.get(spoke.id, {}) if isinstance(hub_spokes_data.get(spoke.id), dict) else {}
        site_mappings = spoke_hub_data.get("site_mappings", {}) if isinstance(spoke_hub_data.get("site_mappings"), dict) else {}
        if not site_mappings:
            site_mappings = dict(central_sites_config.get("site_mappings") or {}) if isinstance(central_sites_config.get("site_mappings"), dict) else {}
        spoke_status = spoke_hub_data.get("status", {}) if isinstance(spoke_hub_data.get("status"), dict) else {}
        spoke_wireless = spoke_hub_data.get("wireless_clients", {}) if isinstance(spoke_hub_data.get("wireless_clients"), dict) else {}
        sites = [
            {
                "wsite": wsite,
                "central_site": central_site,
                "wireless_clients": spoke_wireless.get(wsite),
                "status_map": spoke_status.get(wsite, {}) if isinstance(spoke_status.get(wsite), dict) else {},
            }
            for wsite, central_site in site_mappings.items()
        ]

        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "spoke_online": _is_online(spoke),
            "last_seen": spoke.last_seen,
            "assigned_sites": spoke.assigned_sites or [],
            "client_count_status": spoke_ccs,
            "sites": sites,
            "central_status": central,
        })

    return {
        "tenant_id": tenant_id,
        "hub_central_config": _serialize_hub_central_config(tenant),
        "central_sites_config": central_sites_config,
        "mode": mode,
        "client_count_status": agg_client_count_status,
        "spokes": spokes_out,
    }


def _store_and_queue_tenant_config(
    tenant_id: str,
    hub_config_updates: dict[str, Any],
    *,
    spoke_config_updates: dict[str, Any] | None = None,
    force_push: bool = False,
) -> list[dict[str, Any]]:
    tenant = _get_tenant(tenant_id)
    next_hub_config = dict(tenant.hub_config or {})
    next_hub_config.update(hub_config_updates or {})
    tenant_changed = next_hub_config != (tenant.hub_config or {})
    if tenant_changed:
        tenant.hub_config = next_hub_config
        store.save_tenant(tenant)

    effective_spoke_updates = dict(spoke_config_updates if spoke_config_updates is not None else hub_config_updates or {})
    updated_spokes: list[dict[str, Any]] = []
    for spoke in _approved_spokes(tenant_id):
        config_changed = False
        next_spoke_config = dict(spoke.config or {})
        if effective_spoke_updates:
            next_spoke_config.update(effective_spoke_updates)
            config_changed = next_spoke_config != (spoke.config or {})
            if config_changed:
                spoke.config = next_spoke_config

        should_queue = force_push or tenant_changed or config_changed
        if should_queue:
            spoke.config_version += 1
            store.save_spoke(spoke)
            store.ensure_config_update_command(tenant_id, spoke.id)
        elif config_changed:
            store.save_spoke(spoke)

        updated_spokes.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "config_version": spoke.config_version,
            "applied_config_version": spoke.applied_config_version,
            "last_config_applied_at": spoke.last_config_applied_at,
        })
    return updated_spokes


# ── Hub-managed conf overrides ────────────────────────────────────────────────
# These allow a tenant admin to override configs/simulation.conf and
# configs/user-overrides.conf without needing GitHub write access.
# Overrides are stored on the hub and pushed to connected spokes via
# config_update.  When the spoke is standalone (no hub), GitHub files apply.

class ConfOverrideRequest(BaseModel):
    content: str  # Raw INI text in the same format as the .conf file


def _push_conf_overrides_to_spokes(tenant_id: str, current_user: User) -> int:
    """Bump config_version on all approved spokes to push updated overrides."""
    count = 0
    for spoke in store.get_spokes(tenant_id):
        if spoke.status != "approved":
            continue
        spoke.config_version = (spoke.config_version or 0) + 1
        store.save_spoke(spoke)
        store.ensure_config_update_command(tenant_id, spoke.id)
        count += 1
    return count


# ── Demo Scenario Endpoints ────────────────────────────────────────────────────
# Hub → Spoke relay for demo user scenario triggers.
# Demo scenarios are ephemeral (in-memory on spoke, cleared on hub/spoke reboot
# and auto-expired after 120 minutes).

class DemoScenarioRequest(BaseModel):
    scenario: str  # e.g. "dns_fail", "dhcp_fail", "normal"


class ClientSimOverrideRequest(BaseModel):
    simulation: str  # e.g. "dns_fail", "ping_test"
    enabled: bool


async def _relay_demo_command(tenant_id: str, spoke_id: str, message: dict) -> bool:
    """Send a demo command to a spoke via WebSocket relay. Returns True if sent."""
    from ...ws import relay_ws
    return await relay_ws.send_to_spoke(tenant_id, spoke_id, message)


def _modify_ini_content(content: str, section: str, key: str, enabled: bool) -> str:
    """Add or remove a key=on entry in an INI section without disrupting surrounding content.

    - enabled=True  → sets [section]\nkey=on  (adds section if missing)
    - enabled=False → removes key from section (leaves section header if other keys exist)
    """
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    updated: list[str] = []
    section_found = False
    in_target = False
    key_handled = False

    for line in lines:
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            if in_target and enabled and not key_handled:
                updated.append(f"{key}=on")
                key_handled = True
            in_target = (m.group(1) == section)
            section_found = section_found or in_target
            updated.append(line)
            continue

        if in_target:
            km = re.match(r"^(\s*)([^=\s#;][^=]*?)\s*=.*$", line)
            if km and km.group(2).strip() == key:
                key_handled = True
                if enabled:
                    updated.append(f"{km.group(1)}{key}=on")
                # else: skip (remove the key)
                continue

        updated.append(line)

    # End of file: still in target section and key not yet written
    if in_target and enabled and not key_handled:
        updated.append(f"{key}=on")

    # Section didn't exist — create it at end of file
    if not section_found and enabled:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"[{section}]")
        updated.append(f"{key}=on")

    result = newline.join(updated)
    if not result.endswith(newline) and (not content or content.endswith("\n") or content.endswith("\r\n")):
        result += newline
    return result


class UsbVidpidEntry(BaseModel):
    vidpid: str
    type: str = ""
    label: str = ""


async def _refresh_central_browse(tenant_id: str) -> None:
    """Fetch fresh Central browse data for one tenant and store in memory + disk."""
    now = time.time()
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return
    mode = _central_mode(tenant)

    if mode == "centralized":
        if not tenant.aruba_config_enc:
            result = {"sites": [], "alerts": [], "insights": [], "clients": [],
                      "mode": mode, "warning": "Central not configured on hub.",
                      "cached_at": now}
        else:
            try:
                cfg = decrypt_dict(tenant.aruba_config_enc)
                cfg["cluster_url"] = validate_cluster_url(cfg.get("cluster_url", ""))
            except Exception:
                result = {"sites": [], "alerts": [], "insights": [], "clients": [],
                          "mode": mode, "warning": "Could not read Central config.",
                          "cached_at": now}
                _central_browse_cache[tenant_id] = result
                _central_browse_cache_ts[tenant_id] = now
                _save_browse_disk_cache(tenant_id, result)
                return
            aruba = ArubaClient(cfg)
            if not aruba.is_configured():
                result = {"sites": [], "alerts": [], "insights": [], "clients": [],
                          "mode": mode, "warning": "Central not configured.",
                          "cached_at": now}
            else:
                try:
                    data = await aruba.browse_all()
                    result = {**data, "mode": mode, "cached_at": now,
                              "warning": data.get("warning")}
                except Exception as exc:
                    logger.warning("central_browse refresh failed for %s: %s", tenant_id, exc)
                    # Keep existing cache on error — don't overwrite with empty
                    return
    else:
        sites_map: dict[str, dict[str, Any]] = {}
        alerts: list[dict[str, Any]] = []
        insights: list[dict[str, Any]] = []
        clients: list[dict[str, Any]] = []
        devices_by_site: dict[str, list[dict[str, Any]]] = {}
        clients_by_site: dict[str, dict[str, Any]] = {}
        # Track seen alert/insight/device keys to deduplicate across spokes sharing the same sites
        seen_alert_keys: set[tuple[str, str]] = set()
        seen_insight_keys: set[tuple[str, str]] = set()
        seen_device_keys: set[tuple[str, str]] = set()
        for spoke in _approved_spokes(tenant_id):
            central = _central_telemetry(spoke)
            for wsite, central_site in (central.get("site_mappings") or {}).items():
                if wsite not in sites_map:
                    wc = (central.get("wireless_clients") or {}).get(wsite)
                    sites_map[wsite] = {"name": wsite, "central_site": central_site,
                                        "wireless_clients": wc, "health_score": None,
                                        "site_id": "", "status": central_site or "—"}
            spoke_nc_alerts = central.get("central_alerts") or []
            if spoke_nc_alerts:
                for alert in spoke_nc_alerts:
                    key = (str(alert.get("name") or "").lower(), str(alert.get("site") or "").lower())
                    if key not in seen_alert_keys:
                        seen_alert_keys.add(key)
                        alerts.append(alert)
            else:
                for wsite, checks in (central.get("status") or {}).items():
                    for check_id, info in (checks or {}).items():
                        if info and info.get("status") == "ERROR":
                            key = (check_id, wsite)
                            if key not in seen_alert_keys:
                                seen_alert_keys.add(key)
                                alerts.append({"name": info.get("check_name") or check_id,
                                               "site": wsite, "severity": "error",
                                               "detail": f"Count: {info.get('count', 0)}",
                                               "ts": info.get("ts")})
            for insight in (central.get("central_insights") or []):
                key = (str(insight.get("name") or "").lower(), str(insight.get("site") or "").lower())
                if key not in seen_insight_keys:
                    seen_insight_keys.add(key)
                    insights.append(insight)
            for site_name, devs in (central.get("central_devices_by_site") or {}).items():
                for dev in devs:
                    key = (site_name, str(dev.get("name") or dev.get("serial") or "").lower())
                    if key not in seen_device_keys:
                        seen_device_keys.add(key)
                        devices_by_site.setdefault(site_name, []).append(dev)
            for site_name, counts in (central.get("central_clients_by_site") or {}).items():
                # First-seen wins: multiple spokes report the same Central site counts
                # (they all query the same Aruba Central API). Adding them together
                # would multiply the real count by the number of spokes.
                if site_name not in clients_by_site:
                    clients_by_site[site_name] = counts
            seen_client_keys: set[str] = {
                str(c.get("mac") or c.get("hostname") or "")
                for c in clients if isinstance(c, dict)
            }
            for client in _telemetry_clients(spoke):
                if not isinstance(client, dict):
                    continue
                # Deduplicate across spokes by MAC (preferred) or hostname
                key = str(client.get("mac") or client.get("hostname") or "")
                if key and key in seen_client_keys:
                    continue
                if key:
                    seen_client_keys.add(key)
                clients.append(client)
        result = {
            "sites": sorted(sites_map.values(), key=lambda item: str(item.get("name") or "").casefold()),
            "alerts": alerts, "insights": insights, "clients": clients,
            "clients_by_site": clients_by_site, "devices_by_site": devices_by_site,
            "mode": mode, "cached_at": now, "warning": None,
        }

    _central_browse_cache[tenant_id] = result
    _central_browse_cache_ts[tenant_id] = now
    _save_browse_disk_cache(tenant_id, result)
    logger.debug("central_browse: refreshed cache for tenant %s", tenant_id)


# ── Monitored Items ──────────────────────────────────────────────────────────

class MonitoredItemCreate(BaseModel):
    type: str  # "site", "alert", "insight", "client"
    name: str
    identifier: str  # lookup key: site/alert/insight name, or client MAC


__all__ = [
    'json',
    'base64',
    're',
    'Counter',
    'datetime',
    'timedelta',
    'timezone',
    'logging',
    'secrets',
    'time',
    'Path',
    'Any',
    'Optional',
    'urlparse',
    'httpx',
    'APIRouter',
    'Body',
    'Depends',
    'HTTPException',
    'Query',
    'BaseModel',
    'Field',
    'auth',
    'store',
    'ArubaClient',
    'DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS',
    'DEFAULT_NEW_CENTRAL_MONITORED_CHECKS',
    'validate_cluster_url',
    'decrypt_dict',
    'encrypt_dict',
    'AuditEntry',
    'Command',
    'Spoke',
    'Tenant',
    'User',
    'logger',
    'FAIL_STATUSES',
    'PASS_STATUSES',
    'WARNING_STATUSES',
    'MODE_VALUES',
    'CENTRAL_WEBHOOK_HOST',
    '_central_browse_cache',
    '_central_browse_cache_ts',
    '_CENTRAL_BROWSE_TTL',
    '_browse_cache_path',
    '_load_browse_disk_cache',
    '_save_browse_disk_cache',
    '_is_individual_browse_client',
    '_has_legacy_client_summary_rows',
    '_normalize_browse_cache',
    '_central_webhook_endpoint_url',
    '_load_aruba_config',
    '_persist_aruba_config',
    'ConfigPushRequest',
    'CentralConfigPayload',
    'CentralUpdateRequest',
    'CentralSitesConfigPayload',
    'SimulationConfUpdateRequest',
    '_resolve_tenant_id',
    '_require_tenant_admin',
    '_require_tenant_access',
    '_require_tenant_demo_or_above',
    '_approved_spokes',
    '_get_tenant',
    '_is_online',
    '_telemetry_clients',
    '_telemetry_dict',
    '_telemetry_list',
    '_coerce_int',
    '_setting_toggle',
    '_spoke_usb_capacity',
    '_central_telemetry',
    '_hardware_type',
    '_normalize_lookup_value',
    '_spoke_usb_lookup',
    '_spoke_t3_lookup',
    '_client_has_usb',
    '_client_has_t3_pci',
    '_record_check',
    '_build_checks_summary',
    '_serialize_hub_central_config',
    '_github_repo_settings',
    '_parse_github_repo',
    '_github_api_headers',
    '_require_sim_repo_config',
    '_github_error_detail',
    '_fetch_simulation_conf_from_github',
    '_fetch_user_overrides_conf_from_github',
    '_validated_cluster_url_or_400',
    '_queue_repo_sync_for_all_spokes',
    '_central_mode',
    '_normalize_central_sites_config',
    '_aggregate_central_payload',
    '_store_and_queue_tenant_config',
    'ConfOverrideRequest',
    '_push_conf_overrides_to_spokes',
    'DemoScenarioRequest',
    'ClientSimOverrideRequest',
    '_relay_demo_command',
    '_modify_ini_content',
    'UsbVidpidEntry',
    '_refresh_central_browse',
    'MonitoredItemCreate',
]
