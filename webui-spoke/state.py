"""Shared mutable module-level state for the webui-spoke, consolidated out of server.py."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from fastapi import WebSocket

# ── Settings cache ────────────────────────────────────────────────────────────
_settings_cache: dict[str, Any] = {}
_settings_cache_time: float = 0.0

# ── State snapshot cache (JSON file, no DB) ──────────────────────────────────
_state_cache_last_save: float = 0.0

# WS delta: skip proxmox broadcast when payload hasn't changed
_last_proxmox_hash: str = ""

# ── Aruba Central state ───────────────────────────────────────────────────────
# {wsite: {check_id: {status, count, ts, check_name, check_type}}}
central_status: dict[str, dict[str, Any]] = {}
central_wireless_clients: dict[str, int] = {}   # wsite → client count from Central API
central_history: list[dict[str, Any]] = []   # in-memory 24-h window
central_auth_error: str | None = None          # last auth/token failure message
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

# Populated during each Central poll cycle from alert objects.
hardware_alert_devices: dict[str, dict[str, list[str]]] = {}
# In hub-connected (centralized) mode the hub computes hardware_alerts and pushes the
# full pre-built list (id/name/device_type/total/sites) to the spoke.  We cache it
# here so _hw_alerts_payload() can return it when local settings["hardware_checks"] is
# empty (i.e. the spoke has no locally-configured checks).
_hub_fed_hardware_alerts: list[dict] = []

# ── Proxmox WebSocket relay ───────────────────────────────────────────────────
proxmox_ws_connection: WebSocket | None = None
proxmox_ws_hostname: str | None = None
proxmox_ws_disconnect_task: asyncio.Task[Any] | None = None

# ── Relay registration / WS ───────────────────────────────────────────────────
# Recomputed at server load from bool(relay_state["enabled"]).
relay_registration_refresh_needed: bool = False
_relay_ws_send_json: Callable[[dict[str, Any]], Awaitable[None]] | None = None
_relay_ws_spoke_id: str | None = None
_repo_ver: str | None = None
_proxmox_reseed_in_progress = False

# Hub-synced monitored items — fetched each relay cycle, cached here
_hub_monitored_items: dict[str, Any] = {"items": [], "has_sites": False, "assigned_sites": []}

# Previous usb_state vmid→prov_status snapshot for transition detection
_prev_usb_by_vmid: dict[str, str] = {}
# Cooldown: earliest time a new auto-delete may be queued (updated after each
# confirmed deletion so the fleet has time to stabilise before the next one).
_delete_gate_cooldown_until: float = 0.0
# Rolling resource samples for 1-hour average CPU/memory threshold checks.
# Each entry is (unix_timestamp, value_percent).  Pruned to the last hour on each update.
_cpu_samples: list[tuple[float, float]] = []
_mem_samples: list[tuple[float, float]] = []
_resource_samples_started: float = 0.0  # epoch when first sample was recorded

# ── Proxmox VM simulation tags ────────────────────────────────────────────────
# Tracks which sim tags we last applied per (agent_hostname, vmid) to avoid
# redundant API calls.  Keyed this way because VMIDs can collide across nodes.
_vm_applied_sim_tags: dict[tuple[str, int], frozenset[str]] = {}

_resource_cache_last_saved: float = 0.0

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
last_schedule_trigger: str | None = None
_hub_repo_sync_task: asyncio.Task[Any] | None = None  # dedup guard for fire-and-forget repo_sync
