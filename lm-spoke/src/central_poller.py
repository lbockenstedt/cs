"""Background Aruba Central poller for a standalone/hub-connected cs spoke.

Drives ``aruba.ArubaClient`` (vendored from ``solutions-hpe/webui-hub``'s
``app/aruba.py`` — see that file's docstring) against the sites configured in
``local_store.py``'s ``central_sites_config``, and assembles the result into
``spoke.central_status`` in the EXACT shape ``lm/WebUI/sim-views.js``'s
Simulations Checks/Hardware/Client-Count tabs already expect:

    {"status": {site: {check_id: {status, message}}},
     "hardware_alerts": [{id, name, device_type, total}],
     "client_count_status": {site: {current: N}}}

This closes the gap flagged when the Simulations tab first landed (Central
integration didn't exist in lm-spoke at all) — real polling now runs, sourced
from THIS spoke's own local Central credentials instead of an LM hub tenant
config.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from aruba import ArubaClient

logger = logging.getLogger("CentralPoller")

_POLL_INTERVAL_S = 300  # 5 min — matches aruba.py's own cache TTLs


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
            client_count_status[wireless_site] = {"current": data.get("client_count", 0)}
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

    # ── on-demand actions (Setup → Central API tab) ─────────────────────────

    async def available_checks(self) -> Dict[str, Any]:
        if not self._client:
            return {"status": "SUCCESS", "alerts": [], "insights": [], "hardware": [],
                    "warning": "Central not configured."}
        result = await self._client.available_checks()
        return {"status": "SUCCESS", **result}

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
