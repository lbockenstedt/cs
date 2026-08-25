"""Shared client-row builder for the two Clients views.

``control_plane._cs_telemetry_relay_loop`` (hub telemetry) and
``local_ui_routes.aggregate_clients`` (local dashboard) both built the
identical ~75-line client row — each carrying a "mirrors the other path"
comment. This module is the single source: both callers now call
:func:`build_client_rows` and add only their own envelope fields
(``payload["clients"]`` vs the per-row ``spoke_*`` keys).

Row semantics (unchanged from both originals):

* ``online`` — last_seen within 300 s.
* Sim-ID / Site / PHY resolved server-side from the hostname's bucket profile
  (``sim_config.effective_client_fields``) — the client's own report can be
  stale ("sl" from the old character-position hashing) or incomplete.
* Tier join (client → VM → USB dongle): a client whose Proxmox VM has a dongle
  assigned is T2; when the host/agent is offline (vmid unresolvable) the row
  falls back to the last-known persisted tier/has_usb so it doesn't drop to T1.
* ``tier_updates`` collects the authoritative tier/has_usb for clients whose VM
  is currently reporting; the CALLER awaits
  ``registry.record_tiers_batch(tier_updates)`` (async, change-gated).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sim_config

logger = logging.getLogger("CSClientRows")

# configs/ lives at <repo>/configs (this file is <repo>/lm-spoke/src/…) —
# matches control_plane._CONFIGS_DIR / local_ui_routes._CONFIGS_DIR.
_CONFIGS_DIR = Path(__file__).resolve().parent.parent.parent / "configs"


def _t1_exclude_hosts(spoke) -> list:
    """The operator's ``t1_exclude_hosts`` list (hub_config): pxmx servers whose
    T1 PCI card is NOT passed through — their clients run as USB/T2 and must
    never be classified/deployed as T1. Returns lowercased hostname/prefix tokens
    (empty on any error)."""
    try:
        ls = getattr(spoke, "local_store", None)
        hc = ((ls.get_hub_config() or {}).get("hub_config") or {}) if ls else {}
        return [str(x).strip().lower() for x in (hc.get("t1_exclude_hosts") or [])
                if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []


def _host_t1_excluded(host: str, patterns: list) -> bool:
    """True when a pxmx server ``host`` matches a ``t1_exclude_hosts`` entry —
    exact hostname OR prefix (the Setup field accepts either, e.g. ``sim-svr-05``
    or a bare prefix that covers a numbered range)."""
    if not host or not patterns:
        return False
    h = str(host).strip().lower()
    return any(h == p or h.startswith(p) for p in patterns)


def _t3_exclude_hosts(spoke) -> list:
    """Same as ``_t1_exclude_hosts`` but for ``t3_exclude_hosts``."""
    try:
        ls = getattr(spoke, "local_store", None)
        hc = ((ls.get_hub_config() or {}).get("hub_config") or {}) if ls else {}
        return [str(x).strip().lower() for x in (hc.get("t3_exclude_hosts") or [])
                if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []


def build_client_rows(spoke, now: float | None = None
                      ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Build the Clients-view rows for *spoke* (a ``CSSpoke``).

    Returns ``(rows, tier_updates)``. Never raises for a missing/degraded
    deploy or config load — degrades to reported values, like both originals.
    The caller is responsible for persisting ``tier_updates`` via
    ``await spoke.registry.record_tiers_batch(tier_updates)``.
    """
    if now is None:
        now = time.time()
    registry = getattr(spoke, "registry", None)
    if registry is None:
        return [], {}

    deploy = getattr(spoke, "deploy", None)
    if deploy is not None:
        usb_vmids, name_to_vmid = deploy.usb_vmid_index()
        tier_index = deploy.vm_tier_index()
        health_index = deploy.vm_health_index()
        name_to_host = deploy.name_to_host()
    else:
        usb_vmids, name_to_vmid, tier_index, health_index = set(), {}, {}, {}
        name_to_host = {}

    # Per-host T1 opt-out (hub mode): pxmx servers the operator listed do NOT
    # PCI-pass their T1 card, so their clients are USB/T2 and must never be
    # classified/deployed as T1. Only explicitly-excluded hosts are touched.
    excluded_t1_hosts = _t1_exclude_hosts(spoke)
    excluded_t3_hosts = _t3_exclude_hosts(spoke)

    # Load the sim configs ONCE per call (mtime-cached) so each client's
    # authoritative Site/PHY/Sim-ID can be resolved from its hostname.
    try:
        _sim_conf, _user_conf = sim_config.load_configs(_CONFIGS_DIR)
    except Exception as _e:  # noqa: BLE001 — degrade to reported values
        _sim_conf = _user_conf = None
        logger.debug("build_client_rows: config load failed: %s", _e)

    rows: List[Dict[str, Any]] = []
    tier_updates: Dict[str, Dict[str, Any]] = {}
    for hostname, c in registry.get_all().items():
        last_seen = c.get("last_seen")
        eff_sim_id, eff_cfg = sim_config.effective_client_fields(
            hostname, _sim_conf, _user_conf,
            c.get("simulation_id") or "", c.get("config"))
        if deploy is not None:
            vmid, has_usb = deploy.client_has_usb(
                hostname, c, usb_vmids, name_to_vmid)
        else:
            vmid, has_usb = None, False
        tier = tier_index.get(str(vmid)) if vmid else None
        health_state = health_index.get(str(vmid)) if vmid else None
        if vmid and tier:
            tier_updates[hostname] = {"tier": tier, "has_usb": has_usb}
        tier_stale = False
        tier_as_of: Any = None
        if vmid is None:
            # Host/agent offline or VM aged out of proxmox_states: the live
            # join can't classify it. Fall back to the last-known authoritative
            # tier/has_usb persisted while it WAS reporting, so
            # csClassifyClient (which prefers c.tier) keeps it T2 instead of
            # dropping to T1. Stamp tier_stale so the UI/QuotaEngine can tell an
            # ASSUMED (persisted-fallback) tier from a live-resolved one.
            _fb_tier = c.get("tier")
            if tier is None and _fb_tier:
                tier_stale = True
                tier_as_of = c.get("tier_as_of") or c.get("last_seen")
            tier = tier or _fb_tier
            has_usb = has_usb or bool(c.get("last_known_has_usb"))
        # Per-host T1 opt-out: if this client lives on an excluded pxmx server and
        # would otherwise classify as T1 (no USB dongle, no T2/T3 signal — the
        # exact case csClassifyClient renders as T1), force it to T2 so neither
        # the UI nor the quota engine ever treats/deploys it as T1. Persisted via
        # tier_updates so the registry (engine's source) agrees.
        if excluded_t1_hosts and tier not in ("t2", "t3") and not has_usb:
            _pmx_host = name_to_host.get(str(hostname).strip().lower())
            if _host_t1_excluded(_pmx_host, excluded_t1_hosts):
                tier = "t2"
                tier_updates[hostname] = {"tier": "t2", "has_usb": has_usb}
        # Per-host T3 opt-out: mirrors T1 above. Unlike T1 (the ambiguous-default
        # fallback), T3 is explicitly resolved via vm_tier_index/persisted tier, so
        # guard directly on tier == "t3" rather than "not yet classified".
        if excluded_t3_hosts and tier == "t3":
            _pmx_host = name_to_host.get(str(hostname).strip().lower())
            if _host_t1_excluded(_pmx_host, excluded_t3_hosts):
                tier = "t2"
                tier_updates[hostname] = {"tier": "t2", "has_usb": has_usb}
        rows.append({
            "hostname": hostname, "id": hostname,
            "platform": c.get("platform") or "—",
            "hw_type": c.get("platform") or "",
            "online": bool(last_seen and (now - last_seen) < 300),
            "connected_ssid": c.get("connected_ssid") or "—",
            # Sim-network connectivity (relayed to the hub so a T2 client that
            # never got an IP / never associated can be detected: the heartbeat
            # rides a separate backend network, so online≠has-sim-IP).
            "ip": c.get("ip") or "",
            "gateway_reachable": bool(c.get("gateway_reachable")),
            # Current DNS self-throttle rate (failures/min the AIMD ratchet settled
            # on for this dongle). 0 = not throttled / sidelined. Visibility only —
            # surfaced as the client-list badge.
            "dns_ceiling": c.get("dns_ceiling") or 0,
            "simulation_id": eff_sim_id,
            "active_simulations": c.get("active_simulations") or [],
            "last_seen": last_seen if last_seen is not None else "—",
            "error_count": len(c.get("recent_errors") or []),
            "recent_errors": c.get("recent_errors") or [],
            "vmid": vmid,
            "has_usb": has_usb,
            # Self-reported physical adapter inventory (name/mac/media_type/
            # is_default_route per interface). [] until the client's first
            # heartbeat with this field lands. See sim_quota_engine._media_ok.
            "adapters": c.get("adapters") or [],
            # Passive SSID sweep the client ran this beacon: the WiFi networks it
            # currently sees + the count. Relayed so the hub can distinguish
            # "associated but no IP" (sees SSIDs) from a dead radio (sees nothing
            # → faulty card) rather than treating both as "never connected".
            "visible_ssids": c.get("visible_ssids") or [],
            "visible_ssid_count": c.get("visible_ssid_count") or 0,
            # Authoritative tier (t1/t2/t3) from the agent's per-VM passthrough
            # classification; csClassifyClient prefers this over has_usb.
            "tier": tier,
            # In-guest dongle health (agent QGA probe): healthy / no_driver /
            # no_assoc / no_gateway / not_visible. None = no probe data. Surfaced
            # as a small badge on the Clients row so a dongle that's USB-present
            # but has no driver / no gateway is flagged (not just "offline").
            "health": health_state,
            # True when `tier` came from the persisted last-known fallback (host
            # unresolvable) rather than a live join — an ASSUMED tier, not live.
            "tier_stale": tier_stale,
            "tier_as_of": tier_as_of,
            # Carry the persisted per-client sim overrides + config up so the
            # WebUI's per-sim override buttons reflect what's SET (not just
            # what's running) and STAY across refreshes.
            "config": eff_cfg,
            "overrides": c.get("overrides") or {},
        })
    return rows, tier_updates
