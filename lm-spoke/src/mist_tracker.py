"""Mist-owned client-count + check-health helpers for ``mist_poller``.

Central and Mist are SEPARATE products and must not share code. Earlier the
Mist poller imported ``ClientCountTracker`` / ``CheckHealthHistory`` /
``_cc_thresholds`` / ``_cc_worst`` / ``_CC_SCOPE`` straight out of
``central_poller`` — that tied Mist's runtime to Aruba's module. This module
is the MIRROR (not a shared import): the same client-count baseline + drop
detection and the same 30-day per-check health history, but owned by Mist so
Central can change its copy without touching Mist and vice-versa.

The logic is identical to ``central_poller``'s (ported verbatim from the
source webui-spoke) so the dashboard shape stays the same; only the module
identity, logger, and on-disk file names differ. ``check_eval`` (the generic
``count_for_check`` / ``normalize_counts`` matcher) stays shared — it is a
data-source-neutral primitive used across the whole sim system, not Central
code.

A separate copy of this same logic also lives in lm's
``central_hub_poller.py`` (centralized mode) — keep these three in sync by
hand when the drop-detection / health-bucketing math changes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger("MistTracker")

# Client-count baseline constants — mirrored verbatim from central_poller
# (ported from the source webui-spoke server.py). The alarm baseline is a
# 7-DAY rolling average of hourly snapshots (NOT the 1h average), so a
# prolonged client drop stays flagged instead of the baseline sagging to
# match it. KEEP IN SYNC with central_poller.py + lm central_hub_poller.py.
_CC_WINDOW = 3600
_CC_MIN_SAMPLES = 3
_CC_WARN_PCT = 20.0        # >20% below the hour average -> WARNING
_CC_ERROR_PCT = 50.0       # >50% below -> ERROR
_CC_7DAY_WINDOW = 7 * 86400
_CC_30DAY_WINDOW = 30 * 86400   # long-run history retention (7-day is a subset)
_CC_SNAPSHOT_INTERVAL = 3600
# Severe sustained die-off: current hour < 20% of the rolling PEAK (max hourly-avg)
# over the window -> ERROR. Gated on a meaningful peak so a quiet site can't error on noise.
_CC_MAX_FRACTION = 0.20
_CC_MAX_MIN_PEAK = 5
_CC_KEYSEP = "\x1f"
_MIST_CC_SCOPE = "_"  # single-tenant spoke -> one fixed scope key


def _mist_cc_thresholds(mist_config):
    """Per-tenant client-count CHECK thresholds for Mist, read from
    ``mist_config['cc_thresholds']`` (Setup -> Mist API) with the module
    defaults as fallback and clamped to sane ranges. Mirror of
    ``central_poller._cc_thresholds`` (reads the mist config, not central)."""
    t = (mist_config or {}).get("cc_thresholds") or {}

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
    return {"warn_pct": warn, "error_pct": err, "die_off_frac": die, "min_peak": peak}


_CC_SEVERITY = {"error": 3, "warning": 2, "ok": 1}  # else (no_data/pending/...) -> 0


def _mist_cc_worst(*statuses):
    """Worst (most severe) of a set of client-count statuses (error > warning
    > ok > no_data). Mirror of ``central_poller._cc_worst``."""
    worst, rank = None, -1
    for s in statuses:
        r = _CC_SEVERITY.get(s, 0)
        if r > rank:
            rank, worst = r, s
    return worst or "ok"


class MistClientCountTracker:
    """Per-(scope, wsite) client-count baseline + drop detection for Mist.
    Mirror of ``central_poller.ClientCountTracker`` (ported from the source
    webui-spoke). Monitoring a site = watching its client count for a
    sustained DROP: 1h of raw samples gives the smoothed "current hourly"
    average, a 7-DAY history of hourly snapshots is the STABLE alarm
    baseline. ``drop_pct = (baseline - hourly_avg) / baseline``; DEGRADED at
    >= warn_pct / error_pct. Both the last-hour baseline and the 7-day
    history persist to disk so a restart keeps the reference."""

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

    def entry(self, scope: str, wsite: str, mist_site: str, thresholds=None, kind: str = "") -> Dict[str, Any]:
        """Per-site client-count status (doubles as a dashboard CHECK). Tiered:
          - WITHIN-HOUR drop (current vs last-hour average): WARNING / ERROR at
            ``warn_pct`` / ``error_pct`` below.
          - SUSTAINED die-off: current hour < ``die_off_frac`` of the 7/30-DAY
            rolling PEAK -> ERROR. Gated on a peak of at least ``min_peak``;
            die_off_frac=0 disables it.
        ``thresholds`` (from _mist_cc_thresholds -> mist_config) overrides the
        module defaults per tenant. Mirror of ClientCountTracker.entry."""
        _t = thresholds or {}
        warn_pct = _t.get("warn_pct", _CC_WARN_PCT)
        error_pct = _t.get("error_pct", _CC_ERROR_PCT)
        die_off_frac = _t.get("die_off_frac", _CC_MAX_FRACTION)
        min_peak = _t.get("min_peak", _CC_MAX_MIN_PEAK)
        now = time.time()
        key = self._key(scope, wsite, kind)
        samples = self._samples.get(key, [])
        hist = self._hourly.get(key, [])
        vals_7d = [v for ts, v in hist if ts >= now - _CC_7DAY_WINDOW]
        vals_30d = [v for ts, v in hist if ts >= now - _CC_30DAY_WINDOW]
        if not samples:
            return {"site_name": mist_site, "current": 0, "hourly_avg": 0,
                    "drop_pct": 0.0, "max_7day": round(max(vals_7d or [0]), 1),
                    "max_30day": round(max(vals_30d or [0]), 1),
                    "status": "no_data", "ts": now}
        current = samples[-1][1]
        hourly_avg = sum(s[1] for s in samples) / len(samples)
        max_7day = round(max(vals_7d + [hourly_avg]), 1)
        max_30day = round(max(vals_30d + [hourly_avg]), 1)
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
            if (die_off_frac > 0
                    and ((max_7day >= min_peak and hourly_avg < die_off_frac * max_7day)
                         or (max_30day >= min_peak and hourly_avg < die_off_frac * max_30day))):
                status = "error"
        return {"site_name": mist_site, "current": current,
                "hourly_avg": round(hourly_avg, 1), "drop_pct": round(drop_pct, 1),
                "max_7day": max_7day, "max_30day": max_30day,
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
            logger.warning("MistClientCountTracker: persist failed (%s): %s", path, exc)


_HEALTH_IDX = {"ok": 0, "warning": 1, "error": 2}  # else (no_data/pending/unknown) -> 3


class MistCheckHealthHistory:
    """Rolling 30-day per-check status history in HOURLY buckets
    [ok,warn,error,other] (green/yellow/red/grey) for Mist. Mirror of
    ``central_poller.CheckHealthHistory``. summary() rolls hourly up to 30
    DAILY buckets; hourly() returns raw."""

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
        except Exception:  # noqa: BLE001 — absent/corrupt -> start empty
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