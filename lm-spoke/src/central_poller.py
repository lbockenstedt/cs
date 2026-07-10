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

logger = logging.getLogger("CentralPoller")

_POLL_INTERVAL_S = 300  # 5 min — matches aruba.py's own cache TTLs

# Client-count baseline constants — ported verbatim from the source webui-spoke
# (server.py). The alarm baseline is a 7-DAY rolling average of hourly snapshots
# (NOT the 1h average), so a prolonged client drop stays flagged instead of the
# baseline sagging to match it. KEEP IN SYNC with lm's central_hub_poller.py
# ClientCountTracker (centralized mode uses that copy).
_CC_WINDOW = 3600
_CC_MIN_SAMPLES = 3
_CC_DROP_PCT = 25.0
_CC_7DAY_WINDOW = 7 * 86400
_CC_SNAPSHOT_INTERVAL = 3600
_CC_KEYSEP = "\x1f"
_CC_SCOPE = "_"  # single-tenant spoke → one fixed scope key


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
            cutoff = now - _CC_7DAY_WINDOW
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

    def entry(self, scope: str, wsite: str, central_site: str) -> Dict[str, Any]:
        now = time.time()
        key = self._key(scope, wsite)
        samples = self._samples.get(key, [])
        hist = self._hourly.get(key, [])
        has_7day = len(hist) >= 2
        baseline_7day = (sum(v for _, v in hist) / len(hist)) if has_7day else None
        if not samples:
            return {"site_name": central_site, "current": 0, "hourly_avg": 0,
                    "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                    "baseline_source": "7day" if has_7day else "none",
                    "drop_pct": 0.0, "status": "NO_DATA", "ts": now}
        current = samples[-1][1]
        if len(samples) < _CC_MIN_SAMPLES:
            saved = self._baseline.get(key)
            if saved:
                hourly_avg = saved["hourly_avg"]
                baseline = baseline_7day if has_7day else hourly_avg
                drop_pct = max(0.0, (baseline - hourly_avg) / baseline * 100.0) if baseline >= 1 else 0.0
                status = "DEGRADED" if drop_pct >= _CC_DROP_PCT else "OK"
                return {"site_name": central_site, "current": current, "hourly_avg": hourly_avg,
                        "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                        "baseline_source": "7day" if has_7day else "hourly",
                        "drop_pct": round(drop_pct, 1), "status": status,
                        "ts": samples[-1][0], "baseline_stale": True}
            return {"site_name": central_site, "current": current, "hourly_avg": current,
                    "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                    "baseline_source": "7day" if has_7day else "none",
                    "drop_pct": 0.0, "status": "NO_DATA", "ts": samples[-1][0]}
        hourly_avg = sum(s[1] for s in samples) / len(samples)
        baseline = baseline_7day if has_7day else hourly_avg
        if baseline < 1:
            drop_pct, status = 0.0, "OK"
        else:
            drop_pct = (baseline - hourly_avg) / baseline * 100.0
            status = "DEGRADED" if drop_pct >= _CC_DROP_PCT else "OK"
        return {"site_name": central_site, "current": current, "hourly_avg": round(hourly_avg, 1),
                "baseline_7day": round(baseline_7day, 1) if baseline_7day is not None else None,
                "baseline_source": "7day" if has_7day else "hourly",
                "drop_pct": round(max(0.0, drop_pct), 1), "status": status,
                "ts": samples[-1][0], "baseline_stale": False}

    def maybe_snapshot(self) -> None:
        now = time.time()
        if now - self._last_snapshot < _CC_SNAPSHOT_INTERVAL:
            return
        self._last_snapshot = now
        cutoff = now - _CC_7DAY_WINDOW
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
            checks: Dict[str, Any] = {}
            for chk in monitored:
                cid = str(chk.get("id") or "")
                if not cid:
                    continue
                counts = alert_counts if (chk.get("type") or "alert") == "alert" else insight_counts
                n = int(counts.get(cid, 0) or 0)
                checks[cid] = {"status": "ok" if n == 0 else "error",
                               "message": f"{n} active" if n else "No active alerts"}
            if not checks and data.get("site_health") is not None:
                # No monitored checks configured yet — fall back to overall
                # site health so the Checks tab isn't empty once a site is
                # mapped, even before the operator picks specific checks.
                checks["site_health"] = {
                    "status": "ok" if (data.get("site_health") or 0) >= 80 else "warning",
                    "message": f"Site health {data.get('site_health')}",
                }
            status[wireless_site] = checks
            self._cc.record(_CC_SCOPE, wireless_site, int(data.get("client_count", 0) or 0))
            client_count_status[wireless_site] = self._cc.entry(
                _CC_SCOPE, wireless_site, central_site)
            for alert_id, devices in (data.get("hw_devices") or {}).items():
                hw_totals[alert_id] = hw_totals.get(alert_id, 0) + sum(devices.values())

        hardware_alerts = [
            {"id": aid, "name": (hw_names.get(aid) or {}).get("name", aid),
             "device_type": (hw_names.get(aid) or {}).get("device_type", ""),
             "total": total}
            for aid, total in hw_totals.items()
        ]
        self.spoke.central_status = {
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
            "fetched_at": time.time(),
        }
        # Append the hourly snapshot to the 7-day baseline history (self-gated to
        # once per hour) and persist — the stable reference sustained drops flag against.
        self._cc.maybe_snapshot()

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
