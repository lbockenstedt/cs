"""Sim-Quota config foundation (Chunk 1) — schema, validation, resolution.

A sim-quota links a monitored Aruba Central alert/insight to a simulation
flag + a run policy (N runners in a site) so the SimQuotaEngine (Chunk 2) can
auto-generate per-client overrides that keep N online clients running the sim
— producing the network condition Aruba Central reports as the alert. The
INVERTED-semantics poller (``central_poller``) is HEALTHY when the error IS
present; the quota engine is what keeps it reliably present.

This module is intentionally engine-free: declaring a quota here only stores
config under ``central_sites_config["sim_quotas"]``; nothing auto-runs yet.

Data sourcing (per the feature design):
  * Sims are pulled from ``simulation.conf`` (the bucket flags that are runnable
    PRIMITIVES), enriched with per-sim metadata (``SIM_META``).
  * Sites are pulled from ``simulation.conf`` ``wsite`` values ∪ Central
    ``site_mappings``.
  * The alert→sim linkage is a TENANT user action (Config → Sim Quotas). The
    global catalog (Setup → Simulations) supplies per-sim metadata defaults +
    suggested linkage (``SUGGESTED_ALERT_SIM``) the tenant UI pre-fills; the
    tenant can change the sim per-quota. Hardware alerts (AP_DOWN, ...) have no
    sim and are monitoring-only.

Mirrored in ``lm/core/src/simulations/sim_quota.py`` (vendored twin) so the hub
and spoke agree on the schema. Keep the two in sync.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("CSSimQuota")

# ── Schema ────────────────────────────────────────────────────────────────
# A sim-quota record, stored as a list under central_sites_config["sim_quotas"].
# Backward-compatible: an absent "sim_quotas" key = no quotas (today's behavior).
SIM_QUOTA_KEYS = ("alert_id", "alert_type", "sim_id", "count", "site",
                  "multi_capable", "rehome", "enabled", "learning", "tier",
                  # Device quota kind (T3 IoT fleet): kind=device + a catalog
                  # device_id, count of that device profile, at a site. No sim/alert.
                  "kind", "device_id",
                  # Consumer-row (Adaptive, not Learning) knob floor — see the hub
                  # twin's comment (_learned_knob_floor / _knob_overrides_for_tenant,
                  # both hub-only; this spoke only carries the schema fields through).
                  "inherit_learned_knobs", "knob_overrides",
                  # Consumer-row count floor — see the hub twin's adaptive_step /
                  # apply_adaptive_targets (hub-only; schema field only here).
                  "inherit_learned_count")
# Per-quota client-tier policy. "best" (default) = prefer T1 (dedicated PCI —
# highest reliability), fall back to T2 (USB dongle). "t1"/"t2" = that tier ONLY
# (underfill rather than degrade a high-reliability sim onto an unreliable dongle).
QUOTA_TIERS = ("best", "t1", "t2")
ALERT_TYPES = ("alert", "insight")

# Source prefix: Central and Mist are separate products. A quota row's
# ``alert_id`` carries a ``Central:`` / ``Mist:`` prefix (applied in the hub's
# sim-quota catalog/picker) so a Central ``dns_fail`` and a Mist ``dns_fail``
# are distinct dedup/adaptive keys. The prefix is the ONLY seam between the two
# products; the bare id is what the dashboard Checks view / poller status keys
# by. ``parse_alert_source`` splits it back out (defaults to central for legacy
# / bare / unknown ids). Mirrored in lm/core/src/simulations/sim_quota.py.
SOURCE_PREFIXES = {"central": "Central", "mist": "Mist"}
_PREFIX_TO_SOURCE = {"central": "central", "mist": "mist"}


def parse_alert_source(alert_id: str) -> Tuple[str, str]:
    """Split a (possibly prefixed) ``alert_id`` into ``(source, bare_id)``.

    ``"Central:DNS Fail"`` → ``("central", "DNS Fail")``;
    ``"Mist:ap_offline"`` → ``("mist", "ap_offline")``;
    ``"DNS Fail"`` (legacy / untethered-display) → ``("central", "DNS Fail")``.
    An unknown/empty prefix falls back to ``central`` (bare). Returns the source
    as the canonical lowercase key (``"central"``/``"mist"``)."""
    aid = str(alert_id or "").strip()
    if ":" in aid:
        prefix, _, rest = aid.partition(":")
        src = _PREFIX_TO_SOURCE.get(prefix.strip().lower())
        if src and rest.strip():
            return src, rest.strip()
    return "central", aid


def prefixed_alert_id(source: str, bare_id: str) -> str:
    """Render a bare alert id with its product prefix:
    ``prefixed_alert_id("mist", "ap_offline")`` → ``"Mist:ap_offline"``.
    An unknown/empty source defaults to Central. A bare_id that is ALREADY
    prefixed is returned unchanged (idempotent)."""
    src = str(source or "central").strip().lower()
    if src not in SOURCE_PREFIXES:
        src = "central"
    bid = str(bare_id or "").strip()
    cur_src, cur_bare = parse_alert_source(bid)
    if cur_src != "central" or bid != cur_bare:
        return bid
    return f"{SOURCE_PREFIXES[src]}:{bid}" if bid else bid

# Per-sim metadata defaults. category: "failure" sims produce a network condition
# Aruba reports as an alert/insight; "traffic" sims are baseline load generators
# (the default bucket state — the abundant generic pool the quota draws from).
# multi_capable: True = may pack onto a client already running other sims;
# False (exclusive) = the engine assigns it only to a client not already running
# another exclusive quota sim. Defaults are overrideable per-quota in the tenant
# Config → Sim Quotas subtab.
SIM_META: Dict[str, Dict[str, object]] = {
    "dns_fail":    {"category": "failure", "multi_capable": False},
    "dns_latency": {"category": "failure", "multi_capable": False},
    "dhcp_fail":   {"category": "failure", "multi_capable": False},
    "assoc_fail":  {"category": "failure", "multi_capable": False},
    "auth_fail":   {"category": "failure", "multi_capable": False},
    "ssidpw_fail": {"category": "failure", "multi_capable": False},
    "mac_auth_fail": {"category": "failure", "multi_capable": False},
    "port_flap":   {"category": "failure", "multi_capable": False},
    "ping_test":   {"category": "traffic", "multi_capable": True},
    "download":    {"category": "traffic", "multi_capable": True},
    "www_traffic": {"category": "traffic", "multi_capable": True},
    "iperf":       {"category": "traffic", "multi_capable": True},
}

# Suggested alert/insight → sim linkage (global defaults the tenant UI
# pre-fills; the tenant can change the sim per-quota). Hardware alerts
# (AP_DOWN, SWITCH_DOWN, GATEWAY_DOWN, ...) are intentionally absent — they are
# not produced by sim clients, so they get monitoring only, never a quota.
SUGGESTED_ALERT_SIM: Dict[str, str] = {
    "CLIENT_DHCP_FAILURE": "dhcp_fail",
    "CLIENT_ASSOCIATION_FAILURE": "assoc_fail",
    "CLIENT_DISCONNECTED": "assoc_fail",
    "WIRELESS_CLIENT_ROAM": "assoc_fail",
    "DHCP_POOL_EXHAUSTED": "dhcp_fail",
    # DNS failure alerts → dns_fail. Aruba classic's KNOWN_CLASSIC_ALERT_TYPES
    # has no CLIENT_DNS_FAILURE; this is a forward-looking suggestion for
    # new_central / custom checks a tenant may surface.
    "CLIENT_DNS_FAILURE": "dns_fail",
}

# ── Tunable intensity knobs per sim (config-value learner) ────────────────
# The knob-floor learner (hub ``_knob_step``) ratchets these ``[simulation]``-
# section values DOWN one at a time to discover the minimum that still fires the
# sim's alert. Each entry: ``key`` (the simulation.conf ``[simulation]`` name the
# client reads), ``min``/``max`` sweep bounds, ``step``, and ``start`` (the
# known-firing high end the learner begins at). Only sims listed here can be
# knob-learned; add a sim by declaring its 1–4 numeric knobs. The client reads
# these unchanged (e.g. ``dns_fail.sh`` already reads ``dns_fail_rate`` /
# ``dns_fail_duration`` and clamps rate ≥200). Byte-identical to the hub twin.
SIM_KNOBS: Dict[str, List[Dict[str, int]]] = {
    "dns_fail": [
        {"key": "dns_fail_rate",     "min": 200, "max": 3000, "step": 200, "start": 3000},
        {"key": "dns_fail_duration", "min": 120, "max": 600,  "step": 60,  "start": 600},
    ],
    "dns_latency": [
        {"key": "dns_latency_rate",     "min": 200, "max": 3000, "step": 200, "start": 3000},
        {"key": "dns_latency_duration", "min": 120, "max": 600,  "step": 60,  "start": 600},
    ],
}


def knobs_for_sim(sim_id: str) -> List[Dict[str, int]]:
    """The ordered tunable knob specs for a sim (empty list if it has none)."""
    return [dict(k) for k in SIM_KNOBS.get(str(sim_id or "").strip(), [])]


KNOB_SETTLE_S = 1800.0  # ≥30 min — Central alert latency floor (see adaptive_step)


def knob_step(st: Dict[str, Any], knobs: List[Dict[str, int]], firing,
              now: float, settle: float = KNOB_SETTLE_S) -> Dict[str, Any]:
    """Advance one tick of the coordinate-descent floor search over ``knobs``
    (``SIM_KNOBS[sim]``). Pure — returns a NEW state dict so the caller can diff
    before/after. Shared by the hub controller and the unit test.

    One knob moves per settle window. Ratchet the ACTIVE knob DOWN while
    ``firing`` is True (probe lower); when a down-step loses the alert
    (``firing`` False) step back UP one and record that recovered value as the
    knob's floor, then advance to the next knob; hitting ``min`` while still
    firing floors it at ``min`` and advances. ``firing`` None → hold (never move
    blind). Once every knob is floored it keeps cycling — the same up/down logic
    re-seeks the floor as conditions drift, and a floored knob that loses the
    alert simply ramps back UP to recover.

    State shape: ``{values:{key:int}, floors:{key:int|None}, active:int,
    mode:str, last_change:float}``, keyed per quota by the caller."""
    if not knobs:
        return dict(st)
    st = dict(st)
    values = dict(st.get("values") or {})
    floors = dict(st.get("floors") or {})
    # Cold start: seed each knob at its known-firing high end and arm the settle
    # clock so even the first move waits a full window (let Central confirm firing
    # at the start level).
    if not values:
        for kn in knobs:
            values[kn["key"]] = int(kn.get("start", kn.get("max", kn["min"])))
            floors.setdefault(kn["key"], None)
        return {"values": values, "floors": floors, "active": 0,
                "mode": "learning", "last_change": now}
    active = int(st.get("active") or 0) % len(knobs)
    last = float(st.get("last_change") or 0)
    _all_floored = all(floors.get(kn["key"]) is not None for kn in knobs)
    if firing is None or (now - last) < settle:  # hold
        st.update(values=values, floors=floors, active=active,
                  mode=("stable" if _all_floored else "learning"))
        return st
    kn = knobs[active]
    key = kn["key"]
    mn, mx, step = int(kn["min"]), int(kn["max"]), max(1, int(kn["step"]))
    cur = int(values.get(key, kn.get("start", mx)))
    if firing is True:
        nv = cur - step
        if nv < mn:                     # min still fires → that's the floor
            values[key] = mn
            floors[key] = mn
            active = (active + 1) % len(knobs)
        else:                           # keep probing lower on this knob
            values[key] = nv
    else:                               # firing False → this value lost the alert
        rv = min(mx, cur + step)        # step back up to the last firing level
        values[key] = rv
        prev = floors.get(key)
        floors[key] = rv if prev is None else min(int(prev), rv)
        active = (active + 1) % len(knobs)
    st.update(values=values, floors=floors, active=active, last_change=now,
              mode=("stable" if all(floors.get(k2["key"]) is not None
                                    for k2 in knobs) else "learning"))
    return st


# ── Coercion helpers ──────────────────────────────────────────────────────
def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_int(v: Any, default: int = 1) -> int:
    try:
        n = int(str(v).strip())
        return n if n >= 1 else default
    except Exception:
        return default


def normalize_quota(raw: Any) -> Dict[str, Any]:
    """Coerce a raw quota dict to the canonical shape; drop unknown keys.
    ``multi_capable`` defaults from ``SIM_META[sim_id]`` when absent so the
    tenant inherits the per-sim default unless they explicitly override it.

    A PRESENCE quota (``sim_id`` empty, "Clients Associated") homes N clients
    to a site and runs NO sim — it only guarantees N clients are associated to
    the site (re-homing ``wsite`` if ``rehome``). Presence quotas are ALWAYS
    multi-capable (they don't consume the client for sim purposes; other
    stackable sims may pack onto a presence-homed client)."""
    if not isinstance(raw, dict):
        return {}
    sim_id = str(raw.get("sim_id") or "").strip()
    meta = SIM_META.get(sim_id, {})
    alert_type = str(raw.get("alert_type") or "alert").strip().lower()
    if alert_type not in ALERT_TYPES:
        alert_type = "alert"
    is_presence = not sim_id
    # Device quota (T3 IoT fleet): a catalog ``device_id`` + count + site, no
    # sim/alert. ``kind`` is explicit when the UI sends it, else derived:
    # device_id → device, sim_id → sim, else presence. See the T3 device-quota
    # design ([[t3-iot-device-quota-kind-design]]).
    device_id = str(raw.get("device_id") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ("sim", "presence", "device"):
        kind = "device" if device_id else ("sim" if sim_id else "presence")
    q = {
        "kind": kind,
        "device_id": device_id,
        "alert_id": str(raw.get("alert_id") or "").strip(),
        "alert_type": alert_type,
        "sim_id": sim_id,
        "count": _as_int(raw.get("count"), 1),
        "site": str(raw.get("site") or "").strip(),
        # Presence always packs (a homed-but-sim-less client is still a free
        # runner other sims may stack onto); ignore an operator override here.
        "multi_capable": True if is_presence
        else _as_bool(raw.get("multi_capable"), bool(meta.get("multi_capable", False))),
        "rehome": _as_bool(raw.get("rehome"), False),
        "enabled": _as_bool(raw.get("enabled"), False),
        # `learning` ON = this row is the "learning lab": full thermostat (ramp
        # up AND down continuously to re-evaluate the floor) AND tunes the sim's
        # [simulation] intensity knobs (SIM_KNOBS[sim_id]) down to the floor that
        # still fires, then publishes the learned count + knobs so production
        # consumers go straight to learned + 20%. OFF (default) = a consumer
        # (Adaptive): up-only, seeds/lifts from the learned op + knobs, never
        # down-ratchets. The two are MUTUALLY EXCLUSIVE (both off = fixed count).
        # See design doc §9 / adaptive_step. Mirrored in the hub twin.
        "learning": _as_bool(raw.get("learning"), False),
        # Client-tier policy (see QUOTA_TIERS). "best" prefers T1 then T2.
        "tier": (lambda t: t if t in QUOTA_TIERS else "best")(
            str(raw.get("tier") or "best").strip().lower()),
        # Consumer-row knob floor — schema symmetry with the hub twin (the
        # computation itself is hub-only; see SIM_QUOTA_KEYS comment above).
        "inherit_learned_knobs": _as_bool(raw.get("inherit_learned_knobs"), True),
        "knob_overrides": ({str(k): v for k, v in raw["knob_overrides"].items()}
                           if isinstance(raw.get("knob_overrides"), dict) else {}),
        # Consumer-row count floor — schema symmetry with the hub twin.
        "inherit_learned_count": _as_bool(raw.get("inherit_learned_count"), True),
    }
    # Adaptive-controller fields (design doc §9) — carried through only when the
    # quota declares them, so a fixed-count quota stays exactly as before. The
    # hub-side controller reads min/max/step/settle/buffer and modulates `count`;
    # the spoke only consumes `count` but preserves these for schema symmetry.
    for k in ("min", "max", "step", "settle", "buffer"):
        if raw.get(k) is not None:
            q[k] = raw.get(k)
    return q


def quota_dedup_key(q: Dict[str, Any]) -> str:
    """The dedup/identity key for a normalized quota.

    A sim quota is keyed by ``alert_type:alert_id:site`` (one sim per monitored
    alert per site). A presence quota (``sim_id`` empty — "Clients Associated")
    has no alert, so it's keyed by site alone — one presence count per site
    (last-wins), independent of the sim-quota namespace so a presence row never
    collides with an alert-driven row. The engine's ``_quota_key`` mirrors this.
    """
    if q.get("kind") == "device" or q.get("device_id"):
        # One quota per device profile per site (last-wins).
        return f"device:{q.get('device_id', '')}:{q.get('site', '')}"
    if not q.get("sim_id"):
        return f"presence::{q.get('site', '')}"
    return f"{q.get('alert_type', 'alert')}:{q.get('alert_id', '')}:{q.get('site', '')}"


def validate_sim_quotas(
    quotas: Any, available_sims: List[str] | None = None,
    available_devices: List[str] | None = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalize + validate a ``sim_quotas`` list.

    Returns ``(clean, errors)``. A SIM quota (``sim_id`` set) requires an
    ``alert_id`` and its ``sim_id`` must be in *available_sims* (when provided) —
    the engine can only run sims the tenant's simulation.conf offers. A PRESENCE
    quota (``sim_id`` empty — "Clients Associated") requires a ``site`` and NO
    ``alert_id`` (it homes clients, runs no sim, so there's nothing to monitor).
    Duplicate keys (``quota_dedup_key``) collapse last-wins. The clean list is
    normalized to the canonical shape.
    """
    clean: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen: Dict[str, Dict[str, Any]] = {}
    sim_set = set(available_sims or [])
    device_set = set(available_devices or [])
    for i, raw in enumerate(quotas or []):
        q = normalize_quota(raw)
        if q["kind"] == "device":
            # Device quota (T3 IoT fleet) — needs a site + a catalog device_id;
            # no sim/alert. Validate the device_id against the catalog when given.
            if not q["site"]:
                errors.append(f"quota #{i}: device quota requires a site — dropped")
                continue
            if not q["device_id"]:
                errors.append(f"quota #{i}: device quota requires a device_id — dropped")
                continue
            if device_set and q["device_id"] not in device_set:
                errors.append(f"quota #{i}: device_id '{q['device_id']}' "
                              f"not in the IoT catalog — dropped")
                continue
        elif not q["sim_id"]:
            # Presence quota — needs a site (it homes N clients there); no
            # alert_id (nothing to monitor — no sim runs).
            if not q["site"]:
                errors.append(f"quota #{i}: presence quota (Clients Associated) "
                              f"requires a site — dropped")
                continue
        else:
            if not q["alert_id"]:
                errors.append(f"quota #{i}: missing alert_id — dropped")
                continue
            if sim_set and q["sim_id"] not in sim_set:
                errors.append(
                    f"quota #{i} ({q['alert_id']}): sim_id '{q['sim_id']}' "
                    f"not in available sims — dropped")
                continue
        seen[quota_dedup_key(q)] = q
    clean = list(seen.values())
    return clean, errors


def guard_sim_quota_wipe(
    existing_quotas: Any, clean: List[Dict[str, Any]], body: Any,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Anti-blast safeguard: refuse to replace a non-empty ``sim_quotas`` list
    with an empty one unless the caller explicitly opts in via
    ``body["force_sim_quotas_clear"]``.

    A config save that takes a tenant from N>0 quotas to 0 is almost always
    unintentional — a stale ``simulation.conf`` (``available_sims`` no longer
    lists the quota sims, so ``validate_sim_quotas`` drops every row on the
    next save of *anything*), a UI render bug that serializes an empty editor,
    or a partial save that accidentally sends ``sim_quotas: []``. Each has, in
    the field, wiped a tenant's entire quota table on a single save. This guard
    makes that impossible without an explicit, unambiguous opt-in.

    Returns ``(quotas_to_persist, wipe_blocked)``. When blocked, the existing
    quotas are returned unchanged so the caller keeps them and reports the
    block; the caller must persist THESE, not the empty ``clean``. A deliberate
    "clear all" sets ``force_sim_quotas_clear`` (truthy) and is honored. A
    partial drop (some rows survive) is NOT blocked — only a full wipe is.
    Mirrored in the hub twin.
    """
    had = len(existing_quotas or [])
    if had and not (clean or []):
        force = body.get("force_sim_quotas_clear") if isinstance(body, dict) else None
        if str(force).strip().lower() in ("1", "true", "yes", "on"):
            return list(clean or []), False
        return list(existing_quotas or []), True
    return list(clean or []), False


def resolve_effective_quotas(
    tenant_quotas: Any, available_sims: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """The quotas the engine should run: validated + ``enabled`` only."""
    clean, _ = validate_sim_quotas(tenant_quotas, available_sims)
    return [q for q in clean if q["enabled"]]


# ── Catalog: sims + sites derived from simulation.conf ─────────────────────
def _bucket_sections(sim_conf) -> List[str]:
    out = []
    for sec in (sim_conf.sections() if sim_conf is not None else []):
        if sec.startswith("s") and sec[1:].isdigit():
            out.append(sec)
    return out


def available_sims(config_dir: Any) -> List[Dict[str, Any]]:
    """Sims the Sim-Quota UI may offer, derived from ``simulation.conf`` bucket
    sections (only flags that are runnable PRIMITIVES), enriched with
    ``SIM_META``. Sims a tenant actually uses in its buckets appear first;
    runnable sims not yet placed in any bucket follow (still offerable — the
    engine sets them via per-client override regardless of bucket default)."""
    try:
        from sim_primitives import SIM_FLAGS  # lazy: avoid import cycle
    except Exception:  # noqa: BLE001
        SIM_FLAGS = tuple(SIM_META.keys())
    flags = list(SIM_FLAGS)

    bucket_flags: List[str] = []
    seen = set()
    try:
        from sim_config import load_configs
        sim_conf, _ = load_configs(config_dir)
        for sec in _bucket_sections(sim_conf):
            for key in sim_conf.options(sec):
                if key in flags and key not in seen:
                    seen.add(key)
                    bucket_flags.append(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("available_sims: load_configs failed: %s", exc)

    ordered = bucket_flags + [f for f in flags if f not in seen]
    return [
        {"sim_id": f,
         "category": SIM_META.get(f, {}).get("category", "failure"),
         "multi_capable": bool(SIM_META.get(f, {}).get("multi_capable", False)),
         "has_knobs": f in SIM_KNOBS}
        for f in ordered
    ]


def available_sites(
    config_dir: Any, central_site_mappings: Dict[str, str] | None = None,
) -> List[str]:
    """Sites the Sim-Quota UI may offer =
    ``simulation.conf`` ``wsite`` values across buckets ∪ Central
    ``site_mappings`` keys+values. Sorted, de-duplicated."""
    sites: set[str] = set()
    try:
        from sim_config import load_configs
        sim_conf, _ = load_configs(config_dir)
        for sec in _bucket_sections(sim_conf):
            w = sim_conf.get(sec, "wsite", fallback="").strip()
            if w:
                sites.add(w)
    except Exception as exc:  # noqa: BLE001
        logger.warning("available_sites: load_configs failed: %s", exc)
    for k, v in (central_site_mappings or {}).items():
        if k:
            sites.add(str(k))
        if v:
            sites.add(str(v))
    return sorted(sites)


def sim_quota_catalog(
    config_dir: Any, central_site_mappings: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """The full catalog the Sim-Quota UI renders against:
    ``{sims, sites, suggested, meta}``. ``sims``/``sites`` are derived from the
    tenant's ``simulation.conf``; ``suggested`` is the global alert→sim map the
    tenant UI pre-fills; ``meta`` is the per-sim metadata."""
    return {
        "sims": available_sims(config_dir),
        "sites": available_sites(config_dir, central_site_mappings),
        "suggested": dict(SUGGESTED_ALERT_SIM),
        # `knobs` per sim = the [simulation] key names the lab tunes (so the UI can
        # label them on a Learning row); empty for sims with no declared knobs.
        "meta": {k: {**dict(v), "knobs": [kn["key"] for kn in SIM_KNOBS.get(k, [])]}
                 for k, v in SIM_META.items()},
    }