"""Aruba API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so hints like "str | None" are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Shared helpers, state and third-party modules all come from server.py so the logic stays in
# one place; this file only holds the HTTP route functions.
from server import (
    Any,
    Body,
    Depends,
    HTTPException,
    NC_BROWSE_SERVER_CACHE_TTL_S,
    Query,
    SpokeUser,
    _can_refresh,
    _central_cfg,
    _central_headers,
    _central_ready,
    _central_status_payload,
    _central_token_state,
    _client_count_payload,
    _fetch_central_token,
    _fetch_nc_browse_for_spoke,
    _get_cached_settings,
    _hw_alerts_payload,
    _is_new_central_api,
    _persisted,
    _poll_central_once,
    _public_central_api_settings,
    _refresh_central_token,
    _save_settings,
    _test_classic_central_connection,
    asyncio,
    broadcast,
    central_token,
    history_lock,
    httpx,
    logger,
    require_auth,
    settings,
    state,
    time,
    uuid,
)

# The router object every endpoint below is registered on.
router = APIRouter()




# ── Aruba Central API endpoints ───────────────────────────────────────────────
@router.post("/api/central/test-connection")
async def api_central_test() -> dict[str, Any]:
    """Test the configured Central credentials and report success or the failure reason."""
    # Classic Central uses a different auth flow than New Central, so branch on the mode.
    mode = settings.get("central_api", {}).get("mode", "classic")
    # One HTTP client for the whole test.
    async with httpx.AsyncClient() as client:
        # Classic mode: call the classic test helper.
        if mode == "classic":
            ok, detail_msg = await _test_classic_central_connection(client)
            if ok:
                return {"status": "ok", "message": detail_msg}
            # Upstream/auth problems map to 502 (bad gateway); everything else to 422 (bad input).
            raise HTTPException(status_code=502 if "HTTP" in detail_msg or "rejected" in detail_msg else 422, detail=detail_msg)

        # New Central: credentials must be filled in before a token can be requested.
        if not _central_ready():
            raise HTTPException(
                status_code=422,
                detail="Central API not configured — enter URL, Client ID, and Client Secret in Setup.",
            )
        # Try to obtain an OAuth token with the saved credentials.
        ok, detail_msg = await _fetch_central_token(client)
    # A token came back, so the connection works.
    if ok:
        return {
            "status": "ok",
            "message": "Connected to Central API successfully.",
        }
    # Token request failed: report the upstream error as 502.
    raise HTTPException(status_code=502, detail=detail_msg)




@router.get("/api/central/available")
async def api_central_available() -> dict[str, Any]:
    """Return available alert types and insight categories from Central. Always returns 200."""
    # Without configuration there is nothing to query; return an empty list plus a hint.
    if not _central_ready():
        return {"alerts": [], "insights": [], "warning": "Central not configured."}

    # New Central v1alpha1 has no alerts/insights endpoints — return static synthetic checks.
    # These correspond to the metrics _poll_central_once() derives from sites-health, /aps, and /devices.
    # No live token is required to return this static list.
    if _is_new_central_api():
        return {
            "alerts": [
                {"id": "SITE_HEALTH",    "name": "Site Health Score (0–100)"},
                {"id": "AP_COUNT",       "name": "Total AP Count"},
                {"id": "AP_DOWN",        "name": "APs Down / Offline"},
                {"id": "SWITCH_DOWN",    "name": "Switches Down / Offline"},
                {"id": "GATEWAY_DOWN",   "name": "Gateways Down / Offline"},
                {"id": "CLIENT_COUNT",   "name": "Connected Client Count"},
            ],
            "insights": [],
            "warning": None,
        }

    # Classic mode needs a valid token to query the live API.
    if not central_token.get("access_token"):
        return {"alerts": [], "insights": [], "warning": "No valid token — save & test connection first."}

    # Static fallback list of well-known Aruba Central alert types (used when no live alerts exist)
    KNOWN_ALERT_TYPES: dict[str, str] = {
        "AP_DOWN": "AP Down",
        "AP_UP": "AP Up",
        "ACCESS_POINT_DOWN": "Access Point Down",
        "CLIENT_ASSOCIATION_FAILURE": "Client Association Failure",
        "CLIENT_DHCP_FAILURE": "Client DHCP Failure",
        "CLIENT_DISCONNECTED": "Client Disconnected",
        "DHCP_POOL_EXHAUSTED": "DHCP Pool Exhausted",
        "IDS_AP_SPOOFED": "IDS AP Spoofed",
        "PORTAL_DOWN": "Portal Down",
        "RADIO_INTERFERENCE": "Radio Interference",
        "ROGUE_AP_DETECTED": "Rogue AP Detected",
        "SWITCH_DOWN": "Switch Down",
        "SWITCH_PORT_DOWN": "Switch Port Down",
        "TUNNEL_DOWN": "Tunnel Down",
        "UPLINK_FAILURE": "Uplink Failure",
        "VPN_TUNNEL_DOWN": "VPN Tunnel Down",
        "WIRELESS_CLIENT_ROAM": "Wireless Client Roam",
        "WIRELESS_INTERFERENCE": "Wireless Interference",
    }
    KNOWN_INSIGHT_CATEGORIES: dict[str, str] = {
        "CONNECTIVITY": "Connectivity",
        "PERFORMANCE": "Performance",
        "RELIABILITY": "Reliability",
        "SECURITY": "Security",
    }

    # Auth headers and API base URL for the configured cluster.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    # Results collected from the live API, plus any non-fatal warnings for the UI.
    alert_types: dict[str, str] = {}
    insight_categories: dict[str, str] = {}
    warnings: list[str] = []

    # 30-day lookback window to catch historical alert types even when none are active now
    thirty_days_ago = int(time.time()) - 30 * 86400

    async with httpx.AsyncClient() as client:
        # Alerts — try v1 then v2 (v2 is 404 on some clusters)
        for alerts_path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
            # Ask for up to 1000 alerts from the last 30 days.
            try:
                resp = await client.get(
                    f"{base_url}{alerts_path}",
                    headers=headers,
                    params={"limit": 1000, "from_timestamp": thirty_days_ago},
                    timeout=20,
                )
                logger.info("Central available alerts %s → %s", alerts_path, resp.status_code)
                # Success: collect each alert type id with a readable name.
                if resp.status_code == 200:
                    for alert in resp.json().get("alerts", []):
                        atype = alert.get("alert_type") or alert.get("type", "")
                        # Use the API's display name, or derive one from the type id (AP_DOWN -> Ap
                        # Down).
                        aname = alert.get("alert_type_name") or atype.replace("_", " ").title()
                        if atype:
                            alert_types[atype] = aname
                    break  # success — stop trying
                if resp.status_code == 404:
                    continue  # try next path
                # Token rejected: record a warning and stop trying.
                if resp.status_code == 401:
                    warnings.append("Token rejected (401) fetching alerts.")
                    break
            except Exception as exc:
                # Network errors are reported as a warning; the static list below still fills the
                # menu.
                logger.warning("Could not fetch alert types from %s: %s", alerts_path, exc)
                warnings.append(f"Network error fetching alerts: {exc}")
                break

        # Insights
        try:
            resp = await client.get(
                f"{base_url}/aiops/v1/insights",
                headers=headers,
                params={"limit": 1000, "from_timestamp": thirty_days_ago},
                timeout=20,
            )
            logger.info("Central available insights → %s", resp.status_code)
            # Collect each insight category id with a readable name.
            if resp.status_code == 200:
                for insight in resp.json().get("insights", []):
                    cat = insight.get("category") or insight.get("type", "")
                    cat_name = insight.get("category_name") or cat.replace("_", " ").title()
                    if cat:
                        insight_categories[cat] = cat_name
            elif resp.status_code not in (404,):
                # 404 just means insights are not available on this cluster; other codes are worth a
                # warning.
                warnings.append(f"Insights endpoint returned HTTP {resp.status_code}.")
        except Exception as exc:
            # Network errors become a warning; the static list fills the gap.
            logger.warning("Could not fetch insight categories: %s", exc)
            warnings.append(f"Network error fetching insights: {exc}")

    # If live API returned nothing, fall back to the known static list
    using_fallback = False
    if not alert_types:
        alert_types = dict(KNOWN_ALERT_TYPES)
        using_fallback = True
    if not insight_categories:
        insight_categories = dict(KNOWN_INSIGHT_CATEGORIES)
        using_fallback = True
    if using_fallback:
        warnings.append("No live checks returned by Central — showing standard Aruba Central check types.")

    # Return sorted id/name pairs and join any warnings into one message.
    return {
        "alerts": [{"id": k, "name": v} for k, v in sorted(alert_types.items())],
        "insights": [{"id": k, "name": v} for k, v in sorted(insight_categories.items())],
        "warning": "; ".join(warnings) if warnings else None,
    }




@router.get("/api/central/status")
async def api_central_status() -> dict[str, Any]:
    """Current check status for all mapped sites."""
    # Bundle every piece of Central monitoring state the UI shows in one response.
    return {
        "status": _central_status_payload(),
        "wireless_clients": dict(state.central_wireless_clients),
        "hardware_alerts": _hw_alerts_payload(),
        "client_count_status": _client_count_payload(),
        "site_mappings": settings.get("site_mappings", {}),
        "monitored_checks": settings.get("monitored_checks", []),
        "central_api": _public_central_api_settings(),
        # The token counts as valid if we have one and it has not expired yet.
        "token_valid": bool(central_token.get("access_token") and time.time() < central_token["expires_at"]),
        "token_state": _central_token_state(),
    }




@router.get("/api/central/history")
async def api_central_history(
    site: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=24),
) -> dict[str, Any]:
    """Return history records, optionally filtered by wsite."""
    # Only keep records newer than the requested number of hours.
    cutoff = time.time() - hours * 3600
    # Copy matching records under the history lock so the poller cannot change them mid-read.
    async with history_lock:
        records = [
            r for r in state.central_history
            if r["ts"] >= cutoff and (site is None or r["wsite"] == site)
        ]
    return {"records": records, "count": len(records)}




@router.get("/api/central/site-alerts")
async def api_central_site_alerts(site: str = Query(...)) -> dict[str, Any]:
    """Fetch current alerts from Central for a specific site name. Always returns 200."""
    # Need both configuration and a token to query Central.
    if not _central_ready() or not central_token.get("access_token"):
        return {"alerts": [], "warning": "Central not configured or no valid token."}

    # New Central branch.
    if _is_new_central_api():
        # New Central has no alerts endpoint — derive device-status alerts from /sites-health + /devices
        # Common request setup plus the result list and the timestamp used on synthetic alerts.
        headers = _central_headers()
        base_url = _central_cfg()["cluster_url"].rstrip("/")
        alerts: list[dict[str, Any]] = []
        warning: str | None = None
        ts_now = int(time.time())

        # One HTTP client for all New Central calls in this request.
        async with httpx.AsyncClient() as client:
            # 1. Find site_id from sites-health so we can filter devices by site
            site_id: str | None = None
            health_score: int | None = None
            # Find the requested site (case-insensitive) and remember its id and health score.
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            health_score = int(item.get("healthScore", item.get("health_score", 100)))
                            break
                elif resp.status_code == 401:
                    # Token rejected: tell the user to re-save settings.
                    warning = "Token rejected (401) — re-save settings."
            except Exception as exc:
                # Network failure: report it.
                warning = f"Network error fetching site health: {exc}"

            # Cannot continue without the site lookup, so return what we have with the warning.
            if warning:
                return {"alerts": alerts, "count": 0, "warning": warning}

            # 2. Add site health alert if score is degraded
            if health_score is not None and health_score < 100:
                # Map the score to a severity: below 50 critical, below 80 major, otherwise minor.
                severity = "CRITICAL" if health_score < 50 else "MAJOR" if health_score < 80 else "MINOR"
                alerts.append({
                    "type": "SITE_HEALTH",
                    "name": "Site Health Score",
                    "severity": severity,
                    "state": "active",
                    "site": site,
                    "device": site,
                    "ts": ts_now,
                    "message": f"Site health score is {health_score}/100",
                })

            # 3. Fetch devices for this site and add down devices as alerts
            # Ask for devices, filtered to the site with an OData filter when we know its id.
            try:
                params: dict[str, Any] = {"limit": 500}
                if site_id:
                    params["filter"] = f"siteId eq '{site_id}'"
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=20,
                )
                if resp.status_code == 200:
                    # Device type -> (alert type id, readable name) for a device that is down.
                    _TYPE_MAP = {
                        "ACCESS_POINT": ("AP_DOWN", "AP Down"),
                        "SWITCH": ("SWITCH_DOWN", "Switch Down"),
                        "GATEWAY": ("GATEWAY_DOWN", "Gateway Down"),
                    }
                    # Every device not reported up becomes a CRITICAL alert.
                    for dev in resp.json().get("items", []):
                        # Post-filter by siteId in case the API ignored the OData filter param
                        if site_id and dev.get("siteId") and dev.get("siteId") != site_id:
                            continue
                        # Devices reported up or online are healthy, so skip them.
                        status = (dev.get("status") or "").upper()
                        if status in ("UP", "ONLINE"):
                            continue
                        # Pick the alert type from the device type; unknown types become generic
                        # Device Down.
                        dtype = (dev.get("deviceType") or "").upper()
                        atype, aname = _TYPE_MAP.get(dtype, ("DEVICE_DOWN", "Device Down"))
                        alerts.append({
                            "type": atype,
                            "name": aname,
                            "severity": "CRITICAL",
                            "state": "active",
                            "site": site,
                            "device": dev.get("deviceName") or dev.get("id") or "—",
                            "ts": ts_now,
                            "message": f"{dev.get('model', dtype)} — status: {dev.get('status', 'Unknown')} | IP: {dev.get('ipv4') or dev.get('ip', '—')}",
                        })
            except Exception as exc:
                # A failure here is logged and returned as a warning, keeping any site-health alert.
                logger.warning("CNX devices fetch failed for site-alerts: %s", exc)
                warning = f"Could not fetch device status: {exc}"

        # Nothing found and no errors: explicitly tell the user everything is healthy.
        if not alerts and not warning:
            warning = "All devices are up and site health is 100% — no issues detected."

        return {"alerts": alerts, "count": len(alerts), "warning": warning}

    # Classic Central branch: same response shape, using the alerts API.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    alerts: list[dict[str, Any]] = []
    warning: str | None = None
    thirty_days_ago = int(time.time()) - 30 * 86400

    # Try the v1 alerts path first, then v2, because availability differs per cluster.
    async with httpx.AsyncClient() as client:
        for path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
            # Ask for up to 500 alerts for this site from the last 30 days.
            try:
                resp = await client.get(
                    f"{base_url}{path}",
                    headers=headers,
                    params={"site": site, "limit": 500, "from_timestamp": thirty_days_ago},
                    timeout=20,
                )
                logger.info("site-alerts %s for '%s' → %s", path, site, resp.status_code)
                # Success: normalise each alert into the UI's fields.
                if resp.status_code == 200:
                    for alert in resp.json().get("alerts", []):
                        # Skip alerts that belong to a different site, in case the API ignored the
                        # site filter.
                        alert_site = alert.get("site_name") or alert.get("site") or ""
                        if alert_site and site and alert_site.lower() != site.lower():
                            continue
                        alerts.append({
                            "type":     alert.get("alert_type") or alert.get("type", ""),
                            "name":     alert.get("alert_type_name") or alert.get("alert_type", ""),
                            "severity": alert.get("severity", ""),
                            "state":    alert.get("state", ""),
                            "site":     alert.get("site_name") or site,
                            "device":   alert.get("device_name") or alert.get("hostname", ""),
                            "ts":       alert.get("timestamp") or alert.get("raised_at", ""),
                            "message":  alert.get("details") or alert.get("description", ""),
                        })
                    # Got a valid answer, so do not try the other path.
                    break
                # Path not available on this cluster: try the next one.
                if resp.status_code == 404:
                    continue
                # Token rejected: warn and stop.
                if resp.status_code == 401:
                    warning = "Token rejected (401)."
                    break
            except Exception as exc:
                # Network failure: warn and stop.
                logger.warning("site-alerts fetch error: %s", exc)
                warning = str(exc)
                break

    # Make an empty result explicit in the UI.
    if not alerts and not warning:
        warning = "No alerts in the last 30 days for this site."

    return {"alerts": alerts, "count": len(alerts), "warning": warning}




@router.post("/api/central/poll")
async def api_central_poll() -> dict[str, Any]:
    """Trigger an immediate Central poll cycle."""
    # Polling needs configuration.
    if not _central_ready():
        raise HTTPException(status_code=422, detail="Central not configured.")
    async def _poll_with_client() -> None:
        """Run one poll using its own HTTP client so it can outlive this request."""
        async with httpx.AsyncClient() as client:
            await _poll_central_once(client)
    # Run the poll in the background and return immediately.
    asyncio.create_task(_poll_with_client())
    return {"status": "ok", "message": "Poll started."}




@router.get("/api/central/sites")
async def api_central_sites() -> dict[str, Any]:
    """Fetch site list from Aruba Central API. Always returns 200 with sites[] and optional warning."""
    # Need both configuration and a token before calling Central.
    if not _central_ready():
        return {"sites": [], "warning": "Central not configured — enter Cluster URL and token in Setup first."}
    if not central_token.get("access_token"):
        return {"sites": [], "warning": "No valid token — click 'Save & Test Connection' in Setup first."}

    # Request setup plus the results to return.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    sites: list[str] = []
    warning: str | None = None

    # Classic Central — try multiple known site endpoints
    CLASSIC_SITE_PATHS = [
        ("/monitoring/v2/sites", {"limit": 1000, "offset": 0}),
        ("/monitoring/v1/sites", {"limit": 1000, "offset": 0}),
        ("/central/v2/sites", {"limit": 1000, "offset": 0}),
    ]

    async with httpx.AsyncClient() as client:
        # New Central and Classic Central expose site lists differently.
        if _is_new_central_api():
            # New Central: sites come from sites-health
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers,
                    timeout=20,
                )
                logger.info("New Central sites-health → %s", resp.status_code)
                if resp.status_code == 200:
                    # Collect each site name from the health items.
                    for item in resp.json().get("items", []):
                        name = item.get("siteName") or item.get("site_name") or item.get("name", "")
                        if name:
                            sites.append(name)
                elif resp.status_code == 401:
                    # Token rejected or another HTTP error: report it as a warning.
                    warning = "Token rejected (401) — re-save settings to refresh."
                else:
                    warning = f"sites-health returned HTTP {resp.status_code}."
            except Exception as exc:
                # Network failure: report it.
                logger.warning("Could not fetch New Central sites-health: %s", exc)
                warning = f"Network error fetching sites: {exc}"
        else:
            # Classic Central: try each known path, stop on first 200
            # Remember the last status code and every path tried so the warning can be specific.
            last_status: int | None = None
            tried: list[str] = []
            # Try each known site path in order.
            for path, params in CLASSIC_SITE_PATHS:
                tried.append(path)
                try:
                    resp = await client.get(
                        f"{base_url}{path}",
                        headers=headers,
                        params=params,
                        timeout=20,
                    )
                    last_status = resp.status_code
                    logger.info("Classic Central sites %s → %s: %s", path, resp.status_code, resp.text[:200])
                    # Success: parse whatever shape this endpoint returned.
                    if resp.status_code == 200:
                        data = resp.json()
                        # Response may use "sites", "items", or root list
                        raw = data.get("sites") or data.get("items") or (data if isinstance(data, list) else [])
                        # Items may be bare strings or objects with a name field.
                        for site in raw:
                            if isinstance(site, str):
                                sites.append(site)
                            else:
                                name = site.get("site_name") or site.get("siteName") or site.get("name", "")
                                if name:
                                    sites.append(name)
                        # Stop after the first successful path.
                        break
                    elif resp.status_code == 401:
                        warning = "Token rejected (401) — re-save settings."
                        break
                    # 404 = path doesn't exist on this cluster, try next
                except Exception as exc:
                    # Network failure: warn and stop trying.
                    logger.warning("Could not fetch Classic Central sites from %s: %s", path, exc)
                    warning = f"Network error fetching sites: {exc}"
                    break

            # Nothing found: list what was tried so the user can see why.
            if not sites and not warning:
                warning = f"No sites found — tried {', '.join(tried)} (last HTTP {last_status}). Your cluster may not expose a sites list API."

    # Remove duplicates and sort the names for display.
    return {"sites": sorted(set(sites)), "warning": warning}




@router.get("/api/central/browse")
async def api_central_browse(force: bool = False) -> dict[str, Any]:
    """Return aggregated Central browse data for the spoke Central Monitoring tab.

    Serves a 5-minute server-side cache (same TTL as the hub) to avoid hammering
    the Central API on every tab open.  Pass ?force=true to bypass the cache.
    If the background browse cache is empty (first load before any poll cycle),
    trigger a live on-demand fetch so the caller always gets fresh data.
    """

    now = time.time()
    # Serve the cached response if it's still within TTL and not a forced refresh.
    if not force and state._central_browse_response_cache and (now - state._central_browse_response_cached_at) < NC_BROWSE_SERVER_CACHE_TTL_S:
        return state._central_browse_response_cache

    # If the background loop hasn't populated browse data yet, do an on-demand fetch.
    # Guard with a flag so concurrent requests don't each spawn their own fetch.
    if not state.central_browse_alerts and not state.central_browse_insights and not state.central_browse_clients and not state.central_browse_clients_by_site and _central_ready():
        if not state._central_browse_fetching:
            state._central_browse_fetching = True
            try:
                async with httpx.AsyncClient() as _browse_client:
                    await _fetch_nc_browse_for_spoke(_browse_client)
            except Exception as exc:
                logger.warning("api_central_browse: on-demand fetch failed: %s", exc)
            finally:
                state._central_browse_fetching = False

    # Fetch the site list (this also reports any configuration/token warning).
    sites_resp = await api_central_sites()
    site_names = list(sites_resp.get("sites") or [])
    sites_with_health: list[dict[str, Any]] = []

    # Build a health summary per site from the cached browse alerts.
    for site_name in site_names:
        # Alerts belonging to this site (case-insensitive match).
        site_alerts = [a for a in state.central_browse_alerts if str(a.get("site") or "").strip().lower() == str(site_name).strip().lower()]
        # Severity words that mean poor health, and those that mean fair health, across both APIs.
        severities = {str(a.get("severity") or "").strip().lower() for a in site_alerts}
        critical = bool(severities & {"critical", "major", "poor", "red", "orange", "error"})
        fair = bool(severities & {"minor", "warning", "yellow"})
        # Turn that into a label plus a rough score for the UI's colour coding.
        health_label = "Poor" if critical else ("Fair" if fair else "Healthy")
        health_score = 30 if critical else (60 if fair else 90)
        # Wireless client count for the site, trying each key name the poller may have used.
        clients_info = state.central_browse_clients_by_site.get(site_name, {}) or {}
        wireless_count = clients_info.get("wireless_clients")
        if wireless_count is None:
            wireless_count = clients_info.get("wireless")
        if wireless_count is None:
            wireless_count = clients_info.get("count")
        # Summary row for this site.
        sites_with_health.append({
            "name": site_name,
            "health_label": health_label,
            "health_score": health_score,
            "wireless_clients": wireless_count,
            "central_site": site_name,
        })

    # Assemble the full browse response from the background poller's cached data.
    result: dict[str, Any] = {
        "mode": settings.get("central_api", {}).get("mode") or ("central" if _is_new_central_api() else "classic"),
        "cached_at": now,
        "sites": sites_with_health,
        "alerts": list(state.central_browse_alerts),
        "insights": list(state.central_browse_insights),
        "clients": list(state.central_browse_clients),
        "clients_by_site": dict(state.central_browse_clients_by_site),
        "devices_by_site": dict(state.central_browse_devices_by_site),
        "warning": sites_resp.get("warning"),
    }
    # Cache the response so the next request within the TTL is served instantly.
    state._central_browse_response_cache = result
    state._central_browse_response_cached_at = now
    return result




@router.post("/api/central/monitor-site")
async def api_central_monitor_site(body: dict[str, Any] = Body(...), _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Add or remove a Central site from the spoke site mappings."""
    # Read the action (default add) and the Central site name, which is required.
    action = str(body.get("action") or "add").strip().lower()
    central_site = str(body.get("central_site") or "").strip()
    if not central_site:
        raise HTTPException(status_code=422, detail="central_site required")

    # Work on a copy of the local-site -> Central-site mapping.
    mappings = dict(settings.get("site_mappings") or {})
    # add: map the local site name (defaults to the Central name) to the Central site.
    if action == "add":
        wsite = str(body.get("wsite") or central_site).strip() or central_site
        mappings[wsite] = central_site
    elif action == "remove":
        # remove: drop every mapping whose key or value matches the site name.
        target = central_site.lower()
        to_remove = [k for k, v in mappings.items() if str(v or "").strip().lower() == target or str(k or "").strip().lower() == target]
        for key in to_remove:
            mappings.pop(key, None)
    else:
        # Any other action is invalid.
        raise HTTPException(status_code=422, detail="action must be add or remove")

    # Save the new mapping in memory and on disk, then push the change to connected UIs.
    settings["site_mappings"] = mappings
    _persisted["site_mappings"] = mappings
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True, "action": action, "central_site": central_site, "site_mappings": mappings}




@router.post("/api/central/monitored-items")
async def api_central_add_monitored_item(body: dict[str, Any] = Body(...), _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Add an item to the spoke's local Central monitored-items list."""
    # Read the item fields; the identifier defaults to the name.
    item_type = str(body.get("type") or "").strip()
    name = str(body.get("name") or "").strip()
    identifier = str(body.get("identifier") or body.get("name") or "").strip()
    if not item_type or not identifier:
        raise HTTPException(status_code=422, detail="type and identifier required")

    # Work on a copy of the current list.
    items = list(settings.get("spoke_monitored_items") or [])
    site = str(body.get("site") or "").strip()
    # If an identical item (same type, identifier and site) exists, return it instead of a
    # duplicate.
    for existing in items:
        if str(existing.get("type") or "") != item_type:
            continue
        if str(existing.get("identifier") or existing.get("name") or "").strip().lower() != identifier.lower():
            continue
        if str(existing.get("site") or "").strip().lower() != site.lower():
            continue
        return {"ok": True, "item": existing}

    # Otherwise create a new item with a unique id and creation time.
    item = {
        "id": str(uuid.uuid4()),
        "type": item_type,
        "name": name,
        "site": site,
        "identifier": identifier,
        "ts": time.time(),
    }
    # Save the list in memory and on disk, then notify connected UIs.
    items.append(item)
    settings["spoke_monitored_items"] = items
    _persisted["spoke_monitored_items"] = items
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True, "item": item}




@router.delete("/api/central/monitored-items/{item_id}")
async def api_central_remove_monitored_item(item_id: str, _user: SpokeUser = Depends(require_auth)) -> dict[str, Any]:
    """Remove an item from the spoke's local Central monitored-items list."""
    # Keep every item except the one with this id, then save and notify UIs.
    items = list(settings.get("spoke_monitored_items") or [])
    items = [item for item in items if str(item.get("id") or "") != item_id]
    settings["spoke_monitored_items"] = items
    _persisted["spoke_monitored_items"] = items
    _save_settings()
    await broadcast({"type": "settings_update", "settings": _get_cached_settings()})
    return {"ok": True}




@router.get("/api/central/devices")
async def api_central_devices(site: str | None = Query(default=None)) -> dict[str, Any]:
    """Return device inventory from New Central v1alpha1. Always returns 200.
    Optional ?site= filters to a specific Central site name.
    Classic Central: returns empty (use monitoring/v1/devices instead).
    """
    # Need configuration, a token and New Central mode.
    if not _central_ready() or not central_token.get("access_token"):
        return {"devices": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"devices": [], "count": 0, "warning": "Device inventory endpoint only available in Central (CNX) mode."}

    # Request setup plus the results to return.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    devices: list[dict[str, Any]] = []
    warning: str | None = None

    # One HTTP client for the lookups below.
    async with httpx.AsyncClient() as client:
        # Resolve site_id if a site name was provided
        site_id: str | None = None
        if site:
            # Look the site name up in sites-health to get its id; failures just mean "no filter".
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            break
            except Exception as exc:
                logger.warning("CNX devices: sites-health lookup failed: %s", exc)

        # Fetch devices, optionally filtered by site
        # Ask for up to 500 devices, filtered by site id when known.
        try:
            params: dict[str, Any] = {"limit": 500}
            if site_id:
                params["filter"] = f"siteId eq '{site_id}'"
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/devices",
                headers=headers, params=params, timeout=30,
            )
            # On 401, refresh the token once (if possible) and retry with the new headers.
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=30,
                )
            # Success: normalise each device into the UI's fields, using "—" for missing values.
            if resp.status_code == 200:
                for dev in resp.json().get("items", []):
                    devices.append({
                        "id":         dev.get("id") or dev.get("deviceId") or dev.get("serialNumber", ""),
                        "name":       dev.get("deviceName") or dev.get("name", "—"),
                        "type":       dev.get("deviceType") or dev.get("type", "—"),
                        "model":      dev.get("model", "—"),
                        "serial":     dev.get("serialNumber", "—"),
                        "mac":        dev.get("macAddress", "—"),
                        "ip":         dev.get("ipv4") or dev.get("ip") or dev.get("ipAddress", "—"),
                        "status":     dev.get("status", "—"),
                        "site":       dev.get("siteId", "—"),
                        "version":    dev.get("softwareVersion") or dev.get("firmwareVersion", "—"),
                        "uptime_ms":  dev.get("uptimeInMillis"),
                        "deployment": dev.get("deployment", "—"),
                    })
                # Post-filter by siteId in case the API ignored the OData filter param
                if site_id:
                    devices = [d for d in devices if d["site"] == site_id]
            elif resp.status_code == 401:
                # Token still rejected, or another HTTP error: report it as a warning.
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"Devices endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            # Network failure: report it.
            logger.warning("CNX devices fetch failed: %s", exc)
            warning = f"Network error fetching devices: {exc}"

    return {"devices": devices, "count": len(devices), "warning": warning}




@router.get("/api/central/wlans")
async def api_central_wlans() -> dict[str, Any]:
    """Return WLAN/SSID list from New Central v1alpha1. Always returns 200."""
    # Need configuration, a token and New Central mode.
    if not _central_ready() or not central_token.get("access_token"):
        return {"wlans": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"wlans": [], "count": 0, "warning": "WLAN endpoint only available in Central (CNX) mode."}

    # Request setup plus the results to return.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    wlans: list[dict[str, Any]] = []
    warning: str | None = None

    async with httpx.AsyncClient() as client:
        # Ask for up to 500 WLANs.
        try:
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/wlans",
                headers=headers, params={"limit": 500}, timeout=20,
            )
            # On 401, refresh the token once (if possible) and retry.
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/wlans",
                    headers=headers, params={"limit": 500}, timeout=20,
                )
            # Success: normalise each WLAN into the UI's fields.
            if resp.status_code == 200:
                for w in resp.json().get("items", []):
                    wlans.append({
                        "id":       w.get("wlanId") or w.get("id", ""),
                        "ssid":     w.get("ssid", "—"),
                        "type":     w.get("type", "—"),
                        "security": w.get("security", "—"),
                        "enabled":  w.get("enabled", True),
                        "band":     w.get("band") or w.get("radioType", "—"),
                    })
            elif resp.status_code == 401:
                # Token still rejected, or another HTTP error: report it as a warning.
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"WLANs endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            # Network failure: report it.
            logger.warning("CNX wlans fetch failed: %s", exc)
            warning = f"Network error fetching WLANs: {exc}"

    return {"wlans": wlans, "count": len(wlans), "warning": warning}




@router.get("/api/central/clients-detail")
async def api_central_clients_detail(site: str | None = Query(default=None)) -> dict[str, Any]:
    """Return connected client list from New Central v1alpha1. Always returns 200.
    Optional ?site= filters to a specific Central site name.
    """
    # Need configuration, a token and New Central mode.
    if not _central_ready() or not central_token.get("access_token"):
        return {"clients": [], "count": 0, "warning": "Central not configured or no valid token."}
    if not _is_new_central_api():
        return {"clients": [], "count": 0, "warning": "Client detail endpoint only available in Central (CNX) mode."}

    # Request setup plus the results to return.
    headers = _central_headers()
    base_url = _central_cfg()["cluster_url"].rstrip("/")
    result_clients: list[dict[str, Any]] = []
    warning: str | None = None

    # Resolve the site name to its id via sites-health; failures just mean "no filter".
    async with httpx.AsyncClient() as client:
        site_id: str | None = None
        if site:
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            break
            except Exception as exc:
                logger.warning("CNX clients-detail: sites-health lookup failed: %s", exc)

        # Ask for connected clients, filtered by site id when known.
        try:
            params: dict[str, Any] = {}
            if site_id:
                params["site-id"] = site_id
            resp = await client.get(
                f"{base_url}/network-monitoring/v1alpha1/clients",
                headers=headers, params=params, timeout=20,
            )
            # On 401, refresh the token once (if possible) and retry.
            if resp.status_code == 401 and _can_refresh():
                ok, _ = await _refresh_central_token(client)
                if ok:
                    headers = _central_headers()
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/clients",
                    headers=headers, params=params, timeout=20,
                )
            # Success: normalise each client into the UI's fields.
            if resp.status_code == 200:
                for c in resp.json().get("items", []):
                    result_clients.append({
                        "mac":             c.get("macAddress", "—"),
                        "ip":              c.get("ipAddress", "—"),
                        "username":        c.get("username") or c.get("name", "—"),
                        "device":          c.get("deviceName") or c.get("hostname", "—"),
                        "connection_type": c.get("connectionType", "—"),
                        "ssid":            c.get("ssid", "—"),
                        "ap":              c.get("apName") or c.get("accessPoint", "—"),
                        "connected":       c.get("connected", True),
                        "signal":          c.get("signalStrength") or c.get("signal"),
                    })
            elif resp.status_code == 401:
                # Token still rejected, or another HTTP error: report it as a warning.
                warning = "Token rejected (401) — re-save settings to refresh."
            else:
                warning = f"Clients endpoint returned HTTP {resp.status_code}."
        except Exception as exc:
            # Network failure: report it.
            logger.warning("CNX clients-detail fetch failed: %s", exc)
            warning = f"Network error fetching clients: {exc}"

    return {"clients": result_clients, "count": len(result_clients), "warning": warning}
