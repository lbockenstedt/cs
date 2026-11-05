"""Background workers for Hub maintenance, monitoring, and command routing.

These coroutines are started from the FastAPI lifespan hook and run on fixed
intervals: gkill polling every 5 minutes, heartbeat checks every 30 seconds,
auto-recovery every 30 minutes, schedule evaluation every minute, Aruba polling
every 5 minutes, state-transition notifications every minute, and maintenance
purges every 5 minutes. Each worker evaluates tenant and spoke processing
mode so Hub either executes centrally or queues work for distributed spoke
execution through the spoke inbox/ack relay.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import store
from .aruba import (
    ArubaClient,
    ArubaFinding,
    DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS,
    DEFAULT_NEW_CENTRAL_MONITORED_CHECKS,
    validate_cluster_url,
)
from .check_eval import count_for_check, normalize_counts
from .crypto import decrypt_dict
from .data_models import AuditEntry, Command
from .config import get_settings
from .notifications import get_notification_config
from .ws import ws_broadcast

logger = logging.getLogger(__name__)

# Text file holding "on" or "off" for the global kill switch.
GKILL_SWITCH_URL = "https://raw.githubusercontent.com/solutions-hpe/main/main/gkill"

# Latest gkill value, and each tenant's spoke online state as last broadcast.
gkill_state: dict[str, Any] = {"value": "off", "last_fetched": 0.0, "error": None}
spoke_online: dict[str, dict[str, bool]] = {}
# Hub-side Aruba Central status cache: tenant_id → polled status
_hub_central_status: dict[str, dict] = {}
_cache_updated_at: dict[str, float] = {}
# Per-tenant rolling 60-min client count samples: tenant_id → {wsite → [(ts, count), ...]}
_hub_client_samples: dict[str, dict[str, list[tuple[float, int]]]] = {}
_hub_client_baseline: dict[str, dict[str, dict[str, float]]] = {}

# Client-count drop alert: minimum samples, drop percent, and window length.
HUB_CLIENT_COUNT_MIN_SAMPLES = 3
HUB_CLIENT_COUNT_DROP_PCT = 25.0
HUB_CLIENT_WINDOW_SECS = 3600

# Event-loop lag measured by loop_lag_monitor (updated every 100ms).
# Exposed in telemetry_ack so spokes echo it back in their next payload,
# letting the hub store and surface it via the aggregate API.
hub_loop_lag_ms: float = 0.0
_LAG_MONITOR_INTERVAL = 0.1  # 100 ms reference sleep


async def loop_lag_monitor() -> None:
    """Measure hub asyncio event-loop responsiveness continuously.

    Schedules a 100 ms sleep and records how much longer than 100 ms it actually
    waited.  Any excess is time the event loop spent in synchronous blocking work.
    A healthy hub idles near 0 ms; sustained values above 200 ms indicate
    blocking calls on the hot path (synchronous disk I/O, CPU-bound work, etc.).
    """
    global hub_loop_lag_ms
    # Time a 100 ms sleep; the excess is how long the loop was blocked.
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(_LAG_MONITOR_INTERVAL)
        excess = (time.monotonic() - t0 - _LAG_MONITOR_INTERVAL) * 1000
        hub_loop_lag_ms = round(max(excess, 0.0), 1)
        # Warn on lag above 200 ms.
        if hub_loop_lag_ms > 200:
            logger.warning("hub event-loop lag: %.0f ms — synchronous blocking detected", hub_loop_lag_ms)


def _set_hub_central_status(tenant_id: str, payload: dict[str, Any]) -> None:
    """Store a tenant's polled Central status and when it was stored."""
    _hub_central_status[tenant_id] = payload
    _cache_updated_at[tenant_id] = time.time()


def _clear_hub_central_status(tenant_id: str) -> None:
    """Remove a tenant's cached Central status."""
    _hub_central_status.pop(tenant_id, None)
    _cache_updated_at.pop(tenant_id, None)


def _now() -> datetime:
    """Return the current UTC timestamp for task scheduling and retention checks."""
    return datetime.now(timezone.utc)


def _cmd(spoke_id: str, tenant_id: str, cmd_type: str, payload: dict[str, Any]) -> Command:
    """Build a 24-hour command queue entry destined for a specific spoke."""
    return Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type=cmd_type,
        payload=payload,
        expires_at=_now() + timedelta(hours=24),
    )


def _audit(spoke_id: str, tenant_id: str, task_type: str, mode: str, status: str, detail: str = "") -> AuditEntry:
    """Create a normalized audit record for work initiated by background tasks."""
    return AuditEntry(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        task_type=task_type,
        execution_mode=mode,
        status=status,
        detail=detail,
        initiated_by="system",
    )


async def gkill_switch_poller() -> None:
    """Poll global kill switch from GitHub every 5 minutes. Broadcast changes."""
    # One HTTP client reused for every poll.
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                # Fetch the value; anything other than on/off counts as off.
                resp = await client.get(GKILL_SWITCH_URL)
                value = resp.text.strip().lower()
                if value not in {"on", "off"}:
                    value = "off"
                # Store the new value and clear any old error.
                prev = gkill_state["value"]
                gkill_state["value"] = value
                gkill_state["last_fetched"] = _now().timestamp()
                gkill_state["error"] = None
                # Broadcast only on a change.
                if value != prev:
                    logger.warning("GKill switch changed: %s → %s", prev, value)
                    await _broadcast_gkill(value)
            # Let shutdown cancel the loop.
            except asyncio.CancelledError:
                raise
            # Record the error and keep polling.
            except Exception as exc:
                gkill_state["error"] = str(exc)
                logger.warning("GKill switch fetch failed: %s", exc)
            # Every 5 minutes.
            await asyncio.sleep(300)


async def _broadcast_gkill(value: str) -> None:
    """Publish a new gkill value to the UI and queue updates for distributed islands."""
    # Tell the browsers.
    await ws_broadcast({"type": "gkill_switch_update", "value": value})
    # Queue an update for each approved spoke that runs gkill itself, and audit it.
    for tenant in store.list_tenants():
        for spoke in store.list_spokes(tenant.id):
            if spoke.status != "approved":
                continue
            mode = spoke.processing_mode.resolve("gkill")
            if mode == "distributed":
                store.enqueue_command(_cmd(spoke.id, tenant.id, "gkill_update", {"value": value}))
            store.append_audit(_audit(spoke.id, tenant.id, "gkill", mode, "success", f"gkill={value}"))


async def heartbeat_monitor() -> None:
    """Check spoke last_seen every 30 seconds. Update online state, broadcast changes."""
    # Wait 30 seconds before the first check.
    await asyncio.sleep(30)
    while True:
        try:
            changed = False
            # Check each approved spoke in each tenant.
            for tenant in store.list_tenants():
                tenant_state = spoke_online.setdefault(tenant.id, {})
                for spoke in store.list_spokes(tenant.id):
                    if spoke.status != "approved":
                        continue
                    # Use 300s to match the frontend isOnline() threshold — keeps the
                    # WS heartbeat_update in sync with what the browser displays.
                    online = bool(spoke.last_seen and (_now() - spoke.last_seen).total_seconds() < 300)
                    # Log and record each change.
                    prev = tenant_state.get(spoke.id)
                    if prev != online:
                        tenant_state[spoke.id] = online
                        changed = True
                        logger.info(
                            "Spoke %s (%s) went %s",
                            spoke.hostname,
                            spoke.id,
                            "online" if online else "offline",
                        )
            # Broadcast the full online map if anything changed.
            if changed:
                await ws_broadcast(
                    {
                        "type": "heartbeat_update",
                        "island_online": {tenant_id: dict(values) for tenant_id, values in spoke_online.items()},
                    }
                )
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Heartbeat monitor error: %s", exc)
        await asyncio.sleep(30)


async def auto_recovery_check() -> None:
    """Check for islands offline > configured threshold and queue recovery commands."""
    # First check after 30 minutes.
    await asyncio.sleep(1800)
    while True:
        try:
            # Check each approved spoke in each tenant.
            for tenant in store.list_tenants():
                for spoke in store.list_spokes(tenant.id):
                    if spoke.status != "approved":
                        continue
                    # Offline limit in hours (default 24); spokes never seen are skipped.
                    timeout_hours = float(spoke.config.get("vm_silent_timeout", 24))
                    if not spoke.last_seen:
                        continue
                    # Past the limit: queue a recovery, audit it, and alert the browsers.
                    offline_secs = (_now() - spoke.last_seen).total_seconds()
                    if offline_secs > timeout_hours * 3600:
                        mode = spoke.processing_mode.resolve("heartbeat")
                        store.enqueue_command(
                            _cmd(
                                spoke.id,
                                tenant.id,
                                "auto_recovery",
                                {"reason": f"Spoke offline for {offline_secs / 3600:.1f}h"},
                            )
                        )
                        store.append_audit(
                            _audit(
                                spoke.id,
                                tenant.id,
                                "auto_recovery",
                                mode,
                                "pending",
                                f"Offline {offline_secs / 3600:.1f}h — recovery queued",
                            )
                        )
                        await ws_broadcast(
                            {
                                "type": "notification",
                                "level": "warning",
                                "tenant_id": tenant.id,
                                "message": f"Auto-recovery: spoke {spoke.hostname} offline {offline_secs / 3600:.1f}h",
                            }
                        )
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Auto-recovery error: %s", exc)
        await asyncio.sleep(1800)


# Last minute each spoke's schedule fired, so it fires once per minute.
_last_schedule_trigger: dict[str, str] = {}


async def schedule_check() -> None:
    """Check per-spoke schedules every 60 seconds."""
    # First check after 60 seconds.
    await asyncio.sleep(60)
    while True:
        try:
            # Today's day name.
            now = _now()
            day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            current_day = day_names[now.weekday()]

            # Check each approved spoke with a reclone schedule.
            for tenant in store.list_tenants():
                for spoke in store.list_spokes(tenant.id):
                    if spoke.status != "approved":
                        continue
                    cfg = spoke.config
                    if cfg.get("reclone_schedule_enabled") != "on":
                        continue
                    # Schedule format is "<day> HH:MM"; bad formats are skipped.
                    cron = cfg.get("reclone_schedule_cron", "sunday 02:00")
                    parts = cron.strip().lower().split()
                    if len(parts) != 2:
                        continue
                    sched_day = parts[0]
                    try:
                        sched_hour, sched_min = map(int, parts[1].split(":"))
                    except ValueError:
                        continue

                    # Only fire at the scheduled minute.
                    if current_day != sched_day or now.hour != sched_hour or now.minute != sched_min:
                        continue

                    # Only fire once for this minute.
                    trigger_key = now.strftime("%Y-%m-%d %H:%M")
                    if _last_schedule_trigger.get(spoke.id) == trigger_key:
                        continue
                    _last_schedule_trigger[spoke.id] = trigger_key

                    # Queue the reclone and audit it.
                    mode = spoke.processing_mode.resolve("schedules")
                    store.enqueue_command(_cmd(spoke.id, tenant.id, "reclone_schedule", {}))
                    store.append_audit(_audit(spoke.id, tenant.id, "schedule", mode, "pending", f"Scheduled reclone: {cron}"))
                    logger.info("Schedule triggered reclone for spoke %s (%s)", spoke.hostname, spoke.id)
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Schedule check error: %s", exc)
        await asyncio.sleep(60)


# Aruba clients per tenant and the hash of the config each was built from.
_aruba_clients: dict[str, ArubaClient] = {}
_aruba_client_hashes: dict[str, str] = {}


def _get_aruba_client(tenant_id: str) -> ArubaClient | None:
    """Return a cached Aruba client for a tenant, rebuilding it when config changes."""
    # No config: drop any cached client.
    tenant = store.get_tenant(tenant_id)
    if not tenant or not tenant.aruba_config_enc:
        _aruba_clients.pop(tenant_id, None)
        _aruba_client_hashes.pop(tenant_id, None)
        return None
    try:
        # Decrypt and check the config.
        cfg = decrypt_dict(tenant.aruba_config_enc)
        cfg["cluster_url"] = validate_cluster_url(cfg.get("cluster_url", ""))
        # Reuse the cached client if the config is unchanged.
        cfg_hash = hashlib.md5(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()
        cached = _aruba_clients.get(tenant_id)
        if cached and _aruba_client_hashes.get(tenant_id) == cfg_hash:
            return cached
        # Otherwise build and cache a new one.
        client = ArubaClient(cfg)
        _aruba_clients[tenant_id] = client
        _aruba_client_hashes[tenant_id] = cfg_hash
        return client
    # On error, log and drop the cached client.
    except Exception as exc:
        logger.warning("Failed to load Aruba config for tenant %s: %s", tenant_id, exc)
        _aruba_clients.pop(tenant_id, None)
        _aruba_client_hashes.pop(tenant_id, None)
        return None


def _normalize_site_token(value: Any) -> str:
    """Return a site name lowercased with extra spaces removed, for matching."""
    return " ".join(str(value or "").strip().casefold().split())


def _spoke_aruba_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of a spoke's Aruba config without its webhook fields, or None."""
    # Not a dict: nothing.
    if not isinstance(config, dict):
        return None
    return {
        key: value
        for key, value in config.items()
        if key not in {"webhook_id", "webhook_api_key"}
    }


def _finding_payload(finding: ArubaFinding) -> dict[str, Any]:
    """Convert an Aruba finding to a dict for the status payload."""
    return {
        "site": finding.site_name,
        "check": finding.check_name,
        "status": finding.status,
        "source": finding.source,
    }


def _merge_webhook_findings(
    findings: list[dict[str, Any]],
    webhook_findings: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return findings plus webhook findings not already present.

    Findings are matched on site, check and source.
    """
    # Keys of the findings already present.
    merged = list(findings)
    existing = {
        (str(item.get("site") or "").strip(), str(item.get("check") or "").strip(), str(item.get("source") or "").strip())
        for item in findings
        if isinstance(item, dict)
    }
    # Add each new, valid webhook finding.
    for item in (webhook_findings or {}).values():
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        key = (
            str(payload.get("site") or "").strip(),
            str(payload.get("check") or "").strip(),
            str(payload.get("source") or "").strip(),
        )
        if key in existing:
            continue
        merged.append(payload)
        existing.add(key)
    return merged


async def _auto_discover_hub_central_config(tenant_id: str, client: ArubaClient) -> tuple[dict[str, Any], dict[str, str]]:
    """Fill in a tenant's Central site config from what Central reports, and save any changes.

    Adds unmapped, non-excluded sites as same-name mappings and, for new Central, the
    default monitored and hardware checks if none are set. Returns the normalized config
    and a map of Central site id to site name.
    """
    # Load the saved config.
    config = dict(store.get_tenant_central_sites_config(tenant_id) or {})
    site_mappings = dict(config.get("site_mappings") or {}) if isinstance(config.get("site_mappings"), dict) else {}
    monitored_checks = list(config.get("monitored_checks") or []) if isinstance(config.get("monitored_checks"), list) else []
    hardware_checks = list(config.get("hardware_checks") or []) if isinstance(config.get("hardware_checks"), list) else []
    excluded_sites = {str(s).strip().casefold() for s in (config.get("excluded_sites") or []) if str(s).strip()}
    changed = False

    # Ask Central for its sites and map site id to name.
    discovered_sites = await client.list_sites()
    site_id_map = {
        str((site or {}).get("site_id") or "").strip(): str((site or {}).get("name") or "").strip()
        for site in discovered_sites
        if str((site or {}).get("site_id") or "").strip() and str((site or {}).get("name") or "").strip()
    }
    # Names already mapped on either side.
    existing_wsites = {_normalize_site_token(name) for name in site_mappings}
    existing_central = {_normalize_site_token(name) for name in site_mappings.values()}
    # Add a same-name mapping for each new site.
    for site in discovered_sites:
        site_name = str((site or {}).get("name") or "").strip()
        normalized = _normalize_site_token(site_name)
        # Skip sites already mapped, or explicitly excluded by the user
        if not normalized or normalized in existing_wsites or normalized in existing_central or normalized in excluded_sites:
            continue
        site_mappings[site_name] = site_name
        existing_wsites.add(normalized)
        existing_central.add(normalized)
        changed = True

    # New Central with no checks set: use the defaults.
    if client.api_version == "new_central" and not monitored_checks:
        monitored_checks = [dict(item) for item in DEFAULT_NEW_CENTRAL_MONITORED_CHECKS]
        changed = True
    if client.api_version == "new_central" and not hardware_checks:
        hardware_checks = [dict(item) for item in DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS]
        changed = True

    if changed:
        # Merge changes back into the full stored config to preserve all other fields
        # (excluded_sites, monitored_items, etc.) — never do a partial overwrite.
        updated = dict(config)
        updated["site_mappings"] = {str(wsite).strip(): str(central_site).strip() for wsite, central_site in site_mappings.items() if str(wsite).strip() and str(central_site).strip()}
        updated["monitored_checks"] = [dict(item) for item in monitored_checks if isinstance(item, dict)]
        updated["hardware_checks"] = [dict(item) for item in hardware_checks if isinstance(item, dict)]
        store.set_tenant_central_sites_config(tenant_id, updated)

    # Return the cleaned config and the site id map.
    normalized_config = {
        "site_mappings": {str(wsite).strip(): str(central_site).strip() for wsite, central_site in site_mappings.items() if str(wsite).strip() and str(central_site).strip()},
        "monitored_checks": [dict(item) for item in monitored_checks if isinstance(item, dict)],
        "hardware_checks": [dict(item) for item in hardware_checks if isinstance(item, dict)],
    }
    return normalized_config, site_id_map


def _load_hub_client_baseline(tenant_id: str) -> None:
    """Load a tenant's saved client-count baselines into memory, once."""
    # Already loaded.
    if tenant_id in _hub_client_baseline:
        return
    # Read the baselines from the tenant's hub config.
    tenant = store.get_tenant(tenant_id)
    saved = ((tenant.hub_config or {}).get("hub_client_baseline", {}) if tenant else {}) or {}
    baseline: dict[str, dict[str, float]] = {}
    # Keep only well-formed entries.
    if isinstance(saved, dict):
        for wsite, info in saved.items():
            if not isinstance(info, dict):
                continue
            try:
                hourly_avg = float(info.get("hourly_avg") or 0.0)
                recorded_at = float(info.get("recorded_at") or 0.0)
            except (TypeError, ValueError):
                continue
            baseline[str(wsite)] = {"hourly_avg": hourly_avg, "recorded_at": recorded_at}
    _hub_client_baseline[tenant_id] = baseline


def _refresh_hub_client_baseline(tenant_id: str) -> None:
    """Recompute each site's baseline average from the current samples."""
    tenant_samples = _hub_client_samples.get(tenant_id, {})
    baseline = _hub_client_baseline.setdefault(tenant_id, {})
    # Sites with too few samples keep their old baseline.
    for wsite, samples in tenant_samples.items():
        if len(samples) < HUB_CLIENT_COUNT_MIN_SAMPLES:
            continue
        baseline[wsite] = {
            "hourly_avg": sum(sample[1] for sample in samples) / len(samples),
            "recorded_at": samples[-1][0],
        }


def _persist_hub_client_baseline(tenant_id: str) -> None:
    """Save a tenant's baselines into its hub config if they changed."""
    # Tenant must exist.
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return
    # Clean up the baselines.
    baseline = _hub_client_baseline.get(tenant_id, {})
    payload = {
        str(wsite): {
            "hourly_avg": float(info.get("hourly_avg") or 0.0),
            "recorded_at": float(info.get("recorded_at") or 0.0),
        }
        for wsite, info in baseline.items()
        if isinstance(info, dict)
    }
    # Save only if changed.
    hub_config = dict(tenant.hub_config or {})
    if hub_config.get("hub_client_baseline") == payload:
        return
    hub_config["hub_client_baseline"] = payload
    tenant.hub_config = hub_config
    store.save_tenant(tenant)


def _hub_client_count_payload(tenant_id: str, site_mappings: dict[str, str]) -> dict[str, Any]:
    """Return each site's current client count, average, drop percent and status.

    With too few samples, the saved baseline is used and marked stale; with no baseline
    either, the status is NO_DATA.
    """
    # Make sure baselines are loaded.
    _load_hub_client_baseline(tenant_id)
    tenant_samples = _hub_client_samples.get(tenant_id, {})
    baseline = _hub_client_baseline.get(tenant_id, {})
    result: dict[str, Any] = {}
    # Each site with samples; the latest sample is the current count.
    for wsite, samples in tenant_samples.items():
        if not samples:
            continue
        current = samples[-1][1]
        site_name = site_mappings.get(wsite, wsite)
        # Too few samples: compare with the saved baseline, if any.
        if len(samples) < HUB_CLIENT_COUNT_MIN_SAMPLES:
            saved = baseline.get(wsite)
            if saved:
                avg = float(saved.get("hourly_avg") or 0.0)
                drop_pct = max(0.0, (avg - current) / avg * 100.0) if avg >= 1 else 0.0
                status = "DEGRADED" if drop_pct >= HUB_CLIENT_COUNT_DROP_PCT else "OK"
                result[wsite] = {
                    "site_name": site_name,
                    "current": current,
                    "hourly_avg": avg,
                    "drop_pct": drop_pct,
                    "status": status,
                    "ts": samples[-1][0],
                    "baseline_stale": True,
                    "baseline_recorded_at": saved.get("recorded_at"),
                }
            else:
                result[wsite] = {
                    "site_name": site_name,
                    "current": current,
                    "hourly_avg": current,
                    "drop_pct": 0.0,
                    "status": "NO_DATA",
                    "ts": samples[-1][0],
                    "baseline_stale": False,
                }
            continue
        # Enough samples: compare with the window average.
        avg = sum(sample[1] for sample in samples) / len(samples)
        drop_pct = max(0.0, (avg - current) / avg * 100.0) if avg >= 1 else 0.0
        status = "DEGRADED" if drop_pct >= HUB_CLIENT_COUNT_DROP_PCT else "OK"
        result[wsite] = {
            "site_name": site_name,
            "current": current,
            "hourly_avg": avg,
            "drop_pct": drop_pct,
            "status": status,
            "ts": samples[-1][0],
            "baseline_stale": False,
        }
    return result


async def hub_baseline_saver() -> None:
    """Save every tenant's client-count baselines once an hour.

    Baselines for deleted tenants are dropped.
    """
    # First save after an hour.
    await asyncio.sleep(HUB_CLIENT_WINDOW_SECS)
    while True:
        try:
            # Drop deleted tenants; refresh and save the rest.
            active_tenant_ids = {tenant.id for tenant in store.list_tenants()}
            for tenant_id in list(_hub_client_baseline):
                if tenant_id not in active_tenant_ids:
                    _hub_client_baseline.pop(tenant_id, None)
                    _hub_client_samples.pop(tenant_id, None)
                    continue
                _refresh_hub_client_baseline(tenant_id)
                _persist_hub_client_baseline(tenant_id)
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Hub baseline saver error: %s", exc)
        await asyncio.sleep(HUB_CLIENT_WINDOW_SECS)


async def aruba_poller() -> None:
    """Poll Aruba Central every 5 minutes per tenant. Centralized or push config to islands."""
    # First poll after 60 seconds.
    await asyncio.sleep(60)
    while True:
        try:
            # Each tenant and its approved spokes.
            tenants = store.list_tenants()
            active_tenant_ids = {tenant.id for tenant in tenants}
            for tenant in tenants:
                spokes = [spoke for spoke in store.list_spokes(tenant.id) if spoke.status == "approved"]

                # The hub's Aruba client, and the config to send if any spoke polls itself.
                client = _get_aruba_client(tenant.id)
                tenant_config: dict[str, Any] | None = None
                if any(spoke.processing_mode.resolve("aruba_polling") == "distributed" for spoke in spokes):
                    if tenant.aruba_config_enc:
                        try:
                            tenant_config = _spoke_aruba_config(decrypt_dict(tenant.aruba_config_enc))
                        except Exception as exc:
                            logger.warning("Failed to load Aruba config for tenant %s: %s", tenant.id, exc)

                # Send the config to spokes that poll themselves; audit a failure if there is none.
                distributed_spokes = [spoke for spoke in spokes if spoke.processing_mode.resolve("aruba_polling") == "distributed"]
                for spoke in distributed_spokes:
                    if not tenant_config:
                        store.append_audit(_audit(spoke.id, tenant.id, "aruba_poll", "distributed", "failure", "Aruba config unavailable"))
                        continue
                    store.enqueue_command(_cmd(spoke.id, tenant.id, "aruba_config_update", tenant_config))
                    store.append_audit(_audit(spoke.id, tenant.id, "aruba_poll", "distributed", "pending", "Aruba config pushed"))

                # Check monitored items for distributed spokes using spoke telemetry
                if distributed_spokes:
                    try:
                        # Gather site, alert, insight, client and device names from those spokes'
                        # telemetry.
                        dist_site_names: set[str] = set()
                        dist_alert_names: set[str] = set()
                        dist_insight_names: set[str] = set()
                        dist_client_macs: set[str] = set()
                        dist_alert_ts: dict[str, str] = {}
                        dist_insight_ts: dict[str, str] = {}
                        dist_device_names: dict[str, str] = {}
                        for spoke in distributed_spokes:
                            central_tel = spoke.telemetry.get("central", {}) if isinstance(spoke.telemetry, dict) else {}
                            # Mapped site names.
                            for wsite, csite in (central_tel.get("site_mappings") or {}).items():
                                dist_site_names.add(str(wsite).strip().lower())
                                dist_site_names.add(str(csite).strip().lower())
                            # Checks with a nonzero count, split into alerts and insights.
                            for wsite, checks in (central_tel.get("status") or {}).items():
                                for check_id, info in (checks or {}).items():
                                    if isinstance(info, dict) and int(info.get("count") or 0) > 0:
                                        ctype = str(info.get("check_type") or "alert").lower()
                                        cname = str(info.get("check_name") or check_id).strip().lower()
                                        if ctype == "insight":
                                            dist_insight_names.add(cname)
                                        else:
                                            dist_alert_names.add(cname)
                            # Also pull from real browse data (new_central spokes send this in telemetry
                            # as "central_insights" / "central_alerts" — note: NOT "central_browse_*").
                            for ins in (central_tel.get("central_insights") or []):
                                n = str(ins.get("name") or "").strip().lower()
                                if n:
                                    dist_insight_names.add(n)
                                    if ins.get("ts") and n not in dist_insight_ts:
                                        dist_insight_ts[n] = str(ins["ts"])
                            for alt in (central_tel.get("central_alerts") or []):
                                n = str(alt.get("name") or "").strip().lower()
                                if n:
                                    dist_alert_names.add(n)
                                    if alt.get("ts") and n not in dist_alert_ts:
                                        dist_alert_ts[n] = str(alt["ts"])
                            # Client MACs.
                            for client_entry in (central_tel.get("clients") or []):
                                if isinstance(client_entry, dict) and client_entry.get("mac"):
                                    dist_client_macs.add(str(client_entry["mac"]).strip().lower())
                            # Collect gateway device names from browse data sent by distributed spokes
                            # (spoke sends "central_devices_by_site"; fall back to "devices_by_site" for older spokes)
                            for devs in (central_tel.get("central_devices_by_site") or central_tel.get("devices_by_site") or {}).values():
                                for dev in (devs or []):
                                    dev_key = str(dev.get("name") or dev.get("serial") or "").strip().lower()
                                    if dev_key and dev_key not in dist_device_names:
                                        dist_device_names[dev_key] = str(dev.get("last_seen") or "")
                        # Check monitored items against what was gathered.
                        await _check_monitored_items(tenant.id, "distributed", dist_site_names, dist_alert_names, dist_insight_names, dist_client_macs, dist_alert_ts, dist_insight_ts, dist_device_names)
                    # Log and carry on.
                    except Exception as exc:
                        logger.warning("Distributed monitored items check failed for tenant %s: %s", tenant.id, exc)

                centralized_spokes = [spoke for spoke in spokes if spoke.processing_mode.resolve("aruba_polling") == "centralized"]
                # Poll Central if there are centralized spokes, OR if no spokes are approved yet
                # but the tenant's default mode is centralized — the hub should show Central data
                # even before any spokes are provisioned.
                should_poll_central = bool(centralized_spokes) or (
                    not spokes and tenant.default_processing_mode.resolve("aruba_polling") == "centralized"
                )
                # No polling needed: clear the cached status.
                if not should_poll_central:
                    _clear_hub_central_status(tenant.id)
                    continue
                # No working client: store a not-configured status.
                if not client or not client.is_configured():
                    _set_hub_central_status(
                        tenant.id,
                        {
                            "spokes": {},
                            "token_valid": False,
                            "token_state": "not_configured",
                            "status": {},
                            "wireless_clients": {},
                            "hardware_alerts": [],
                            "client_count_status": {},
                            "central_sites_config": store.get_tenant_central_sites_config(tenant.id),
                        },
                    )
                    continue

                # Poll Central's alerts and insights.
                try:
                    findings = await client.poll_alerts_and_insights()
                # Poll failed: store and broadcast an error status, and audit a failure for each
                # spoke.
                except Exception as exc:
                    _set_hub_central_status(
                        tenant.id,
                        {
                            "spokes": {},
                            "token_valid": False,
                            "token_state": {"state": "error", "detail": str(exc)},
                            "status": {},
                            "wireless_clients": {},
                            "hardware_alerts": [],
                            "client_count_status": {},
                            "central_sites_config": store.get_tenant_central_sites_config(tenant.id),
                        },
                    )
                    await ws_broadcast(
                        {
                            "type": "aruba_update",
                            "tenant_id": tenant.id,
                            "findings": [],
                            "status": {},
                            "wireless_clients": {},
                            "hardware_alerts": [],
                            "client_count_status": {},
                            "central_sites_config": store.get_tenant_central_sites_config(tenant.id),
                            "token_state": {"state": "error", "detail": str(exc)},
                        }
                    )
                    for spoke in centralized_spokes:
                        store.append_audit(_audit(spoke.id, tenant.id, "aruba_poll", "centralized", "failure", str(exc)))
                    continue

                # Poll worked: audit a success for each centralized spoke.
                finding_payload = [_finding_payload(finding) for finding in findings]
                for spoke in centralized_spokes:
                    store.append_audit(
                        _audit(spoke.id, tenant.id, "aruba_poll", "centralized", "success", f"{len(findings)} findings")
                    )

                try:
                    # Fill in the site config from Central and read its mappings and checks.
                    site_raw: dict[str, dict[str, Any]] = {}
                    spokes_status: dict[str, dict[str, Any]] = {}
                    hub_sites_cfg, site_id_map = await _auto_discover_hub_central_config(tenant.id, client)
                    hub_site_mappings = hub_sites_cfg.get("site_mappings", {}) if isinstance(hub_sites_cfg.get("site_mappings"), dict) else {}
                    hub_monitored_checks = hub_sites_cfg.get("monitored_checks", []) if isinstance(hub_sites_cfg.get("monitored_checks"), list) else []
                    hub_hardware_checks = hub_sites_cfg.get("hardware_checks", []) if isinstance(hub_sites_cfg.get("hardware_checks"), list) else []
                    # The Central sites to poll and the hardware check ids.
                    unique_sites = {
                        str(site_name).strip()
                        for site_name in hub_site_mappings.values()
                        if str(site_name).strip()
                    }
                    hw_check_ids = {
                        str(check.get("id") or "").strip()
                        for check in hub_hardware_checks
                        if isinstance(check, dict) and str(check.get("id") or "").strip()
                    }

                    # Poll each Central site.
                    for central_site in unique_sites:
                        site_raw[central_site] = await client.poll_site_data(central_site, hw_check_ids=hw_check_ids)

                    # Add a client-count sample for each site, dropping those older than the window.
                    now = time.time()
                    cutoff = now - HUB_CLIENT_WINDOW_SECS
                    _load_hub_client_baseline(tenant.id)
                    tenant_samples = _hub_client_samples.setdefault(tenant.id, {})
                    for wsite, central_site in hub_site_mappings.items():
                        raw = site_raw.get(central_site, {})
                        wl_count = int(raw.get("client_count") or raw.get("wireless_clients") or 0)
                        samples = tenant_samples.setdefault(wsite, [])
                        samples.append((now, wl_count))
                        tenant_samples[wsite] = [sample for sample in samples if sample[0] >= cutoff]
                    # Refresh the baselines and work out the client-count status.
                    _refresh_hub_client_baseline(tenant.id)
                    client_count_status = _hub_client_count_payload(tenant.id, hub_site_mappings)

                    # Per-site results, and the sites where each hardware check found devices.
                    aggregated_status: dict[str, dict[str, Any]] = {}
                    aggregated_wireless_clients: dict[str, int] = {}
                    aggregated_total_clients: dict[str, int] = {}
                    hw_sites: dict[str, dict[str, list[str]]] = {
                        str(check.get("id")): {}
                        for check in hub_hardware_checks
                        if isinstance(check, dict) and str(check.get("id") or "").strip()
                    }

                    # For each mapped site, read that site's poll results.
                    for wsite, central_site in hub_site_mappings.items():
                        raw = site_raw.get(central_site, {})
                        alert_type_counts = raw.get("alert_type_counts", {}) if isinstance(raw.get("alert_type_counts"), dict) else {}
                        insight_cat_counts = raw.get("insight_cat_counts", {}) if isinstance(raw.get("insight_cat_counts"), dict) else {}
                        site_hw_devices = raw.get("hw_devices", {}) if isinstance(raw.get("hw_devices"), dict) else {}
                        site_status: dict[str, Any] = {}

                        # Case-insensitive lookup across BOTH the alert and insight
                        # buckets. The dashboard's alert/insight query is merged, so
                        # a check must fire whether Central classifies the named
                        # condition as an alert or an insight — e.g. "DNS Server
                        # Failed to Respond" returns as an INSIGHT, but its quota is
                        # typed "alert". Reading only the typed bucket (case-
                        # sensitively) reported a live condition as absent → the
                        # adaptive controller ramped forever + exhausted the pool.
                        # Shared with the other three deployments via check_eval
                        # (single source of truth for this matching).
                        _alert_ci = normalize_counts(alert_type_counts)
                        _insight_ci = normalize_counts(insight_cat_counts)
                        # DIAG: what the engine looks for vs what Central actually
                        # returned for this site. A monitored id absent from BOTH
                        # lists = a site-drop (poll_site_data filtered it) or a name
                        # diff; present = should fire now.
                        logger.info("central-check diag [%s→%s]: monitored=%s alert_keys=%s insight_keys=%s",
                                    wsite, central_site,
                                    [str(c.get("id")) for c in hub_monitored_checks if isinstance(c, dict) and c.get("id")],
                                    sorted(_alert_ci), sorted(_insight_ci))

                        # Status of each monitored alert/insight check: OK when it is present.
                        for check in hub_monitored_checks:
                            if not isinstance(check, dict):
                                continue
                            check_type = str(check.get("type") or "")
                            check_id = str(check.get("id") or "").strip()
                            check_name = str(check.get("name") or check_id)
                            if not check_id:
                                continue
                            if check_type not in ("alert", "insight"):
                                continue
                            count = count_for_check(check, _alert_ci, _insight_ci)
                            site_status[check_id] = {
                                "status": "OK" if count > 0 else "ERROR",
                                "count": count,
                                "check_name": check_name,
                                "check_type": check_type,
                                "ts": now,
                            }

                        # Store the site's status and client counts.
                        aggregated_status[wsite] = site_status
                        aggregated_wireless_clients[wsite] = int(raw.get("wireless_clients") or 0)
                        aggregated_total_clients[wsite] = int(raw.get("client_count") or raw.get("wireless_clients") or 0)

                        # Devices each hardware check found at this site.
                        for check in hub_hardware_checks:
                            if not isinstance(check, dict):
                                continue
                            check_id = str(check.get("id") or "").strip()
                            if not check_id:
                                continue
                            devices_for_check = site_hw_devices.get(check_id, {}) if isinstance(site_hw_devices.get(check_id), dict) else {}
                            device_names = sorted(str(name).strip() for name in devices_for_check if str(name).strip())
                            if device_names:
                                hw_sites.setdefault(check_id, {})[wsite] = device_names

                    # One hardware alert entry per check, listing the sites and devices.
                    aggregated_hardware_alerts: list[dict[str, Any]] = []
                    for check in hub_hardware_checks:
                        if not isinstance(check, dict):
                            continue
                        check_id = str(check.get("id") or "").strip()
                        if not check_id:
                            continue
                        devices_by_site = hw_sites.get(check_id, {})
                        aggregated_hardware_alerts.append(
                            {
                                "id": check_id,
                                "name": check.get("name") or check_id,
                                "device_type": check.get("device_type") or check_id,
                                "total": sum(len(devices) for devices in devices_by_site.values()),
                                "sites": {
                                    wsite: {"site_name": hub_site_mappings.get(wsite, wsite), "devices": devices}
                                    for wsite, devices in devices_by_site.items()
                                },
                            }
                        )

                    # Each centralized spoke gets a copy of the same results.
                    for spoke in centralized_spokes:
                        spokes_status[spoke.id] = {
                            "assigned_sites": spoke.assigned_sites,
                            "status": dict(aggregated_status),
                            "wireless_clients": dict(aggregated_wireless_clients),
                            "total_clients": dict(aggregated_total_clients),
                            "hardware_alerts": list(aggregated_hardware_alerts),
                            "client_count_status": dict(client_count_status),
                            "site_mappings": dict(hub_site_mappings),
                            "monitored_checks": list(hub_monitored_checks),
                            "hardware_checks": list(hub_hardware_checks),
                        }

                    # Keep earlier webhook findings and merge them into the poll's findings.
                    existing_webhook_findings = {}
                    existing_status = _hub_central_status.get(tenant.id, {})
                    if isinstance(existing_status.get("webhook_findings"), dict):
                        existing_webhook_findings = dict(existing_status.get("webhook_findings") or {})
                    merged_findings = _merge_webhook_findings(finding_payload, existing_webhook_findings)
                    # Store and broadcast the new status.
                    _set_hub_central_status(
                        tenant.id,
                        {
                            "spokes": spokes_status,
                            "token_valid": True,
                            "token_state": "connected",
                            "status": aggregated_status,
                            "wireless_clients": aggregated_wireless_clients,
                            "total_clients": aggregated_total_clients,
                            "hardware_alerts": aggregated_hardware_alerts,
                            "client_count_status": client_count_status,
                            "central_sites_config": hub_sites_cfg,
                            "site_id_map": site_id_map,
                            "webhook_findings": existing_webhook_findings,
                            "findings": merged_findings,
                        },
                    )
                    await ws_broadcast(
                        {
                            "type": "aruba_update",
                            "tenant_id": tenant.id,
                            "findings": merged_findings,
                            "status": aggregated_status,
                            "wireless_clients": aggregated_wireless_clients,
                            "total_clients": aggregated_total_clients,
                            "hardware_alerts": aggregated_hardware_alerts,
                            "client_count_status": client_count_status,
                            "central_sites_config": hub_sites_cfg,
                            "token_state": {"state": "connected", "detail": ""},
                        }
                    )
                    # Check monitored items against fresh Central data
                    try:
                        # Site names and the names of the alerts and insights found.
                        site_names_lower = {str(s).strip().lower() for s in hub_site_mappings.values() if s} | {str(s).strip().lower() for s in hub_site_mappings.keys() if s}
                        alert_names_lower = {str(f.check_name or "").strip().lower() for f in findings if isinstance(f, ArubaFinding) and f.source == "alert"}
                        insight_names_lower = {str(f.check_name or "").strip().lower() for f in findings if isinstance(f, ArubaFinding) and f.source == "insight"}
                        # Also augment from the real browse_all() data (cached — no extra API calls).
                        # poll_alerts_and_insights() uses sites-health reasons which does NOT include
                        # the insights/alerts from /network-notifications/v1/* so monitored items
                        # added via the browse tab would never match without this.
                        try:
                            # Alert and insight names and times, and device names.
                            browse = await client.browse_all()
                            alert_ts: dict[str, str] = {}
                            insight_ts: dict[str, str] = {}
                            device_names_lower: dict[str, str] = {}
                            for ins in (browse.get("insights") or []):
                                n = str(ins.get("name") or "").strip().lower()
                                if n:
                                    insight_names_lower.add(n)
                                    if ins.get("ts"):
                                        insight_ts[n] = str(ins["ts"])
                            for alt in (browse.get("alerts") or []):
                                n = str(alt.get("name") or "").strip().lower()
                                if n:
                                    alert_names_lower.add(n)
                                    if alt.get("ts"):
                                        alert_ts[n] = str(alt["ts"])
                            for devs in (browse.get("devices_by_site") or {}).values():
                                for dev in (devs or []):
                                    dev_key = str(dev.get("name") or dev.get("serial") or "").strip().lower()
                                    if dev_key:
                                        device_names_lower[dev_key] = str(dev.get("last_seen") or "")
                        # Browse failed: carry on without it.
                        except Exception as browse_exc:
                            alert_ts = {}
                            insight_ts = {}
                            device_names_lower = {}
                            logger.debug("browse_all() augment for monitored check failed: %s", browse_exc)
                        # Fetch clients only if there are client-type monitored items
                        # Is any monitored item a client?
                        cfg_snap = store.get_tenant_central_sites_config(tenant.id)
                        needs_clients = any(isinstance(mi, dict) and mi.get("type") == "client" for mi in (cfg_snap.get("monitored_items") or []))
                        client_macs_lower: set[str] = set()
                        # If so, get the client MACs; failures are ignored.
                        if needs_clients:
                            try:
                                cl_list = await client.list_clients()
                                client_macs_lower = {str(c.get("mac") or "").strip().lower() for c in cl_list if c.get("mac")}
                            except Exception:
                                pass
                        # Check the monitored items.
                        await _check_monitored_items(tenant.id, "centralized", site_names_lower, alert_names_lower, insight_names_lower, client_macs_lower, alert_ts, insight_ts, device_names_lower)
                    # Log and carry on.
                    except Exception as exc:
                        logger.warning("Monitored items check failed for tenant %s: %s", tenant.id, exc)
                # Site poll failed: store and broadcast an error status, and audit a failure for
                # each spoke.
                except Exception as exc:
                    _set_hub_central_status(
                        tenant.id,
                        {
                            "spokes": {},
                            "token_valid": False,
                            "token_state": {"state": "error", "detail": str(exc)},
                            "status": {},
                            "wireless_clients": {},
                            "hardware_alerts": [],
                            "client_count_status": {},
                            "central_sites_config": store.get_tenant_central_sites_config(tenant.id),
                        },
                    )
                    await ws_broadcast(
                        {
                            "type": "aruba_update",
                            "tenant_id": tenant.id,
                            "findings": finding_payload,
                            "status": {},
                            "wireless_clients": {},
                            "hardware_alerts": [],
                            "client_count_status": {},
                            "central_sites_config": store.get_tenant_central_sites_config(tenant.id),
                            "token_state": {"state": "error", "detail": str(exc)},
                        }
                    )
                    for spoke in centralized_spokes:
                        store.append_audit(_audit(spoke.id, tenant.id, "aruba_poll", "centralized", "failure", str(exc)))

            # Drop cached status, samples and clients for deleted tenants.
            for tenant_id in list(_hub_central_status):
                if tenant_id not in active_tenant_ids:
                    _clear_hub_central_status(tenant_id)
            for tenant_id in list(_hub_client_samples):
                if tenant_id not in active_tenant_ids:
                    _hub_client_samples.pop(tenant_id, None)
                    _hub_client_baseline.pop(tenant_id, None)
            for tenant_id in list(_aruba_clients):
                if tenant_id not in active_tenant_ids:
                    _aruba_clients.pop(tenant_id, None)
                    _aruba_client_hashes.pop(tenant_id, None)
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Aruba poller error: %s", exc)
        # Every 5 minutes.
        await asyncio.sleep(300)


async def send_notification(tenant_id: str, spoke_id: str, title: str, message: str, mode: str = "centralized") -> None:
    """Send notifications centrally or queue spoke-side delivery based on feature mode."""
    from .notifications import send_email, send_teams_webhook

    # No notification config: nothing to send.
    cfg = get_notification_config(tenant_id)
    if not cfg:
        return

    # Use the spoke's Teams and email modes, or the given mode if there is no spoke.
    spoke = store.get_spoke(tenant_id, spoke_id) if spoke_id else None
    teams_mode = spoke.processing_mode.resolve("teams_webhook") if spoke else mode
    email_mode = spoke.processing_mode.resolve("email") if spoke else mode

    # Config to send to the spoke, and which paths were used.
    distributed_cfg: dict[str, Any] = {"enabled": True}
    distributed = False
    centralized = False

    # Teams: add to the spoke's config, or send it from here.
    if cfg.get("teams_webhook"):
        if teams_mode == "distributed":
            distributed_cfg["teams_webhook"] = cfg["teams_webhook"]
            distributed_cfg["teams_webhook_url"] = cfg["teams_webhook"]
            distributed = True
        else:
            await send_teams_webhook(cfg["teams_webhook"], title, message)
            centralized = True

    # Email: add to the spoke's config, or send it from here.
    if cfg.get("smtp_host") and cfg.get("to_emails"):
        if email_mode == "distributed":
            distributed_cfg.update(
                {
                    "smtp_host": cfg["smtp_host"],
                    "smtp_port": cfg.get("smtp_port", 587),
                    "smtp_user": cfg.get("smtp_user", ""),
                    "smtp_pass": cfg.get("smtp_pass", ""),
                    "to_emails": cfg.get("to_emails", []),
                    "from_email": cfg.get("from_email", "hub@localhost"),
                }
            )
            distributed = True
        else:
            await send_email(
                cfg["smtp_host"],
                cfg.get("smtp_port", 587),
                cfg.get("smtp_user", ""),
                cfg.get("smtp_pass", ""),
                cfg.get("from_email", "hub@localhost"),
                cfg["to_emails"],
                title,
                message,
            )
            centralized = True

    # Queue the spoke's share and audit it.
    if distributed and spoke_id:
        store.enqueue_command(
            _cmd(
                spoke_id,
                tenant_id,
                "notification_push",
                {"title": title, "message": message, "config": distributed_cfg},
            )
        )
        store.append_audit(_audit(spoke_id, tenant_id, "notification", "distributed", "pending", title))
    # Audit what was sent from here.
    if centralized and spoke_id:
        store.append_audit(_audit(spoke_id, tenant_id, "notification", "centralized", "success", title))


async def _check_monitored_items(
    tenant_id: str,
    mode: str,
    site_names: set[str],
    alert_names: set[str],
    insight_names: set[str],
    client_macs: set[str],
    alert_ts: dict[str, str] | None = None,
    insight_ts: dict[str, str] | None = None,
    device_names: dict[str, str] | None = None,
) -> None:
    """Check each monitored item against current Central data; fire notification at 30 minutes absent.

    device_names maps lowercase device name/serial → lastSeenAt ISO string (or empty string).
    Used to resolve 'gateway' type monitored items.
    """
    # No monitored items: nothing to check.
    cfg = store.get_tenant_central_sites_config(tenant_id)
    items: list[dict[str, Any]] = list(cfg.get("monitored_items") or [])
    if not items:
        return

    # Times past which an item turns yellow, turns red, and triggers a notification.
    now = time.time()
    WARN_SECS  = 15 * 60   # 15 minutes → yellow
    CRIT_SECS  = 30 * 60   # 30 minutes → red
    NOTIFY_SECS = 30 * 60  # notify once when crossing 30-minute threshold
    # Missing maps count as empty.
    alert_ts = alert_ts or {}
    insight_ts = insight_ts or {}
    device_names = device_names or {}
    changed = False

    # Check each item.
    for item in items:
        if not isinstance(item, dict):
            continue
        # Is the item in the matching data, and when did Central last see it?
        item_type = str(item.get("type") or "")
        identifier = str(item.get("identifier") or "").strip().lower()

        if item_type == "site":
            found = identifier in site_names
            central_ts = None
        elif item_type == "alert":
            found = identifier in alert_names
            central_ts = alert_ts.get(identifier)
        elif item_type == "insight":
            found = identifier in insight_names
            central_ts = insight_ts.get(identifier)
        elif item_type == "client":
            found = identifier in client_macs
            central_ts = None
        elif item_type == "gateway":
            found = identifier in device_names
            central_ts = device_names.get(identifier) or None
        else:
            continue

        # Found: reset to OK.
        if found:
            item["consecutive_failures"] = 0
            item["last_seen"] = now
            item["missing_since"] = None
            item["status"] = "ok"
            # Store the actual Central API timestamp when the alert/insight/device last fired/checked in.
            if central_ts:
                item["central_last_seen"] = str(central_ts)
                if not item.get("central_first_seen"):
                    item["central_first_seen"] = str(central_ts)
            # Migrate old items that have central_last_seen but no central_first_seen.
            elif item.get("central_last_seen") and not item.get("central_first_seen"):
                item["central_first_seen"] = item["central_last_seen"]
            changed = True
        else:
            # Record when the item first went missing so we can show time-based status.
            if not item.get("missing_since"):
                item["missing_since"] = now
            missing_for = now - float(item["missing_since"])

            # Count the failure.
            prev = int(item.get("consecutive_failures") or 0)
            item["consecutive_failures"] = prev + 1

            # Status from how long it has been missing.
            if missing_for >= CRIT_SECS:
                item["status"] = "missing"   # red
            elif missing_for >= WARN_SECS:
                item["status"] = "warning"   # yellow
            else:
                item["status"] = "ok"        # grace period — don't alarm yet
            changed = True

            # Notify once when the item first crosses the 30-minute threshold.
            last_notified = float(item.get("last_notified") or 0)
            # Notify once when it passes 30 minutes, at most every 30 minutes; failures are logged.
            if missing_for >= NOTIFY_SECS and (now - last_notified) > NOTIFY_SECS:
                item["last_notified"] = now
                item_name = item.get("name") or identifier
                title = f"⚠️ Monitored {item_type.title()} No Longer Reporting"
                message = (
                    f'The monitored {item_type} "{item_name}" has not been seen in Aruba Central '
                    f"for 30+ minutes."
                )
                try:
                    await send_notification(tenant_id, "", title, message, mode)
                except Exception as exc:
                    logger.warning("Monitored item notification failed for tenant %s: %s", tenant_id, exc)

    # Save the updated items.
    if changed:
        cfg["monitored_items"] = items
        store.set_tenant_central_sites_config(tenant_id, cfg)


async def maintenance_loop() -> None:
    """Purge expired commands and old audit entries every 5 minutes."""
    while True:
        # Every 5 minutes, purge expired commands, old audit entries, and tenants deleted 30+ days
        # ago.
        await asyncio.sleep(300)
        try:
            purged_cmds = store.purge_expired_commands()
            purged_audit = store.purge_old_audit()
            purged_tenants = store.purge_old_deleted_tenants(days=30)
            if purged_cmds or purged_audit:
                logger.info("Maintenance: purged %s expired commands, %s old audit entries", purged_cmds, purged_audit)
            if purged_tenants:
                logger.info("Maintenance: purged %s tenant(s) deleted >30 days ago: %s", len(purged_tenants), purged_tenants)
        # Log and keep going.
        except Exception as exc:
            logger.warning("Maintenance error: %s", exc)


async def acme_renewal_check() -> None:
    """Check ACME certificates on startup and every 24 hours thereafter."""
    settings = get_settings()
    from .acme import get_cert_info, renew_if_needed

    while True:
        try:
            # Renew the certificate if needed and tell the browsers.
            renewed = await renew_if_needed(Path(settings.data_dir))
            if renewed:
                cert_info = get_cert_info()
                logger.info("ACME certificate renewed; expires %s", cert_info.get("expires", ""))
                await ws_broadcast({"type": "cert_renewed", "expires": cert_info.get("expires", "")})
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("ACME renewal check error: %s", exc)
        # Every 24 hours.
        await asyncio.sleep(86400)


async def check_state_engine() -> None:
    """Evaluate spoke online/offline status and fire notifications on transitions."""
    # First check after 60 seconds; online state from the last pass.
    await asyncio.sleep(60)
    prev_online: dict[str, bool] = {}
    while True:
        try:
            # Check each approved spoke in each tenant.
            for tenant in store.list_tenants():
                for spoke in store.list_spokes(tenant.id):
                    if spoke.status != "approved":
                        continue
                    key = f"{tenant.id}:{spoke.id}"
                    # Use 300s to match the frontend isOnline() threshold.
                    online = bool(spoke.last_seen and (_now() - spoke.last_seen).total_seconds() < 300)
                    # Online to offline: notify.
                    was_online = prev_online.get(key)
                    if was_online is True and not online:
                        await send_notification(
                            tenant.id,
                            spoke.id,
                            f"🔴 Spoke Offline: {spoke.hostname}",
                            f"Spoke {spoke.hostname} has gone offline (no telemetry in 10 minutes).",
                        )
                    prev_online[key] = online
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.exception("Check state engine error: %s", exc)
        await asyncio.sleep(60)


async def central_browse_poller() -> None:
    """Refresh the Central browse cache for every tenant at the per-tenant configured interval.

    On the first iteration (no sleep) it loads disk cache into memory so the
    endpoint can serve instantly after a restart.  Subsequent iterations fetch
    fresh data from Central and persist to disk.

    The poll interval is configurable per tenant (central_browse_interval_minutes,
    default 5).  The poller sleeps for 1 minute between iterations and only
    refreshes tenants whose cache is older than their configured interval.
    """
    from .routers.aggregate import _refresh_central_browse, _central_browse_cache, _central_browse_cache_ts, _load_browse_disk_cache
    import time as _time

    first_run = True
    while True:
        try:
            # Each tenant.
            tenants = store.list_tenants()
            now = _time.time()
            for tenant in tenants:
                if first_run:
                    # Warm in-memory cache from disk — no network call
                    if tenant.id not in _central_browse_cache:
                        disk = _load_browse_disk_cache(tenant.id)
                        if disk:
                            _central_browse_cache[tenant.id] = disk
                            _central_browse_cache_ts[tenant.id] = disk.get("cached_at", 0)
                            logger.info("central_browse_poller: loaded disk cache for tenant %s", tenant.id)
                else:
                    # Refresh if the cache is older than the tenant's interval (at least 1 minute).
                    interval_secs = max(60, (tenant.central_browse_interval_minutes or 5) * 60)
                    last_ts = _central_browse_cache_ts.get(tenant.id, 0)
                    if now - last_ts >= interval_secs:
                        try:
                            await _refresh_central_browse(tenant.id)
                            logger.debug("central_browse_poller: refreshed tenant %s (interval=%dm)", tenant.id, tenant.central_browse_interval_minutes)
                        except Exception as exc:
                            logger.warning("central_browse_poller: refresh failed for %s: %s", tenant.id, exc)
        # Let shutdown cancel the loop.
        except asyncio.CancelledError:
            raise
        # Log and keep going.
        except Exception as exc:
            logger.warning("central_browse_poller error: %s", exc)

        first_run = False
        await asyncio.sleep(60)  # Check every minute; actual refresh depends on per-tenant interval
