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
    def _key(scope: str, wsite: str) -> str:
        return f"{scope}{_CC_KEYSEP}{wsite}"

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

    def record(self, scope: str, wsite: str, current: int) -> None:
        now = time.time()
        key = self._key(scope, wsite)
        samples = self._samples.setdefault(key, [])
        samples.append((now, int(current)))
        cutoff = now - _CC_WINDOW
        self._samples[key] = [s for s in samples if s[0] >= cutoff]

    def entry(self, scope: str, wsite: str, central_site: str, thresholds=None) -> Dict[str, Any]:
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
        key = self._key(scope, wsite)
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


class CentralPoller:
    """Drives ``ArubaClient`` on a 5-minute loop, writing
    ``spoke.central_status`` in the shape ``sim-views.js``'s Checks/Hardware/
    Client-Count tabs expect. No-op (empty ``central_status``) when Central
    is not configured. See the module docstring."""

    def __init__(self, spoke) -> None:
        self.spoke = spoke
        self._client: Optional[ArubaClient] = None
        self._task: Optional[asyncio.Task] = None
        # Client-count baseline tracker (7-day baseline + persistence). Files live
        # next to local_store.json in the spoke's runtime-state dir.
        ddir = str(spoke.local_store._path.parent)
        self._cc = ClientCountTracker(
            os.path.join(ddir, "client_count_baseline.json"),
            os.path.join(ddir, "client_count_7day.json"),
        )
        # 30-day per-check status history (green/yellow/red) for the health graphs.
        self._health = CheckHealthHistory(os.path.join(ddir, "check_health_history.json"))
        self.reload()

    # ── (re)build the ArubaClient from the current stored config ───────────
    def reload(self) -> None:
        cfg = _build_config(self.spoke.local_store.get_central_config())
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
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let a bad poll kill the loop
                logger.warning("Central poll failed: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        if not self._client or not self._client.is_configured():
            self.spoke.central_status = {}
            return
        cc_thresh = _cc_thresholds(self.spoke.local_store.get_central_config())
        sites_cfg = self.spoke.local_store.get_central_sites_config()
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
                checks[cid] = {"status": "ok" if n > 0 else "error",
                               "message": f"{n} active (as expected)" if n else "Expected error NOT detected"}
            status[wireless_site] = checks
            current = int(data.get("client_count", 0) or 0)
            self._cc.record(_CC_SCOPE, wireless_site, current)
            cc_entry = self._cc.entry(_CC_SCOPE, wireless_site, central_site, cc_thresh)
            # Break out wired vs wireless (Central reports both; total = their sum).
            cc_entry["wired"] = int(data.get("wired_clients", 0) or 0)
            cc_entry["wireless"] = int(data.get("wireless_clients", 0) or 0)
            client_count_status[wireless_site] = cc_entry
            # Surface the site's client-count monitor as a CHECK so "everything
            # monitored" shows on the dashboard Checks view. Direct (NOT inverted)
            # semantics: a DROP means the sim clients died -> warning (>20% below
            # the hour average) / error (>50%). See ClientCountTracker.
            checks["Steady Client Count 1hr Average"] = {
                "status": cc_entry["status"],
                "message": f"{cc_entry['current']} clients vs {cc_entry['hourly_avg']} hr-avg (down {cc_entry['drop_pct']}%)",
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
        self.spoke.central_status = {
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
            "health": self._health.summary(_CC_SCOPE),
            "fetched_at": time.time(),
        }
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
                    "warning": "Central not configured."}
        try:
            data = await self._client.browse_all()
            return {"status": "SUCCESS", **data}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Central browse failed [%s]: %s",
                           self.spoke.spoke_id, exc)
            return {"status": "ERROR", "message": str(exc),
                    "sites": [], "alerts": [], "insights": [], "clients": []}

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
