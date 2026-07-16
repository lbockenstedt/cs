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
_CC_WARN_PCT = 20.0        # >20% below the hour average -> WARNING
_CC_ERROR_PCT = 50.0       # >50% below -> ERROR
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
        """Per-site client-count status. Baseline = the AVERAGE over the last hour;
        the site goes WARNING when the current count is >20%% below that hourly
        average and ERROR when >50%% below (needs _CC_MIN_SAMPLES first -> no_data).
        Detects sim-client die-off within the last hour (the demo's failure mode).
        Status values (ok/warning/error/no_data) double as dashboard CHECK statuses."""
        now = time.time()
        key = self._key(scope, wsite)
        samples = self._samples.get(key, [])
        if not samples:
            return {"site_name": central_site, "current": 0, "hourly_avg": 0,
                    "drop_pct": 0.0, "status": "no_data", "ts": now}
        current = samples[-1][1]
        hourly_avg = sum(s[1] for s in samples) / len(samples)
        if len(samples) < _CC_MIN_SAMPLES:
            drop_pct, status = 0.0, "no_data"
        elif hourly_avg >= 1:
            drop_pct = max(0.0, (hourly_avg - current) / hourly_avg * 100.0)
            if drop_pct > _CC_ERROR_PCT:
                status = "error"
            elif drop_pct > _CC_WARN_PCT:
                status = "warning"
            else:
                status = "ok"
        else:
            drop_pct, status = 0.0, "ok"
        return {"site_name": central_site, "current": current,
                "hourly_avg": round(hourly_avg, 1), "drop_pct": round(drop_pct, 1),
                "status": status, "ts": samples[-1][0]}

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
            # Match case-insensitively AND across BOTH the alert and insight
            # buckets. The dashboard's alert/insight query is merged, so a check
            # must fire whether Central classifies the named condition as an alert
            # or an insight — e.g. "DNS Server Failed to Respond" comes back as an
            # INSIGHT, but its quota may be typed "alert". Reading only the typed
            # bucket (case-sensitively) reported a live condition as absent, so the
            # adaptive controller ramped forever and exhausted the client pool.
            # Typed bucket wins; fall back to the other so a type mismatch never
            # hides a present condition.
            def _ci(d):
                out: Dict[str, int] = {}
                for k, v in (d or {}).items():
                    kk = str(k).strip().lower()
                    out[kk] = out.get(kk, 0) + int(v or 0)
                return out
            alert_ci, insight_ci = _ci(alert_counts), _ci(insight_counts)
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
                key = cid.strip().lower()
                is_alert = (chk.get("type") or "alert") == "alert"
                primary, other = (alert_ci, insight_ci) if is_alert else (insight_ci, alert_ci)
                n = int(primary.get(key, 0) or other.get(key, 0) or 0)
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
            cc_entry = self._cc.entry(_CC_SCOPE, wireless_site, central_site)
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
