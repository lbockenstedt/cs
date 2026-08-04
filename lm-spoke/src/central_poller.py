"""Background Aruba Central poller for a standalone/hub-connected cs spoke.

Drives ``aruba.ArubaClient`` (vendored from ``solutions-hpe/webui-hub``'s
``app/aruba.py`` — see that file's docstring) against the sites configured in
``local_store.py``'s ``central_sites_config``, and assembles the result into
``spoke.central_status`` in the EXACT shape ``lm/WebUI/sim-views.js``'s
Simulations Checks/Hardware/Client-Count tabs already expect:

    {"status": {site: {check_id: {status, message}}},
     "hardware_alerts": [{id, name, device_type, total}],
     "client_count_status": {site: {current, hourly_avg, drop_pct, status, ...}}}

The client-count entry carries the smoothed current-hourly average + a 7-day
baseline + drop %% (monitoring a site for a sustained client-count DROP) — see
the ClientCountTracker class / the _CC_* constants.

This closes the gap flagged when the Simulations tab first landed (Central
integration didn't exist in lm-spoke at all) — real polling now runs, sourced
from THIS spoke's own local Central credentials instead of an LM hub tenant
config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from aruba import ArubaClient
from check_eval import count_for_check, normalize_counts

logger = logging.getLogger("CentralPoller")

_POLL_INTERVAL_S = 300  # 5 min — matches aruba.py's own cache TTLs

# Central refreshes several metrics on the 5-MINUTE WALL CLOCK (:00, :05, :10 …).
# Querying inside that window returns transitional / half-updated values that read
# as a FALSE POSITIVE — a check flips only because Central is mid-refresh, not
# because anything really changed. So never let a poll land within
# _BOUNDARY_GUARD_S of a 5-minute boundary; delay into the safe zone first.
_CENTRAL_UPDATE_PERIOD_S = 300
_BOUNDARY_GUARD_S = 60


def _boundary_guard_delay(now: float) -> float:
    """Seconds to wait so a poll does NOT fall within ``_BOUNDARY_GUARD_S`` of a
    5-minute wall-clock boundary. ``now`` is epoch seconds (UTC-aligned, so
    ``now % 300`` = seconds since the last 5-minute mark). 0 = already in the safe
    zone (60s..240s past the boundary)."""
    phase = now % _CENTRAL_UPDATE_PERIOD_S
    if phase < _BOUNDARY_GUARD_S:                                  # just AFTER a boundary
        return _BOUNDARY_GUARD_S - phase
    if phase > _CENTRAL_UPDATE_PERIOD_S - _BOUNDARY_GUARD_S:       # just BEFORE the next boundary
        return (_CENTRAL_UPDATE_PERIOD_S - phase) + _BOUNDARY_GUARD_S
    return 0.0

# Client-count baseline constants — ported verbatim from the source webui-spoke
# (server.py). The alarm baseline is a 7-DAY rolling average of hourly snapshots
# (NOT the 1h average), so a prolonged client drop stays flagged instead of the
# baseline sagging to match it. KEEP IN SYNC with lm's central_hub_poller.py
# ClientCountTracker (centralized mode uses that copy).
_CC_WINDOW = 3600
_CC_MIN_SAMPLES = 3
_CC_WARN_PCT = 20.0        # >20% below the hour average -> WARNING
_CC_ERROR_PCT = 50.0       # >50% below -> ERROR
_CC_1DAY_WINDOW = 86400
_CC_7DAY_WINDOW = 7 * 86400
_CC_30DAY_WINDOW = 30 * 86400   # long-run history retention (the 7-day is a subset)
_CC_SNAPSHOT_INTERVAL = 3600
# Severe sustained die-off: current hour < 20% of the rolling PEAK (max hourly-avg)
# over the window → ERROR. Gated on a meaningful peak so a quiet site can't error on noise.
_CC_MAX_FRACTION = 0.20
_CC_MAX_MIN_PEAK = 5
# Which colour each baseline breach produces. Separated from the rule so the
# mapping can be retuned without touching the logic.
_CC_DAILY_SEVERITY = "error"
_CC_WEEKLY_SEVERITY = "error"
_CC_MONTHLY_SEVERITY = "warning"
# ── trend rules ─────────────────────────────────────────────────────────────
# A steady-state metric is judged three ways, because each is blind to what the
# others catch:
#   FLOOR   — an absolute, CONFIGURED expected level. Never decays, so a site
#             that has been dark for weeks stays red instead of the baseline
#             quietly normalising to the outage. This is the only rule that
#             encodes INTENT ("this site should run 100 clients"); no statistic
#             can infer it from history.
#   PERIOD  — this day/week vs the one before. Catches a SLOW BLEED. A rate rule
#             over a few samples cannot: 20% lost over a week is ~0.6% per
#             5-hour window, so it never crosses any threshold worth setting
#             (measured: a 50% loss over 7d was NEVER flagged by the short rule).
#   RATE    — a short-window drop. Catches a FAST collapse, ~7h behind onset,
#             which the period rules would not surface until the next day rolls.
# Worst wins. The rolling PEAK is reported as a WATERMARK only -- anchoring a
# threshold to it pinned sites red for 26.5 days after a single 4h spike.
_CC_PERIOD_DROP_PCT = 20.0     # day-over-day / week-over-week drop -> ERROR
_CC_RATE_SAMPLES = 5           # short-window size, in polls
_CC_RATE_DROP_PCT = 5.0        # short-window drop -> WARNING
# False-yellow rate at this threshold depends entirely on the site's natural
# jitter (5-sample window, modelled on a healthy steady site):
#     jitter 2% -> 0.0% | 3% -> 2.9% | 5% -> 10.3% | 8% -> 22.1%
# Raise _CC_RATE_DROP_PCT to 10 if a fleet turns out noisier than ~5%.
_CC_KEYSEP = "\x1f"
_CC_SCOPE = "_"  # single-tenant spoke → one fixed scope key


def _cc_thresholds(central_config):
    """Per-tenant client-count CHECK thresholds, read from
    ``central_config['cc_thresholds']`` (Setup → Central API) with the module
    defaults as fallback and clamped to sane ranges. Keys: ``warn_pct`` /
    ``error_pct`` = amber/red when the count is that % below the recent hourly
    average; ``die_off_pct`` = red when the hourly average falls below that % of
    the rolling 7/30-day peak (0 disables the die-off rule); ``min_peak`` = the
    peak floor that arms the die-off rule. Returns resolved values with the
    die-off as a 0-1 fraction; ``error_pct`` is coerced up to ``warn_pct`` so red
    can never trip before amber. Mirror of the lm central_hub_poller copy."""
    t = (central_config or {}).get("cc_thresholds") or {}

    def _num(val, dflt, lo, hi):
        try:
            x = float(val)
        except (TypeError, ValueError):
            return dflt
        return max(lo, min(hi, x))

    warn = _num(t.get("warn_pct"), _CC_WARN_PCT, 0.0, 100.0)
    err = _num(t.get("error_pct"), _CC_ERROR_PCT, 0.0, 100.0)
    if err < warn:
        err = warn
    die = _num(t.get("die_off_pct"), _CC_MAX_FRACTION * 100.0, 0.0, 100.0) / 100.0
    peak = int(_num(t.get("min_peak"), _CC_MAX_MIN_PEAK, 1, 1_000_000))
    # Steady-state fractions, defaulting to the existing die_off_pct so an
    # already-tuned tenant keeps its number instead of being silently reset.
    # 0 disables that window.
    # Trend thresholds. period/rate are PERCENT drops, not fractions.
    period = _num(t.get("period_drop_pct"), _CC_PERIOD_DROP_PCT, 0.0, 100.0)
    rate = _num(t.get("rate_drop_pct"), _CC_RATE_DROP_PCT, 0.0, 100.0)
    rate_n = int(_num(t.get("rate_samples"), _CC_RATE_SAMPLES, 2, 100))
    daily = _num(t.get("daily_pct"), die * 100.0, 0.0, 100.0) / 100.0
    weekly = _num(t.get("weekly_pct"), die * 100.0, 0.0, 100.0) / 100.0
    monthly = _num(t.get("monthly_pct"), die * 100.0, 0.0, 100.0) / 100.0
    return {"warn_pct": warn, "error_pct": err, "die_off_frac": die,
            "min_peak": peak, "daily_frac": daily,
            "weekly_frac": weekly, "monthly_frac": monthly,
            "period_drop_pct": period, "rate_drop_pct": rate,
            "rate_samples": rate_n}


_CC_SEVERITY = {"error": 3, "warning": 2, "ok": 1}  # else (no_data/pending/…) -> 0


def _cc_period_drop(hist, now, window_s):
    """Drop % of THIS period's average vs the PREVIOUS period's.

    Returns None when either period lacks data, so a fresh site cannot alarm on
    an empty comparison. Drop only -- growth is not a fault.
    """
    cur = [v for ts, v in hist if ts >= now - window_s]
    prev = [v for ts, v in hist if now - 2 * window_s <= ts < now - window_s]
    if not cur or not prev:
        return None
    a = sum(prev) / len(prev)
    if a <= 0:
        return None
    return max(0.0, (a - (sum(cur) / len(cur))) / a * 100.0)


def _cc_rate_drop(samples, n):
    """Drop % of the last *n* samples' average vs the *n* before them."""
    if len(samples) < 2 * n:
        return None
    vals = [s[1] for s in samples]
    prev = sum(vals[-2 * n:-n]) / n
    if prev <= 0:
        return None
    return max(0.0, (prev - sum(vals[-n:]) / n) / prev * 100.0)


def _cc_site_floor(sites_cfg, *site_names):
    """Configured minimum client count for a site, from the EXISTING
    ``site_min_clients`` map that Central/Mist -> Sites -> Monitor already
    writes. Not a new config surface -- the operator has been setting this
    per site all along; the trend check simply had no idea it existed.

    Tries each name in turn because the map may be keyed by either the
    WIRELESS site or the Central/Mist site name, mirroring the lookup the
    Minimum Client Threshold check already does.

    Returns None when unset, zero or unparseable, which disables the floor rule
    for that site ONLY. Never inferred from history -- that is the peak problem.
    """
    try:
        floors = (sites_cfg or {}).get("site_min_clients") or {}
    except AttributeError:
        return None
    for name in site_names:
        if name in (None, ""):
            continue
        raw = floors.get(name) or floors.get(str(name))
        try:
            if raw not in (None, "") and float(raw) > 0:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return None

def _cc_worst(*statuses):
    """Worst (most severe) of a set of client-count statuses — for the overall
    site check when wired + wireless are tracked separately (error > warning >
    ok > no_data). So a wired-only or wireless-only die-off reddens the site even
    if the other half is healthy. All-empty → the first status (usually
    no_data). Mirror of the lm central_hub_poller copy."""
    worst, rank = None, -1
    for s in statuses:
        r = _CC_SEVERITY.get(s, 0)
        if r > rank:
            rank, worst = r, s
    return worst or "ok"


class ClientCountTracker:
    """Per-(scope, wsite) client-count baseline + drop detection, ported
    faithfully from the source webui-spoke (server.py ``_client_count_payload`` /
    ``_save_client_count_baseline`` / ``hourly_baseline_saver``).

    Monitoring a site means watching its client count for a sustained DROP: 1h of
    raw samples gives the smoothed "current hourly" average, and a 7-DAY history
    of hourly snapshots is the STABLE alarm baseline. ``drop_pct = (baseline -
    hourly_avg) / baseline`` and the site goes DEGRADED at >=25%. Because the
    baseline spans 7 days, a prolonged drop does NOT suppress the alarm. Both the
    last-hour baseline and the 7-day history persist to disk so a restart keeps
    the reference. See the identical copy in lm central_hub_poller.py."""

    def __init__(self, baseline_path: str, sevenday_path: str) -> None:
        self._baseline_path = baseline_path
        self._sevenday_path = sevenday_path
        self._samples: Dict[str, list] = {}
        self._hourly: Dict[str, list] = {}
        self._baseline: Dict[str, dict] = {}
        self._last_snapshot = time.time()  # wait one full hour before first write
        self._load()

    @staticmethod
    def _key(scope: str, wsite: str, kind: str = "") -> str:
        base = f"{scope}{_CC_KEYSEP}{wsite}"
        return f"{base}{_CC_KEYSEP}{kind}" if kind else base

    def _load(self) -> None:
        now = time.time()
        try:
            with open(self._baseline_path, encoding="utf-8") as f:
                self._baseline = json.load(f) or {}
            for key, saved in self._baseline.items():
                avg = round(saved.get("hourly_avg", 0))
                self._samples[key] = [
                    (now - (_CC_MIN_SAMPLES - i) * 60, avg) for i in range(_CC_MIN_SAMPLES)
                ]
        except Exception:  # noqa: BLE001
            self._baseline = {}
        try:
            with open(self._sevenday_path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            cutoff = now - _CC_30DAY_WINDOW
            self._hourly = {
                k: [(float(ts), float(v)) for ts, v in entries if float(ts) >= cutoff]
                for k, entries in raw.items()
            }
        except Exception:  # noqa: BLE001
            self._hourly = {}

    def record(self, scope: str, wsite: str, current: int, kind: str = "") -> None:
        now = time.time()
        key = self._key(scope, wsite, kind)
        samples = self._samples.setdefault(key, [])
        samples.append((now, int(current)))
        cutoff = now - _CC_WINDOW
        self._samples[key] = [s for s in samples if s[0] >= cutoff]

    def entry(self, scope: str, wsite: str, central_site: str, thresholds=None, kind: str = "") -> Dict[str, Any]:
        """Per-site client-count status (doubles as a dashboard CHECK). Tiered:
          - WITHIN-HOUR drop (current vs the last-hour average): WARNING / ERROR
            at ``warn_pct`` / ``error_pct`` below — catches sim-client die-off
            inside the hour.
          - SUSTAINED die-off: the current hour < ``die_off_frac`` of the 7-DAY or
            30-DAY rolling PEAK (max hourly-avg) → ERROR. Gated on a peak of at
            least ``min_peak`` so a quiet site can't false-trigger; die_off_frac=0
            disables it.
        ``thresholds`` (from _cc_thresholds → central_config) overrides the module
        defaults per tenant. The 7d/30d peaks are recorded regardless of status."""
        _t = thresholds or {}
        warn_pct = _t.get("warn_pct", _CC_WARN_PCT)
        error_pct = _t.get("error_pct", _CC_ERROR_PCT)
        die_off_frac = _t.get("die_off_frac", _CC_MAX_FRACTION)
        min_peak = _t.get("min_peak", _CC_MAX_MIN_PEAK)
        daily_frac = _t.get("daily_frac", die_off_frac)
        weekly_frac = _t.get("weekly_frac", die_off_frac)
        period_drop_pct = _t.get("period_drop_pct", _CC_PERIOD_DROP_PCT)
        rate_drop_thresh = _t.get("rate_drop_pct", _CC_RATE_DROP_PCT)
        rate_samples = int(_t.get("rate_samples", _CC_RATE_SAMPLES))
        floor = _t.get("floor")
        monthly_frac = _t.get("monthly_frac", die_off_frac)
        now = time.time()
        key = self._key(scope, wsite, kind)
        samples = self._samples.get(key, [])
        hist = self._hourly.get(key, [])
        vals_1d = [v for ts, v in hist if ts >= now - _CC_1DAY_WINDOW]
        vals_7d = [v for ts, v in hist if ts >= now - _CC_7DAY_WINDOW]
        vals_30d = [v for ts, v in hist if ts >= now - _CC_30DAY_WINDOW]
        if not samples:
            return {"site_name": central_site, "current": 0, "hourly_avg": 0,
                    "drop_pct": 0.0, "max_7day": round(max(vals_7d or [0]), 1),
                    "max_30day": round(max(vals_30d or [0]), 1),
                    "avg_1day": round(sum(vals_1d) / len(vals_1d), 1) if vals_1d else 0.0,
                    "avg_7day": round(sum(vals_7d) / len(vals_7d), 1) if vals_7d else 0.0,
                    "avg_30day": round(sum(vals_30d) / len(vals_30d), 1) if vals_30d else 0.0,
                    "status": "no_data", "ts": now}
        current = samples[-1][1]
        hourly_avg = sum(s[1] for s in samples) / len(samples)
        max_7day = round(max(vals_7d + [hourly_avg]), 1)
        max_30day = round(max(vals_30d + [hourly_avg]), 1)
        # Steady-state baselines. The old rule compared against the rolling PEAK,
        # which is a single best-ever hour and makes the check hair-trigger: any
        # site that was once busy stays "dying" forever. An AVERAGE is what a
        # steady-state metric should hold, so that is what we measure against.
        # Peaks are still reported, as context rather than as the threshold.
        avg_1day = round(sum(vals_1d) / len(vals_1d), 1) if vals_1d else 0.0
        avg_7day = round(sum(vals_7d) / len(vals_7d), 1) if vals_7d else 0.0
        avg_30day = round(sum(vals_30d) / len(vals_30d), 1) if vals_30d else 0.0
        # Trend inputs. Each returns None when it lacks the history to judge,
        # so a fresh site never alarms on an empty comparison.
        day_drop_pct = _cc_period_drop(hist, now, _CC_1DAY_WINDOW)
        week_drop_pct = _cc_period_drop(hist, now, _CC_7DAY_WINDOW)
        rate_drop_pct = _cc_rate_drop(samples, rate_samples)
        if len(samples) < _CC_MIN_SAMPLES:
            drop_pct, status = 0.0, "no_data"
        else:
            if hourly_avg >= 1:
                drop_pct = max(0.0, (hourly_avg - current) / hourly_avg * 100.0)
            else:
                drop_pct = 0.0
            if drop_pct > error_pct:
                status = "error"
            elif drop_pct > warn_pct:
                status = "warning"
            else:
                status = "ok"
            # Steady-state rules, worst-wins against the within-hour drop above.
            #
            #   below weekly_frac of the 7-DAY AVERAGE   -> ERROR  (red)
            #   below monthly_frac of the 30-DAY AVERAGE -> WARNING (yellow)
            #
            # The week is the tighter, more current baseline, so falling through
            # it is an ACUTE collapse and reds. The month is the slower one: a
            # site already degraded for days has a sagging weekly average it can
            # still clear, but it will not clear the monthly one -- so a CHRONIC
            # decline surfaces as amber instead of hiding behind its own decay.
            # That is the failure mode a self-referencing baseline always has,
            # and splitting the windows is what exposes it.
            #
            # min_peak gates both so a genuinely quiet site cannot alarm on noise.
            # ---- trend rules (worst-wins with the within-hour drop above) ----
            # FLOOR: absolute and configured. The only rule that survives a site
            # being dark for weeks -- every statistical baseline normalises to
            # an outage given long enough, which is exactly the blind spot the
            # averages alone could not cover.
            if floor is not None and hourly_avg < floor:
                status = _cc_worst(status, "error")
            # PERIOD: this day/week vs the one before -> catches a slow bleed
            # that no short-window rate rule can see.
            for _drop in (day_drop_pct, week_drop_pct):
                if _drop is not None and _drop > period_drop_pct:
                    status = _cc_worst(status, "error")
            # RATE: short-window fall -> catches a fast collapse hours before
            # the day boundary would show it.
            if rate_drop_pct is not None and rate_drop_pct > rate_drop_thresh:
                status = _cc_worst(status, "warning")
        return {"site_name": central_site, "current": current,
                "hourly_avg": round(hourly_avg, 1), "drop_pct": round(drop_pct, 1),
                "max_7day": max_7day, "max_30day": max_30day,
                "avg_1day": avg_1day, "avg_7day": avg_7day, "avg_30day": avg_30day,
                "floor": floor,
                "day_drop_pct": None if day_drop_pct is None else round(day_drop_pct, 1),
                "week_drop_pct": None if week_drop_pct is None else round(week_drop_pct, 1),
                "rate_drop_pct": None if rate_drop_pct is None else round(rate_drop_pct, 1),
                "status": status, "ts": samples[-1][0]}

    def maybe_snapshot(self) -> None:
        now = time.time()
        if now - self._last_snapshot < _CC_SNAPSHOT_INTERVAL:
            return
        self._last_snapshot = now
        cutoff = now - _CC_30DAY_WINDOW
        snapshot: Dict[str, dict] = {}
        for key, samples in self._samples.items():
            if len(samples) < _CC_MIN_SAMPLES:
                continue
            avg = sum(s[1] for s in samples) / len(samples)
            snapshot[key] = {"hourly_avg": round(avg, 1), "recorded_at": now}
            hist = self._hourly.setdefault(key, [])
            hist.append((now, avg))
            self._hourly[key] = [(ts, v) for ts, v in hist if ts >= cutoff]
        if snapshot:
            self._baseline.update(snapshot)
            self._persist(self._baseline_path, self._baseline)
        if self._hourly:
            self._persist(self._sevenday_path, {k: list(v) for k, v in self._hourly.items()})

    @staticmethod
    def _persist(path: str, data: dict) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ClientCountTracker: persist failed (%s): %s", path, exc)


def _build_config(central_config: Dict[str, Any]) -> Dict[str, Any]:
    """Map the Setup UI's central_config shape (mode: classic|central) onto
    ArubaClient's expected config shape (api_version: classic|new_central)."""
    cfg = dict(central_config)
    mode = cfg.pop("mode", None)
    cfg["api_version"] = "new_central" if mode == "central" else "classic"
    return cfg


_HEALTH_IDX = {"ok": 0, "warning": 1, "error": 2}  # else (no_data/pending/unknown) -> 3


class CheckHealthHistory:
    """Rolling 30-day per-check status history in HOURLY buckets [ok,warn,error,other]
    (green/yellow/red/grey). Mirror of lm central_hub_poller.CheckHealthHistory — keep
    in sync. summary() rolls hourly up to 30 DAILY buckets; hourly() returns raw."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._h: Dict[str, Dict[int, list]] = {}
        self._load()

    @staticmethod
    def _key(tenant: str, site: str, check_id: str) -> str:
        return f"{tenant}{_CC_KEYSEP}{site}{_CC_KEYSEP}{check_id}"

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            cutoff = time.time() - _CC_30DAY_WINDOW
            self._h = {
                k: {int(b): list(v) for b, v in buckets.items() if int(b) >= cutoff}
                for k, buckets in raw.items()
            }
        except Exception:  # noqa: BLE001 — absent/corrupt → start empty
            self._h = {}

    def record(self, tenant: str, site: str, check_id: str, status: str) -> None:
        now = time.time()
        buckets = self._h.setdefault(self._key(tenant, site, check_id), {})
        bucket = int(now // 3600 * 3600)
        cell = buckets.get(bucket)
        if cell is None:
            cell = [0, 0, 0, 0]
            buckets[bucket] = cell
        cell[_HEALTH_IDX.get(str(status).strip().lower(), 3)] += 1
        cutoff = now - _CC_30DAY_WINDOW
        for b in [b for b in buckets if b < cutoff]:
            del buckets[b]

    def save(self) -> None:
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: {str(b): v for b, v in bk.items()}
                           for k, bk in self._h.items()}, f, default=str)
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            pass

    def summary(self, tenant: str) -> Dict[str, Any]:
        now = time.time()
        floor = int(now // 86400 * 86400) - 29 * 86400
        prefix = f"{tenant}{_CC_KEYSEP}"
        out: Dict[str, Any] = {}
        for key, buckets in self._h.items():
            if not key.startswith(prefix):
                continue
            parts = key.split(_CC_KEYSEP, 2)
            if len(parts) != 3:
                continue
            _, site, check_id = parts
            days: Dict[int, list] = {}
            for b, cell in buckets.items():
                d = int(b // 86400 * 86400)
                if d < floor:
                    continue
                acc = days.setdefault(d, [0, 0, 0, 0])
                for i in range(4):
                    acc[i] += cell[i]
            out.setdefault(site, {})[check_id] = [
                {"d": d, "o": v[0], "w": v[1], "e": v[2], "n": v[3]}
                for d, v in sorted(days.items())
            ]
        return out

    def hourly(self, tenant: str, site: str, check_id: str) -> list:
        buckets = self._h.get(self._key(tenant, site, check_id), {})
        return [{"h": b, "o": v[0], "w": v[1], "e": v[2], "n": v[3]}
                for b, v in sorted(buckets.items())]


def client_os_counts(clients) -> Dict[str, int]:
    """``{os_label: count}`` for a browse client list, biggest first.

    The per-client ``os`` field is already normalized by ArubaClient/MistClient
    (osType / os_type / device_type, "—" when the controller reported nothing).
    Aggregated HERE rather than in the browser so every consumer of the browse
    payload — WebUI tabs, the dashboard, and any API caller — sees the same
    numbers. Unreported values collapse into "Unknown" instead of "—" so the
    label reads sensibly in a count ("12 Unknown").
    """
    counts: Dict[str, int] = {}
    for c in (clients or []):
        try:
            raw = str((c or {}).get("os") or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        label = raw if raw and raw != "\u2014" else "Unknown"
        counts[label] = counts.get(label, 0) + 1
    # biggest first, then alphabetical so equal counts render deterministically
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())))


class CentralPoller:
    """Drives ``ArubaClient`` on a 5-minute loop, writing
    ``spoke.central_status`` in the shape ``sim-views.js``'s Checks/Hardware/
    Client-Count tabs expect. No-op (empty ``central_status``) when Central
    is not configured. See the module docstring.

    Parameterized by ``instance`` so ONE class serves BOTH cloud Central and an
    on-prem Aruba Central appliance (same Aruba Central API/``ArubaClient``,
    separate config + sites-config + status slot + tracker shard files so the two
    never step on each other). Default ``instance="central"`` reproduces the
    original behavior byte-identically (the safety anchor for the refactor)."""

    # Per-instance wiring: which local_store config/sites getter to read, which
    # spoke status attr to write, and the shard filenames for the client-count
    # baseline + 30-day health history (different filenames = separate persisted
    # state, so cloud Central and on-prem monitoring the same site keep separate
    # baselines — the no-stepping guarantee at the spoke layer).
    _INSTANCES = {
        "central": {
            "config_getter": "get_central_config",
            "sites_getter": "get_central_sites_config",
            "status_attr": "central_status",
            "cc_baseline": "client_count_baseline.json",
            "cc_7day": "client_count_7day.json",
            "health_file": "check_health_history.json",
        },
        "central_on_prem": {
            "config_getter": "get_central_on_prem_config",
            "sites_getter": "get_central_on_prem_sites_config",
            "status_attr": "central_on_prem_status",
            "cc_baseline": "central_on_prem_client_count_baseline.json",
            "cc_7day": "central_on_prem_client_count_7day.json",
            "health_file": "central_on_prem_check_health_history.json",
        },
    }

    def __init__(self, spoke, instance: str = "central") -> None:
        if instance not in self._INSTANCES:
            raise ValueError(f"unknown CentralPoller instance: {instance!r}")
        self.spoke = spoke
        self._inst_name = instance
        self._inst = self._INSTANCES[instance]
        self._client: Optional[ArubaClient] = None
        self._task: Optional[asyncio.Task] = None
        # Client-count baseline tracker (7-day baseline + persistence). Files live
        # next to local_store.json in the spoke's runtime-state dir; per-instance
        # filenames keep cloud Central's baselines separate from on-prem's.
        ddir = str(spoke.local_store._path.parent)
        self._cc = ClientCountTracker(
            os.path.join(ddir, self._inst["cc_baseline"]),
            os.path.join(ddir, self._inst["cc_7day"]),
        )
        # 30-day per-check status history (green/yellow/red) for the health graphs;
        # per-instance filename so on-prem history doesn't merge with cloud's.
        self._health = CheckHealthHistory(os.path.join(ddir, self._inst["health_file"]))
        self.reload()

    # ── per-instance local_store / status accessors ─────────────────────────
    # Thin wrappers so the poll loop reads/writes THIS instance's config, sites
    # config, and status slot — cloud Central reads central_config/central_status,
    # on-prem reads central_on_prem_config/central_on_prem_status. The default
    # instance reproduces the original hardcoded behavior exactly.
    def _cfg(self) -> Dict[str, Any]:
        return getattr(self.spoke.local_store, self._inst["config_getter"])()

    def _sites_cfg(self) -> Dict[str, Any]:
        return getattr(self.spoke.local_store, self._inst["sites_getter"])()

    def _set_status(self, val: Dict[str, Any]) -> None:
        setattr(self.spoke, self._inst["status_attr"], val)

    # ── (re)build the ArubaClient from the current stored config ───────────
    def reload(self) -> None:
        cfg = _build_config(self._cfg())
        self._client = ArubaClient(cfg) if cfg.get("cluster_url") else None

    def start(self) -> None:
        """Spawn the 5-min poll loop on the running event loop. Cancels any
        prior task first (idempotent). No-op with a warning when no loop is
        running yet (callers without a loop use the FastAPI ``startup`` hook)."""
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Event loop not running; Central poll loop deferred.")
            return
        self._task = loop.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            _guard = _boundary_guard_delay(time.time())
            if _guard > 0:
                logger.info("Central poll: within %ds of a 5-min clock boundary — "
                            "delaying %.0fs to avoid a mid-refresh false positive.",
                            _BOUNDARY_GUARD_S, _guard)
                await asyncio.sleep(_guard)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let a bad poll kill the loop
                logger.warning("Central poll failed: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        if not self._client or not self._client.is_configured():
            self._set_status({})
            return
        cc_thresh = _cc_thresholds(self._cfg())
        sites_cfg = self._sites_cfg()
        site_mappings: Dict[str, str] = sites_cfg.get("site_mappings") or {}
        monitored: list = sites_cfg.get("monitored_checks") or []
        hw_checks: list = sites_cfg.get("hardware_checks") or []
        hw_check_ids = {str(h.get("id")) for h in hw_checks if h.get("id")}

        status: Dict[str, Dict[str, Any]] = {}
        client_count_status: Dict[str, Any] = {}
        hw_totals: Dict[str, int] = {}
        hw_names = {str(h.get("id")): h for h in hw_checks if h.get("id")}

        for wireless_site, central_site in site_mappings.items():
            try:
                data = await self._client.poll_site_data(central_site, hw_check_ids)
            except Exception as exc:  # noqa: BLE001
                status[wireless_site] = {"poll_error": {"status": "error", "message": str(exc)}}
                continue
            alert_counts = data.get("alert_type_counts") or {}
            insight_counts = data.get("insight_cat_counts") or {}
            # Match case-insensitively AND across BOTH the alert and insight
            # buckets. The dashboard's alert/insight query is merged, so a check
            # must fire whether Central classifies the named condition as an alert
            # or an insight — e.g. "DNS Server Failed to Respond" comes back as an
            # INSIGHT, but its quota may be typed "alert". Reading only the typed
            # bucket (case-sensitively) reported a live condition as absent, so the
            # adaptive controller ramped forever and exhausted the client pool.
            # Typed bucket wins; fall back to the other so a type mismatch never
            # hides a present condition. Shared with the other three deployments
            # via check_eval (single source of truth for this matching).
            alert_ci, insight_ci = normalize_counts(alert_counts), normalize_counts(insight_counts)
            # DIAG: what the engine looks for vs what Central actually returned for
            # this site. A monitored id absent from BOTH key lists = a site-drop
            # (poll_site_data filtered it) or a name diff; present = should fire.
            logger.info("central-check diag [%s→%s]: monitored=%s alert_keys=%s insight_keys=%s",
                        wireless_site, central_site,
                        [str(c.get("id")) for c in monitored if isinstance(c, dict) and c.get("id")],
                        sorted(alert_ci), sorted(insight_ci))
            checks: Dict[str, Any] = {}
            for chk in monitored:
                cid = str(chk.get("id") or "")
                if not cid:
                    continue
                # Per-site monitoring: a check pinned to a site evaluates ONLY on
                # that site (central_site); an empty/absent site = global (every
                # mapped site). Lets you monitor an insight/alert at one site.
                chk_site = str(chk.get("site") or "").strip().lower()
                if chk_site and chk_site not in (str(central_site).lower(), str(wireless_site).lower(), "all sites"):
                    continue
                n = count_for_check(chk, alert_ci, insight_ci)
                # INVERTED semantics: this is a demo/simulation platform that is
                # SUPPOSED to be generating these alerts/insights. A monitored check
                # is HEALTHY (ok) when its error IS present, and FAILING (error) when
                # the expected error is NOT detected — the sim stopped producing it.
                # Monitor-for-absence: notify when the expected error goes missing.
                if n > 0:
                    checks[cid] = {"status": "ok", "message": f"{n} active (as expected)"}
                else:
                    # Surface WHY the expected error is absent (mirrors hub twin):
                    # site returned NOTHING → site dropped / name mismatch; vs the
                    # site reporting other conditions → sim not firing THIS alert.
                    _seen = len(alert_ci) + len(insight_ci)
                    _cc = int(data.get("client_count", 0) or 0)
                    if _seen == 0:
                        _msg = (f"Expected error NOT detected — site returned no "
                                f"alerts/insights ({_cc} clients; site dropped or "
                                f"name mismatch?)")
                    else:
                        _msg = (f"Expected error NOT detected — sim not firing "
                                f"(0 seen; {_seen} other condition(s) at site, "
                                f"{_cc} clients)")
                    checks[cid] = {"status": "error", "message": _msg}
            status[wireless_site] = checks
            current = int(data.get("client_count", 0) or 0)
            wired = int(data.get("wired_clients", 0) or 0)
            wireless = int(data.get("wireless_clients", 0) or 0)
            # Track total, wired, and wireless as SEPARATE series so each is
            # evaluated on its own baseline/peak with the same thresholds — a
            # wired-only or wireless-only die-off is caught even when the total is
            # masked (e.g. wired collapses while wireless spikes).
            self._cc.record(_CC_SCOPE, wireless_site, current)
            self._cc.record(_CC_SCOPE, wireless_site, wired, kind="wired")
            self._cc.record(_CC_SCOPE, wireless_site, wireless, kind="wireless")
            # Per-site FLOOR, resolved here because entry() only sees thresholds.
            # Absent/0 disables the floor rule for THIS site alone.
            cc_thresh = dict(cc_thresh or {})
            cc_thresh["floor"] = _cc_site_floor(
                self._sites_cfg(), wireless_site, central_site)
            cc_entry = self._cc.entry(_CC_SCOPE, wireless_site, central_site, cc_thresh)
            w_entry = self._cc.entry(_CC_SCOPE, wireless_site, central_site, cc_thresh, kind="wired")
            wl_entry = self._cc.entry(_CC_SCOPE, wireless_site, central_site, cc_thresh, kind="wireless")
            cc_entry["wired"] = wired
            cc_entry["wireless"] = wireless
            cc_entry["wired_status"] = w_entry["status"]
            cc_entry["wired_drop_pct"] = w_entry["drop_pct"]
            cc_entry["wireless_status"] = wl_entry["status"]
            cc_entry["wireless_drop_pct"] = wl_entry["drop_pct"]
            # Overall = worst of total/wired/wireless.
            cc_entry["status"] = _cc_worst(cc_entry["status"], w_entry["status"], wl_entry["status"])
            client_count_status[wireless_site] = cc_entry
            # Surface the site's client-count monitor as a CHECK so "everything
            # monitored" shows on the dashboard Checks view. Direct (NOT inverted)
            # semantics: a DROP means the sim clients died -> warning / error.
            checks["Steady Client Count 1hr Average"] = {
                "status": cc_entry["status"],
                "message": (f"{cc_entry['current']} clients vs {cc_entry['hourly_avg']} hr-avg "
                            f"(down {cc_entry['drop_pct']}%) · wired {wired} (down {w_entry['drop_pct']}%) "
                            f"· wireless {wireless} (down {wl_entry['drop_pct']}%)"),
            }
            for alert_id, devices in (data.get("hw_devices") or {}).items():
                hw_totals[alert_id] = hw_totals.get(alert_id, 0) + sum(devices.values())

        # Per-device hardware monitoring: look each monitored hardware device up in
        # the live device list and add a check on its pinned site — DOWN = error
        # (a monitored switch/AP/gateway is offline). new_central only; best-effort.
        if hw_checks:
            try:
                all_devices = await self._client._nc_devices()
            except Exception:  # noqa: BLE001
                all_devices = []
            dev_by_key: Dict[str, dict] = {}
            for d in all_devices:
                for k in (d.get("serialNumber"), d.get("serial"), d.get("deviceName"), d.get("name")):
                    if k:
                        dev_by_key[str(k)] = d
            for hc in hw_checks:
                hid = str(hc.get("id") or "")
                if not hid:
                    continue
                hsite = str(hc.get("site") or "").strip().lower()
                dev = dev_by_key.get(hid)
                up = str((dev or {}).get("status") or "").upper() in ("UP", "ONLINE")
                label = str(hc.get("name") or hid)
                for wsite, csite in site_mappings.items():
                    if hsite and hsite not in (str(csite).lower(), str(wsite).lower(), "all sites"):
                        continue
                    status.setdefault(wsite, {})[label] = {
                        "status": "ok" if up else "error",
                        "message": "up" if up else "DOWN",
                    }

        hardware_alerts = [
            {"id": aid, "name": (hw_names.get(aid) or {}).get("name", aid),
             "device_type": (hw_names.get(aid) or {}).get("device_type", ""),
             "total": total}
            for aid, total in hw_totals.items()
        ]
        # Record each check's status into the 30-day health history (hourly bucket),
        # then relay the DAILY summary so the hub dashboard can show the strip for a
        # distributed tenant (hourly-on-hover is fetched on demand via CS_GET_HEALTH).
        for wsite, checks_map in status.items():
            if not isinstance(checks_map, dict):
                continue
            for cid, info in checks_map.items():
                st = (info.get("status") if isinstance(info, dict) else info) or "no_data"
                self._health.record(_CC_SCOPE, wsite, cid, st)
        self._set_status({
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
            "health": self._health.summary(_CC_SCOPE),
            "fetched_at": time.time(),
        })
        # Append the hourly snapshot to the 7-day baseline history (self-gated to
        # once per hour) and persist — the stable reference sustained drops flag against.
        self._cc.maybe_snapshot()
        self._health.save()

    # ── on-demand actions (Setup → Central API tab) ─────────────────────────

    async def available_checks(self) -> Dict[str, Any]:
        if not self._client:
            return {"status": "SUCCESS", "alerts": [], "insights": [], "hardware": [],
                    "warning": "Central not configured."}
        result = await self._client.available_checks()
        return {"status": "SUCCESS", **result}

    async def browse(self) -> Dict[str, Any]:
        """On-demand FULL Central inventory for the Central → Sites/Alerts/Clients
        tabs — every site, alert, insight and client from Central, independent of
        site_mappings (which only scope the background Checks poller). Mirrors the
        original webui-hub browse (ArubaClient.browse_all). Cached inside the
        client (5–15 min per endpoint), so repeated tab opens don't hammer Central.
        """
        if not self._client or not self._client.is_configured():
            return {"status": "SUCCESS", "sites": [], "alerts": [], "insights": [],
                    "clients": [], "devices_by_site": {}, "clients_by_site": {},
                    "os_counts": {}, "warning": "Central not configured."}
        try:
            data = await self._client.browse_all()
            return {"status": "SUCCESS", **data,
                    "os_counts": client_os_counts(data.get("clients"))}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Central browse failed [%s]: %s",
                           self.spoke.spoke_id, exc)
            return {"status": "ERROR", "message": str(exc),
                    "sites": [], "alerts": [], "insights": [], "clients": [],
                    "os_counts": {}}

    async def test_connection(self) -> Dict[str, Any]:
        """Best-effort connectivity check for the Setup → Central API tab's
        "Test Central" button. Mirrors the hub's test_central route shape
        ({"spokes": [...]}) with a single entry describing this spoke.

        Logs every outcome to the cs spoke log (CentralPoller logger) so a
        failed/missing-creds test is diagnosable from /var/log/lm/cs-spoke.log
        instead of only surfacing in the UI's one-line ``status=`` field. The
        hub's /sim/api/{tenant}/test-central route reads RELAYED telemetry (not a
        live probe), so when a row shows all-— it means the spoke hasn't relayed
        a populated central block yet — check this log for the real reason."""
        if not self._client or not self._client.is_configured():
            logger.info("test_connection [%s]: Central not configured (no cluster_url)",
                        self.spoke.spoke_id)
            return {"status": "SUCCESS", "spokes": [{
                "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
                "token_state": None, "token_valid": False,
                "status": "Central not configured.",
            }]}
        chash = getattr(self._client, "_config_hash", "?")
        mode = getattr(self._client, "api_version", "?")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                await self._client._ensure_token(client)
            token_valid = True
            msg = "Connected."
            logger.info("test_connection [%s] mode=%s cfg=%s: connected to Central",
                        self.spoke.spoke_id, mode, chash)
        except Exception as exc:  # noqa: BLE001 — surface any token/transport error
            token_valid = False
            msg = f"Connection failed: {exc}"
            # The full exception (incl. underlying httpx ConnectError / HTTPStatusError
            # response body) lands in the log; the UI only gets the one-line str(exc).
            logger.warning("test_connection [%s] mode=%s cfg=%s FAILED: %r",
                           self.spoke.spoke_id, mode, chash, exc)
        return {"status": "SUCCESS", "spokes": [{
            "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
            "token_state": self._client._token_state() if self._client else None,
            "token_valid": token_valid, "status": msg,
        }]}
