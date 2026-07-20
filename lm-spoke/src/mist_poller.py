"""Background Juniper Mist poller for a standalone/hub-connected cs spoke.

Drives ``mist.MistClient`` against the sites configured in ``local_store.py``'s
``mist_sites_config``, and assembles the result into ``spoke.mist_status`` in
the EXACT shape ``lm/WebUI/sim-views.js``'s Simulations Checks/Hardware/
Client-Count tabs already expect (the same ``central_status`` shape the Aruba
``CentralPoller`` writes):

    {"status": {site: {check_id: {status, message}}},
     "hardware_alerts": [{id, name, device_type, total}],
     "client_count_status": {site: {current, hourly_avg, drop_pct, status, ...}}}

This is a near-twin of ``central_poller.CentralPoller`` — it REUSES the
data-source-agnostic ``ClientCountTracker`` / ``CheckHealthHistory`` /
``_cc_thresholds`` / ``_cc_worst`` helpers from ``central_poller`` so the
client-count baseline + 30-day health history logic is shared verbatim (single
source of truth), not forked. Only the data source (MistClient vs ArubaClient)
and the config keys (mist_config / mist_sites_config vs central_config /
central_sites_config) differ, plus separate on-disk baseline/history files so
Mist and Central never share a baseline.

``alert_type_counts`` keys are the BARE Mist alarm ``type`` (no ``Mist:`` prefix)
— the prefix is applied only in the sim-quota catalog layer (Setup → Sim Quotas),
never on the dashboard or in reports. See ``mist.MistClient``'s module docstring.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from mist import MistClient
from check_eval import count_for_check, normalize_counts
# Reuse the data-source-agnostic helpers from the Aruba Central poller — the
# client-count baseline tracker, the 30-day health history, the threshold
# resolver + worst-of selector, and the single-tenant scope key. Sharing them
# (vs forking) keeps the drop-detection + health-graph logic in ONE place.
from central_poller import (
    ClientCountTracker, CheckHealthHistory, _cc_thresholds, _cc_worst, _CC_SCOPE,
)

logger = logging.getLogger("MistPoller")

_POLL_INTERVAL_S = 300  # 5 min — matches mist.py's own cache TTLs


def _build_mist_config(mist_config: Dict[str, Any]) -> Dict[str, Any]:
    """Pass the stored ``mist_config`` through to ``MistClient``. Mist has no
    mode→api_version remap (unlike Aruba's classic/new_central); the token +
    org_id + host map 1:1."""
    return dict(mist_config)


class MistPoller:
    """Drives ``MistClient`` on a 5-minute loop, writing ``spoke.mist_status`` in
    the shape ``sim-views.js``'s Checks/Hardware/Client-Count tabs expect. No-op
    (empty ``mist_status``) when Mist is not configured. See the module docstring."""

    def __init__(self, spoke) -> None:
        self.spoke = spoke
        self._client: Optional[MistClient] = None
        self._task: Optional[asyncio.Task] = None
        # Separate baseline/history files so Mist + Central never share a series.
        ddir = str(spoke.local_store._path.parent)
        self._cc = ClientCountTracker(
            os.path.join(ddir, "mist_client_count_baseline.json"),
            os.path.join(ddir, "mist_client_count_7day.json"),
        )
        self._health = CheckHealthHistory(os.path.join(ddir, "mist_check_health_history.json"))
        self.reload()

    # ── (re)build the MistClient from the current stored config ─────────────
    def reload(self) -> None:
        cfg = _build_mist_config(self.spoke.local_store.get_mist_config())
        self._client = MistClient(cfg) if (cfg.get("api_token") and cfg.get("org_id")) else None

    def start(self) -> None:
        """Spawn the 5-min poll loop on the running event loop. Cancels any prior
        task first (idempotent). No-op with a warning when no loop is running yet
        (callers without a loop use the FastAPI ``startup`` hook)."""
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Event loop not running; Mist poll loop deferred.")
            return
        self._task = loop.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let a bad poll kill the loop
                logger.warning("Mist poll failed: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        if not self._client or not self._client.is_configured():
            self.spoke.mist_status = {}
            return
        cc_thresh = _cc_thresholds(self.spoke.local_store.get_mist_config())
        sites_cfg = self.spoke.local_store.get_mist_sites_config()
        site_mappings: Dict[str, str] = sites_cfg.get("site_mappings") or {}
        monitored: list = sites_cfg.get("monitored_checks") or []
        hw_checks: list = sites_cfg.get("hardware_checks") or []
        hw_check_ids = {str(h.get("id")) for h in hw_checks if h.get("id")}

        status: Dict[str, Dict[str, Any]] = {}
        client_count_status: Dict[str, Any] = {}
        hw_totals: Dict[str, int] = {}
        hw_names = {str(h.get("id")): h for h in hw_checks if h.get("id")}

        for wireless_site, mist_site in site_mappings.items():
            try:
                data = await self._client.poll_site_data(mist_site, hw_check_ids)
            except Exception as exc:  # noqa: BLE001
                status[wireless_site] = {"poll_error": {"status": "error", "message": str(exc)}}
                continue
            alert_counts = data.get("alert_type_counts") or {}
            insight_counts = data.get("insight_cat_counts") or {}
            # Match case-insensitively across BOTH the alert and insight buckets
            # (shared with Central via check_eval — single source of truth).
            alert_ci, insight_ci = normalize_counts(alert_counts), normalize_counts(insight_counts)
            logger.info("mist-check diag [%s→%s]: monitored=%s alert_keys=%s insight_keys=%s",
                        wireless_site, mist_site,
                        [str(c.get("id")) for c in monitored if isinstance(c, dict) and c.get("id")],
                        sorted(alert_ci), sorted(insight_ci))
            checks: Dict[str, Any] = {}
            for chk in monitored:
                cid = str(chk.get("id") or "")
                if not cid:
                    continue
                chk_site = str(chk.get("site") or "").strip().lower()
                if chk_site and chk_site not in (str(mist_site).lower(), str(wireless_site).lower(), "all sites"):
                    continue
                n = count_for_check(chk, alert_ci, insight_ci)
                # INVERTED semantics (same as Central): a monitored check is
                # HEALTHY when its error IS present, FAILING when it is NOT.
                checks[cid] = {"status": "ok" if n > 0 else "error",
                               "message": f"{n} active (as expected)" if n else "Expected error NOT detected"}
            status[wireless_site] = checks
            current = int(data.get("client_count", 0) or 0)
            wired = int(data.get("wired_clients", 0) or 0)
            wireless = int(data.get("wireless_clients", 0) or 0)
            self._cc.record(_CC_SCOPE, wireless_site, current)
            self._cc.record(_CC_SCOPE, wireless_site, wired, kind="wired")
            self._cc.record(_CC_SCOPE, wireless_site, wireless, kind="wireless")
            cc_entry = self._cc.entry(_CC_SCOPE, wireless_site, mist_site, cc_thresh)
            w_entry = self._cc.entry(_CC_SCOPE, wireless_site, mist_site, cc_thresh, kind="wired")
            wl_entry = self._cc.entry(_CC_SCOPE, wireless_site, mist_site, cc_thresh, kind="wireless")
            cc_entry["wired"] = wired
            cc_entry["wireless"] = wireless
            cc_entry["wired_status"] = w_entry["status"]
            cc_entry["wired_drop_pct"] = w_entry["drop_pct"]
            cc_entry["wireless_status"] = wl_entry["status"]
            cc_entry["wireless_drop_pct"] = wl_entry["drop_pct"]
            cc_entry["status"] = _cc_worst(cc_entry["status"], w_entry["status"], wl_entry["status"])
            client_count_status[wireless_site] = cc_entry
            checks["Steady Client Count 1hr Average"] = {
                "status": cc_entry["status"],
                "message": (f"{cc_entry['current']} clients vs {cc_entry['hourly_avg']} hr-avg "
                            f"(down {cc_entry['drop_pct']}%) · wired {wired} (down {w_entry['drop_pct']}%) "
                            f"· wireless {wireless} (down {wl_entry['drop_pct']}%)"),
            }
            for alert_id, devices in (data.get("hw_devices") or {}).items():
                hw_totals[alert_id] = hw_totals.get(alert_id, 0) + sum(devices.values())

        # Per-device hardware monitoring: look each monitored hardware device up
        # in the live Mist inventory and add a check on its pinned site — DOWN =
        # error. Best-effort (Mist inventory has no live status for some types).
        if hw_checks:
            try:
                all_devices = await self._client._list_inventory()
            except Exception:  # noqa: BLE001
                all_devices = []
            dev_by_key: Dict[str, dict] = {}
            for d in all_devices:
                for k in (d.get("serial"), d.get("mac"), d.get("name"), d.get("id")):
                    if k:
                        dev_by_key[str(k)] = d
            for hc in hw_checks:
                hid = str(hc.get("id") or "")
                if not hid:
                    continue
                hsite = str(hc.get("site") or "").strip().lower()
                dev = dev_by_key.get(hid)
                up = bool((dev or {}).get("connected")) or str((dev or {}).get("status") or "").lower() in ("up", "online", "connected")
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
        for wsite, checks_map in status.items():
            if not isinstance(checks_map, dict):
                continue
            for cid, info in checks_map.items():
                st = (info.get("status") if isinstance(info, dict) else info) or "no_data"
                self._health.record(_CC_SCOPE, wsite, cid, st)
        self.spoke.mist_status = {
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
            "health": self._health.summary(_CC_SCOPE),
            "fetched_at": time.time(),
        }
        self._cc.maybe_snapshot()
        self._health.save()

    # ── on-demand actions (Setup → Mist API tab) ───────────────────────────

    async def available_checks(self) -> Dict[str, Any]:
        if not self._client:
            return {"status": "SUCCESS", "alerts": [], "insights": [], "hardware": [],
                    "warning": "Mist not configured."}
        result = await self._client.available_checks()
        return {"status": "SUCCESS", **result}

    async def browse(self) -> Dict[str, Any]:
        """On-demand FULL Mist inventory for the Mist → Sites/Alerts/Clients
        tabs — every site, alarm, insight and client from Mist, independent of
        site_mappings. Cached inside the client (5-min per endpoint)."""
        if not self._client or not self._client.is_configured():
            return {"status": "SUCCESS", "sites": [], "alerts": [], "insights": [],
                    "clients": [], "devices_by_site": {}, "clients_by_site": {},
                    "warning": "Mist not configured."}
        try:
            data = await self._client.browse_all()
            return {"status": "SUCCESS", **data}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mist browse failed [%s]: %s", self.spoke.spoke_id, exc)
            return {"status": "ERROR", "message": str(exc),
                    "sites": [], "alerts": [], "insights": [], "clients": []}

    async def test_connection(self) -> Dict[str, Any]:
        """Best-effort connectivity check for the Setup → Mist API "Test" button.
        Mirrors the hub's test-mist route shape (``{spokes: [...]}``) with a
        single entry describing this spoke."""
        if not self._client or not self._client.is_configured():
            logger.info("test_connection [%s]: Mist not configured (no api_token/org_id)",
                        self.spoke.spoke_id)
            return {"status": "SUCCESS", "spokes": [{
                "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
                "token_state": None, "token_valid": False,
                "status": "Mist not configured.",
            }]}
        chash = getattr(self._client, "_config_hash", "?")
        try:
            result = await self._client.test_connection()
            # MistClient.test_connection already returns the {spokes:[...]} shape.
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("test_connection [%s] cfg=%s FAILED: %r",
                           self.spoke.spoke_id, chash, exc)
            return {"status": "SUCCESS", "spokes": [{
                "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
                "token_state": None, "token_valid": False,
                "status": f"Connection failed: {exc}",
            }]}