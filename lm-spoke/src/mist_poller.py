"""Background Juniper Mist poller for a standalone/hub-connected cs spoke.

Drives ``mist.MistClient`` against the sites configured in ``local_store.py``'s
``mist_sites_config``, and assembles the result into ``spoke.mist_status`` in
the EXACT shape ``lm/WebUI/sim-views.js``'s Simulations Checks/Hardware/
Client-Count tabs already expect (the same ``central_status`` shape the Aruba
``CentralPoller`` writes):

    {"status": {site: {check_id: {status, message}}},
     "hardware_alerts": [{id, name, device_type, total}],
     "client_count_status": {site: {current, hourly_avg, drop_pct, status, ...}}}

This is a near-twin of ``central_poller.CentralPoller`` — it MIRRORS (does NOT
import) the data-source-agnostic ``MistClientCountTracker`` /
``MistCheckHealthHistory`` / ``_mist_cc_thresholds`` / ``_mist_cc_worst``
helpers from the Mist-owned ``mist_tracker`` so the client-count baseline +
30-day health history logic is duplicated, not shared across products (Central
and Mist are separate products and must not share code). Only the data source
(MistClient vs ArubaClient) and the config keys (mist_config /
mist_sites_config vs central_config / central_sites_config) differ, plus
separate on-disk baseline/history files so Mist and Central never share a
baseline.

``alert_type_counts`` keys are the BARE Mist alarm ``type`` (no ``Mist:`` prefix)
— the prefix is applied only in the sim-quota catalog layer (Setup → Sim Quotas),
never on the dashboard or in reports. See ``mist.MistClient``'s module docstring.
"""
# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# asyncio runs the background poll loop.
import asyncio
# Standard-library logging for poll results and failures.
import logging
# Builds file paths for the baseline/history files.
import os
# Timestamp for the published status.
import time
# Type hints only.
from typing import Any, Dict, Optional

# Async client that talks to the Juniper Mist cloud API.
from mist import MistClient
# Shared helpers that match monitored checks against alert/insight counts.
from check_eval import count_for_check, normalize_counts
# MIRROR (not import) the client-count baseline tracker, 30-day health history,
# threshold resolver + worst-of selector, and single-tenant scope key from the
# Mist-owned mist_tracker — Central and Mist are separate products and must not
# share code. check_eval stays shared: it is a data-source-neutral matcher used
# across the whole sim system, not Central-specific code.
# Mist-specific client-count tracker, health history and threshold helpers (see above).
from mist_tracker import (
    MistClientCountTracker, MistCheckHealthHistory,
    _mist_cc_thresholds, _mist_cc_worst, _mist_cc_site_floor, _MIST_CC_SCOPE,
)

# Dedicated logger for the Mist poller.
logger = logging.getLogger("MistPoller")

# Poll every 5 minutes (300 s), matching Mist client cache lifetimes.
_POLL_INTERVAL_S = 300  # 5 min — matches mist.py's own cache TTLs


def _build_mist_config(mist_config: Dict[str, Any]) -> Dict[str, Any]:
    """Pass the stored ``mist_config`` through to ``MistClient``. Mist has no
    mode→api_version remap (unlike Aruba's classic/new_central); the token +
    org_id + host map 1:1."""
    # Return a copy so later changes do not touch the stored config.
    return dict(mist_config)


def client_os_counts(clients) -> Dict[str, int]:
    """``{os_label: count}`` for a browse client list, biggest first.

    DUPLICATED from central_poller on purpose: Mist mirrors Central but must
    never import it (test_mist_tracker enforces this — the two data sources stay
    independently evolvable). Keep the two copies behaviourally identical."""
    # Running count per OS label.
    counts: Dict[str, int] = {}
    # Walk every client (tolerating None).
    for c in (clients or []):
        try:
            # Read the client's OS text.
            raw = str((c or {}).get("os") or "").strip()
        # Malformed client entry: treat its OS as empty.
        except Exception:  # noqa: BLE001
            raw = ""
        # Blank values or the em-dash placeholder count as "Unknown".
        label = raw if raw and raw != "\u2014" else "Unknown"
        # Increment the count for this label.
        counts[label] = counts.get(label, 0) + 1
    # Sort by count (descending) then name (case-insensitive) and return.
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())))


class MistPoller:
    """Drives ``MistClient`` on a 5-minute loop, writing ``spoke.mist_status`` in
    the shape ``sim-views.js``'s Checks/Hardware/Client-Count tabs expect. No-op
    (empty ``mist_status``) when Mist is not configured. See the module docstring."""

    def __init__(self, spoke) -> None:
        """Set up the trackers and build the Mist client from stored config."""
        # The owning spoke; provides the local store and receives mist_status.
        self.spoke = spoke
        # Mist API client (None until configured) and the background loop task.
        self._client: Optional[MistClient] = None
        self._task: Optional[asyncio.Task] = None
        # Separate baseline/history files so Mist + Central never share a series.
        # Directory where the spoke keeps its local data files.
        ddir = str(spoke.local_store._path.parent)
        # Client-count tracker with Mist-only baseline and 7-day files.
        self._cc = MistClientCountTracker(
            os.path.join(ddir, "mist_client_count_baseline.json"),
            os.path.join(ddir, "mist_client_count_7day.json"),
        )
        # 30-day per-check health history in its own file.
        self._health = MistCheckHealthHistory(os.path.join(ddir, "mist_check_health_history.json"))
        # Build the Mist client from the stored config.
        self.reload()

    # ── (re)build the MistClient from the current stored config ─────────────
    def reload(self) -> None:
        """Rebuild the Mist client from the currently stored config."""
        # Read the stored Mist config.
        cfg = _build_mist_config(self.spoke.local_store.get_mist_config())
        # Create a client only when a token and org id are set; otherwise disable polling.
        self._client = MistClient(cfg) if (cfg.get("api_token") and cfg.get("org_id")) else None

    def start(self) -> None:
        """Spawn the 5-min poll loop on the running event loop. Cancels any prior
        task first (idempotent). No-op with a warning when no loop is running yet
        (callers without a loop use the FastAPI ``startup`` hook)."""
        # Cancel a previous loop if it is still running.
        if self._task and not self._task.done():
            self._task.cancel()
        # Find the running event loop.
        try:
            loop = asyncio.get_running_loop()
        # No loop yet: log and let the startup hook start it later.
        except RuntimeError:
            logger.warning("Event loop not running; Mist poll loop deferred.")
            return
        # Start the poll loop as a background task.
        self._task = loop.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll Mist every _POLL_INTERVAL_S seconds until cancelled."""
        # Repeat forever.
        while True:
            try:
                # Run one poll.
                await self._poll_once()
            # Cancellation means shutdown: let it propagate.
            except asyncio.CancelledError:
                raise
            # Any other error is logged; the loop continues.
            except Exception as exc:  # noqa: BLE001 — never let a bad poll kill the loop
                logger.warning("Mist poll failed: %s", exc)
            # Wait before the next poll.
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        """Poll every mapped Mist site once and publish ``spoke.mist_status``."""
        # Ask the local store whether the hub is polling Mist centrally.
        hub_polls = getattr(self.spoke.local_store, "hub_polls", None)
        if callable(hub_polls) and hub_polls("mist_api"):
            # Hub (centralized) mode: the hub's MistHubPoller owns this API.
            # Log the hub-mode skip only once, not every 5 minutes.
            if not getattr(self, "_hub_mode_logged", False):
                logger.info("mist poll skipped: hub polls in centralized mode")
                self._hub_mode_logged = True
            # Clear local status; the hub provides it instead.
            self.spoke.mist_status = {}
            return
        # Back in local mode: allow the hub-mode message to log again next time.
        self._hub_mode_logged = False
        # Mist not configured: publish an empty status.
        if not self._client or not self._client.is_configured():
            self.spoke.mist_status = {}
            return
        # Client-count thresholds from the Mist config.
        cc_thresh = _mist_cc_thresholds(self.spoke.local_store.get_mist_config())
        # Site settings: wireless-site -> Mist-site map, monitored checks, hardware checks.
        sites_cfg = self.spoke.local_store.get_mist_sites_config()
        site_mappings: Dict[str, str] = sites_cfg.get("site_mappings") or {}
        monitored: list = sites_cfg.get("monitored_checks") or []
        hw_checks: list = sites_cfg.get("hardware_checks") or []
        # Set of hardware alert ids to count per site.
        hw_check_ids = {str(h.get("id")) for h in hw_checks if h.get("id")}

        # Results being built: per-site check status, client counts, hardware totals.
        status: Dict[str, Dict[str, Any]] = {}
        client_count_status: Dict[str, Any] = {}
        hw_totals: Dict[str, int] = {}
        # Hardware check definitions by id, for display names.
        hw_names = {str(h.get("id")): h for h in hw_checks if h.get("id")}

        # Poll each mapped site (wireless site name -> Mist site name).
        for wireless_site, mist_site in site_mappings.items():
            try:
                # Fetch alerts, insights, client counts and hardware data for the site.
                data = await self._client.poll_site_data(mist_site, hw_check_ids)
            # A failed site is shown as a single error check and skipped.
            except Exception as exc:  # noqa: BLE001
                status[wireless_site] = {"poll_error": {"status": "error", "message": str(exc)}}
                continue
            # Alert-type and insight-category counts for the site.
            alert_counts = data.get("alert_type_counts") or {}
            insight_counts = data.get("insight_cat_counts") or {}
            # Match case-insensitively across BOTH the alert and insight buckets
            # (shared with Central via check_eval — single source of truth).
            # Case-insensitive versions of both count tables.
            alert_ci, insight_ci = normalize_counts(alert_counts), normalize_counts(insight_counts)
            # Diagnostic log: which checks are monitored and which keys Mist returned.
            logger.info("mist-check diag [%s→%s]: monitored=%s alert_keys=%s insight_keys=%s",
                        wireless_site, mist_site,
                        [str(c.get("id")) for c in monitored if isinstance(c, dict) and c.get("id")],
                        sorted(alert_ci), sorted(insight_ci))
            # Per-check results for this site.
            checks: Dict[str, Any] = {}
            # Evaluate each monitored check.
            for chk in monitored:
                # Skip checks without an id.
                cid = str(chk.get("id") or "")
                if not cid:
                    continue
                # Skip checks pinned to a different site ("all sites" applies everywhere).
                chk_site = str(chk.get("site") or "").strip().lower()
                if chk_site and chk_site not in (str(mist_site).lower(), str(wireless_site).lower(), "all sites"):
                    continue
                # How many occurrences of this check Mist reports.
                n = count_for_check(chk, alert_ci, insight_ci)
                # INVERTED semantics (same as Central): a monitored check is
                # HEALTHY when its error IS present, FAILING when it is NOT.
                # Present means the simulation is working.
                if n > 0:
                    checks[cid] = {"status": "ok", "message": f"{n} active (as expected)"}
                # Absent: explain the likely cause.
                else:
                    # Surface WHY the expected error is absent (mirrors hub twin):
                    # site returned NOTHING → site dropped / name mismatch; vs the
                    # site reporting other conditions → sim not firing THIS alert.
                    # How many conditions the site reported at all, and its client count.
                    _seen = len(alert_ci) + len(insight_ci)
                    _cc = int(data.get("client_count", 0) or 0)
                    # Nothing at all: the site may be missing or misnamed.
                    if _seen == 0:
                        _msg = (f"Expected error NOT detected — site returned no "
                                f"alerts/insights ({_cc} clients; site dropped or "
                                f"name mismatch?)")
                    # Other conditions exist: the simulation is not producing this one.
                    else:
                        _msg = (f"Expected error NOT detected — sim not firing "
                                f"(0 seen; {_seen} other condition(s) at site, "
                                f"{_cc} clients)")
                    # Record the failing check.
                    checks[cid] = {"status": "error", "message": _msg}
            # Store this site's check results.
            status[wireless_site] = checks
            # Total, wired and wireless client counts.
            current = int(data.get("client_count", 0) or 0)
            wired = int(data.get("wired_clients", 0) or 0)
            wireless = int(data.get("wireless_clients", 0) or 0)
            # Add them to the client-count history.
            self._cc.record(_MIST_CC_SCOPE, wireless_site, current)
            self._cc.record(_MIST_CC_SCOPE, wireless_site, wired, kind="wired")
            self._cc.record(_MIST_CC_SCOPE, wireless_site, wireless, kind="wireless")
            # Per-site FLOOR, resolved here because entry() only sees thresholds.
            # Copy the thresholds and add this site's minimum-client floor.
            cc_thresh = dict(cc_thresh or {})
            cc_thresh["floor"] = _mist_cc_site_floor(
                self.spoke.local_store.get_mist_config(), mist_site)
            # Compare current counts to the hourly averages (total, wired, wireless).
            cc_entry = self._cc.entry(_MIST_CC_SCOPE, wireless_site, mist_site, cc_thresh)
            w_entry = self._cc.entry(_MIST_CC_SCOPE, wireless_site, mist_site, cc_thresh, kind="wired")
            wl_entry = self._cc.entry(_MIST_CC_SCOPE, wireless_site, mist_site, cc_thresh, kind="wireless")
            # Add wired/wireless details to the total entry.
            cc_entry["wired"] = wired
            cc_entry["wireless"] = wireless
            cc_entry["wired_status"] = w_entry["status"]
            cc_entry["wired_drop_pct"] = w_entry["drop_pct"]
            cc_entry["wireless_status"] = wl_entry["status"]
            cc_entry["wireless_drop_pct"] = wl_entry["drop_pct"]
            # Overall status is the worst of the three.
            cc_entry["status"] = _mist_cc_worst(cc_entry["status"], w_entry["status"], wl_entry["status"])
            # Publish the client-count entry for this site.
            client_count_status[wireless_site] = cc_entry
            # Also show it as a check row on the Checks tab.
            checks["Steady Client Count 1hr Average"] = {
                "status": cc_entry["status"],
                "message": (f"{cc_entry['current']} clients vs {cc_entry['hourly_avg']} hr-avg "
                            f"(down {cc_entry['drop_pct']}%) · wired {wired} (down {w_entry['drop_pct']}%) "
                            f"· wireless {wireless} (down {wl_entry['drop_pct']}%)"),
            }
            # Add this site's hardware alert device counts to the running totals.
            for alert_id, devices in (data.get("hw_devices") or {}).items():
                hw_totals[alert_id] = hw_totals.get(alert_id, 0) + sum(devices.values())

        # Per-device hardware monitoring: look each monitored hardware device up
        # in the live Mist inventory and add a check on its pinned site — DOWN =
        # error. Best-effort (Mist inventory has no live status for some types).
        # Only when hardware checks are configured.
        if hw_checks:
            # Fetch the Mist device inventory; failure means no devices found.
            try:
                all_devices = await self._client._list_inventory()
            except Exception:  # noqa: BLE001
                all_devices = []
            # Index devices by serial, MAC, name and id so a check can use any of them.
            dev_by_key: Dict[str, dict] = {}
            for d in all_devices:
                for k in (d.get("serial"), d.get("mac"), d.get("name"), d.get("id")):
                    if k:
                        dev_by_key[str(k)] = d
            # Evaluate each hardware check.
            for hc in hw_checks:
                # Skip checks without an id.
                hid = str(hc.get("id") or "")
                if not hid:
                    continue
                # Site the check is pinned to, the device, and whether it is up.
                hsite = str(hc.get("site") or "").strip().lower()
                dev = dev_by_key.get(hid)
                up = bool((dev or {}).get("connected")) or str((dev or {}).get("status") or "").lower() in ("up", "online", "connected")
                # Name shown for the check.
                label = str(hc.get("name") or hid)
                # Add the check to every matching mapped site.
                for wsite, csite in site_mappings.items():
                    # Skip sites the check is not pinned to.
                    if hsite and hsite not in (str(csite).lower(), str(wsite).lower(), "all sites"):
                        continue
                    # Record up (ok) or DOWN (error).
                    status.setdefault(wsite, {})[label] = {
                        "status": "ok" if up else "error",
                        "message": "up" if up else "DOWN",
                    }

        # Hardware alert rows with friendly names and totals.
        hardware_alerts = [
            {"id": aid, "name": (hw_names.get(aid) or {}).get("name", aid),
             "device_type": (hw_names.get(aid) or {}).get("device_type", ""),
             "total": total}
            for aid, total in hw_totals.items()
        ]
        # Record every check result in the 30-day health history.
        for wsite, checks_map in status.items():
            # Skip non-dict entries.
            if not isinstance(checks_map, dict):
                continue
            # Each check's status ("no_data" when missing).
            for cid, info in checks_map.items():
                st = (info.get("status") if isinstance(info, dict) else info) or "no_data"
                self._health.record(_MIST_CC_SCOPE, wsite, cid, st)
        # Publish the assembled status for the UI.
        self.spoke.mist_status = {
            "status": status,
            "hardware_alerts": hardware_alerts,
            "client_count_status": client_count_status,
            "health": self._health.summary(_MIST_CC_SCOPE),
            "fetched_at": time.time(),
        }
        # Periodically snapshot the client-count series and save the health history.
        self._cc.maybe_snapshot()
        self._health.save()

    # ── on-demand actions (Setup → Mist API tab) ───────────────────────────

    async def available_checks(self) -> Dict[str, Any]:
        """Return every alert, insight and hardware check Mist can provide."""
        # Not configured: return empty lists with a warning.
        if not self._client:
            return {"status": "SUCCESS", "alerts": [], "insights": [], "hardware": [],
                    "warning": "Mist not configured."}
        # Ask Mist and return the lists.
        result = await self._client.available_checks()
        return {"status": "SUCCESS", **result}

    async def browse(self) -> Dict[str, Any]:
        """On-demand FULL Mist inventory for the Mist → Sites/Alerts/Clients
        tabs — every site, alarm, insight and client from Mist, independent of
        site_mappings. Cached inside the client (5-min per endpoint)."""
        # Not configured: return empty data with a warning.
        if not self._client or not self._client.is_configured():
            return {"status": "SUCCESS", "sites": [], "alerts": [], "insights": [],
                    "clients": [], "devices_by_site": {}, "clients_by_site": {},
                    "os_counts": {}, "warning": "Mist not configured."}
        # Fetch everything and add the client OS breakdown.
        try:
            data = await self._client.browse_all()
            return {"status": "SUCCESS", **data,
                    "os_counts": client_os_counts(data.get("clients"))}
        # Log and report failures.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mist browse failed [%s]: %s", self.spoke.spoke_id, exc)
            return {"status": "ERROR", "message": str(exc),
                    "sites": [], "alerts": [], "insights": [], "clients": [],
                    "os_counts": {}}

    async def test_connection(self) -> Dict[str, Any]:
        """Best-effort connectivity check for the Setup → Mist API "Test" button.
        Mirrors the hub's test-mist route shape (``{spokes: [...]}``) with a
        single entry describing this spoke."""
        # Not configured: report that in the expected shape.
        if not self._client or not self._client.is_configured():
            logger.info("test_connection [%s]: Mist not configured (no api_token/org_id)",
                        self.spoke.spoke_id)
            return {"status": "SUCCESS", "spokes": [{
                "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
                "token_state": None, "token_valid": False,
                "status": "Mist not configured.",
            }]}
        # Config fingerprint used in log messages.
        chash = getattr(self._client, "_config_hash", "?")
        # Run the client's own connection test.
        try:
            result = await self._client.test_connection()
            # MistClient.test_connection already returns the {spokes:[...]} shape.
            return result
        # Unexpected failure: log it and report it in the expected shape.
        except Exception as exc:  # noqa: BLE001
            logger.warning("test_connection [%s] cfg=%s FAILED: %r",
                           self.spoke.spoke_id, chash, exc)
            return {"status": "SUCCESS", "spokes": [{
                "spoke_id": self.spoke.spoke_id, "spoke_name": self.spoke.spoke_id,
                "token_state": None, "token_valid": False,
                "status": f"Connection failed: {exc}",
            }]}