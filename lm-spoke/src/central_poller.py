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

# Client-count baseline constants — ported verbatim from the source webui-spoke
# (server.py). The alarm baseline is a 7-DAY rolling average of hourly snapshots
# (NOT the 1h average), so a prolonged client drop stays flagged instead of the
# baseline sagging to match it. KEEP IN SYNC with lm's central_hub_poller.py
# ClientCountTracker (centralized mode uses that copy).
_CC_WINDOW = 3600
_CC_MIN_SAMPLES = 3
_CC_WARN_PCT = 20.0        # >20% below the hour average -> WARNING
_CC_ERROR_PCT = 50.0       # >50% below -> ERROR
_CC_7DAY_WINDOW = 7 * 86400
_CC_30DAY_WINDOW = 30 * 86400   # long-run history retention (the 7-day is a subset)
_CC_SNAPSHOT_INTERVAL = 3600
# Severe sustained die-off: current hour < 20% of the rolling PEAK (max hourly-avg)
# over the window → ERROR. Gated on a meaningful peak so a quiet site can't error on noise.
_CC_MAX_FRACTION = 0.20
_CC_MAX_MIN_PEAK = 5
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
    return {"warn_pct": warn, "error_pct": err, "die_off_frac": die, "min_peak": peak}


_CC_SEVERITY = {"error": 3, "warning": 2, "ok": 1}  # else (no_data/pending/…) -> 0


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
        now = time.time()
        key = self._key(scope, wsite, kind)
        samples = self._samples.get(key, [])
        hist = self._hourly.get(key, [])
        vals_7d = [v for ts, v in hist if ts >= now - _CC_7DAY_WINDOW]
        vals_30d = [v for ts, v in hist if ts >= now - _CC_30DAY_WINDOW]
        if not samples:
            return {"site_name": central_site, "current": 0, "hourly_avg": 0,
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
        return {"site_name": central_site, "current": current,
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
    in sync. summary() rolls hourly up to 30 DAILY buckets; hourly buckets are
    kept raw for the per-check timeline view."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._hourly: Dict[str, list] = {}  # check_id -> [(ts, [ok,warn,error,other])]
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            now = time.time()
            cutoff = now - _CC_30DAY_WINDOW
            self._hourly = {
                k: [(float(ts), v) for ts, v in entries if float(ts) >= cutoff]
                for k, entries in raw.items()
            }
        except Exception:  # noqa: BLE001
            self._hourly = {}

    def record(self, check_id: str, status: str) -> None:
        now = time.time()
        idx = _HEALTH_IDX.get(status, 3)
        bucket = [0, 0, 0, 0]
        bucket[idx] = 1
        hist = self._hourly.setdefault(check_id, [])
        if hist and (now - hist[-1][0]) < 3600:
            old_ts, old_bucket = hist[-1]
            merged = [old_bucket[i] + bucket[i] for i in range(4)]
            hist[-1] = (old_ts, merged)
        else:
            hist.append((now, bucket))
        cutoff = now - _CC_30DAY_WINDOW
        self._hourly[check_id] = [(ts, v) for ts, v in hist if ts >= cutoff]

    def maybe_persist(self) -> None:
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._hourly, f)
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CheckHealthHistory: persist failed: %s", exc)

    def summary(self, check_id: str) -> Dict[str, Any]:
        hist = self._hourly.get(check_id, [])
        now = time.time()
        daily: list = []
        for day_offset in range(30):
            day_start = now - (day_offset + 1) * 86400
            day_end = now - day_offset * 86400
            day_bucket = [0, 0, 0, 0]
            for ts, b in hist:
                if day_start <= ts < day_end:
                    day_bucket = [day_bucket[i] + b[i] for i in range(4)]
            daily.append(day_bucket)
        return {"check_id": check_id, "daily": daily}


class CentralPoller:
    """Background Aruba Central poller.

    Runs ``ArubaClient`` (synchronous HTTP) in a thread via ``asyncio.to_thread``
    so the blocking ``requests`` calls never stall the cs spoke's shared event
    loop. A stalled event loop is the root cause of stale heartbeats — the
    heartbeat relay task can't fire while the loop is blocked inside a sync HTTP
    call to Aruba Central that has no timeout (the recurring cs-spoke-1 stale
    heartbeat issue). All ArubaClient instantiation + API calls are offloaded;
    only the lightweight in-memory assembly (building ``central_status`` dicts)
    runs on the loop.
    """

    def __init__(self, spoke) -> None:
        self._spoke = spoke
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        base = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base, "..", "data")
        self._cc_tracker = ClientCountTracker(
            os.path.join(data_dir, "cc_baseline.json"),
            os.path.join(data_dir, "cc_sevenday.json"),
        )
        self._health_history = CheckHealthHistory(
            os.path.join(data_dir, "check_health_history.json"),
        )

    async def run(self) -> None:
        """Main poll loop. Wraps all blocking ArubaClient I/O in
        ``asyncio.to_thread`` so the event loop (and thus the heartbeat relay)
        is never blocked. Catches all exceptions per-iteration so a single
        failed poll can't kill the loop (which would also stop heartbeats from
        being refreshed via the status payload)."""
        logger.info("CentralPoller: starting (interval=%ss)", _POLL_INTERVAL_S)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("CentralPoller: poll iteration failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("CentralPoller: stopped")

    async def _poll_once(self) -> None:
        """Single poll iteration. All ArubaClient (sync HTTP) work is offloaded
        to a thread so the event loop stays responsive for heartbeats."""
        central_config = (self._spoke.local_store.get_central_config()
                          if hasattr(self._spoke, "local_store") else {})
        sites_config = (self._spoke.local_store.get_central_sites_config()
                        if hasattr(self._spoke, "local_store") else {})
        if not central_config or not sites_config:
            self._spoke.central_status = {
                "status": {}, "hardware_alerts": [], "client_count_status": {}
            }
            return

        # Offload the ENTIRE blocking ArubaClient workflow (construct + all HTTP
        # calls) to a thread. This is the critical fix: ArubaClient uses
        # ``requests`` synchronously with no per-request timeout, so a slow/hung
        # Aruba Central endpoint blocks the asyncio event loop for the full
        # duration of the HTTP stall — which prevents the heartbeat relay task
        # from firing and produces the stale-heartbeat symptom.
        result = await asyncio.to_thread(
            self._poll_aruba_sync, central_config, sites_config
        )
        self._spoke.central_status = result

        # Lightweight in-memory updates (no I/O) — safe on the event loop.
        self._cc_tracker.maybe_snapshot()
        self._health_history.maybe_persist()

    def _poll_aruba_sync(self, central_config: Dict[str, Any],
                         sites_config: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous Aruba Central polling — runs in a worker thread.

        This is the ONLY place ArubaClient is instantiated and called. By
        running in a thread, a hung/slow Aruba API response blocks the thread,
        NOT the asyncio event loop, so the spoke's heartbeat relay continues
        firing on schedule regardless of Aruba Central availability."""
        thresholds = _cc_thresholds(central_config)
        cfg = _build_config(central_config)
        try:
            client = ArubaClient(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CentralPoller: ArubaClient init failed: %s", exc)
            return {"status": {}, "hardware_alerts": [], "client_count_status": {}}

        status: Dict[str, Dict[str, Any]] = {}
        hardware_alerts: list = []
        client_count_status: Dict[str, Any] = {}

        for site_name, site_cfg in (sites_config or {}).items():
            try:
                site_status = self._poll_site_sync(client, site_name, site_cfg, thresholds)
                status[site_name] = site_status.get("checks", {})
                hardware_alerts.extend(site_status.get("hardware_alerts", []))
                if site_status.get("client_count"):
                    client_count_status[site_name] = site_status["client_count"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("CentralPoller: site %s failed: %s", site_name, exc)
                status[site_name] = {"_site": {"status": "error", "message": str(exc)}}

        return {
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
        }

    def _poll_site_sync(self, client, site_name: str, site_cfg: Dict[str, Any],
                        thresholds: Dict[str, Any]) -> Dict[str, Any]:
        """Poll a single Aruba Central site (sync, in worker thread)."""
        checks: Dict[str, Any] = {}
        hardware_alerts: list = []
        client_count: Optional[Dict[str, Any]] = None

        wsite = site_cfg.get("wsite") or site_name

        try:
            raw_checks = client.get_checks(site=wsite) if hasattr(client, "get_checks") else []
            for chk in raw_checks or []:
                cid = chk.get("id") or chk.get("check_id") or "unknown"
                cstatus = chk.get("status", "no_data")
                cmsg = chk.get("message", "")
                checks[cid] = {"status": cstatus, "message": cmsg}
                self._health_history.record(cid, cstatus)
        except Exception as exc:  # noqa: BLE001
            logger.debug("CentralPoller: get_checks for %s failed: %s", site_name, exc)

        try:
            raw_hw = client.get_hardware_alerts(site=wsite) if hasattr(client, "get_hardware_alerts") else []
            for hw in raw_hw or []:
                hardware_alerts.append({
                    "id": hw.get("id", ""),
                    "name": hw.get("name", ""),
                    "device_type": hw.get("device_type", ""),
                    "total": hw.get("total", 0),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("CentralPoller: get_hardware_alerts for %s failed: %s", site_name, exc)

        try:
            counts = client.get_client_counts(site=wsite) if hasattr(client, "get_client_counts") else None
            if counts is not None:
                current = count_for_check(counts) if hasattr(counts, '__len__') else 0
                self._cc_tracker.record(_CC_SCOPE, wsite, current)
                client_count = self._cc_tracker.entry(
                    _CC_SCOPE, wsite, site_name, thresholds=thresholds
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("CentralPoller: get_client_counts for %s failed: %s", site_name, exc)

        return {
            "checks": checks,
            "hardware_alerts": hardware_alerts,
            "client_count": client_count,
        }

    def start(self) -> None:
        """Start the poll loop as a background task (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Signal the poll loop to stop and await its shutdown."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None