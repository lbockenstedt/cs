"""Aruba Poller (helpers moved verbatim from server.py; shared deps imported from server)."""
from __future__ import annotations

from server import (
    Any,
    CENTRAL_POLL_INTERVAL,
    CLIENT_COUNT_MIN_SAMPLES,
    CLIENT_COUNT_WINDOW,
    HTTPException,
    REPO_DIR,
    UpstreamJSONError,
    _CENTRAL_CLIENT_CACHE_TTL,
    _HW_FRIENDLY,
    _NEW_CENTRAL_TOKEN_URL,
    _append_and_trim_history,
    _auto_device_type,
    _build_runtime_central_config,
    _can_refresh,
    _central_client_cache,
    _check_transitions_and_notify,
    _client_count_payload,
    _client_count_samples,
    _default_central_api_settings,
    _fetch_nc_browse_for_spoke,
    _history_cutoff,
    _parse_upstream_json,
    _save_client_count_baseline,
    _save_settings,
    _save_state_cache,
    _sim_conf_cache,
    _update_service_health,
    asyncio,
    broadcast,
    central_token,
    compute_online,
    configparser,
    copy,
    datetime,
    history_lock,
    httpx,
    logger,
    settings,
    state,
    time,
    timezone,
)



def _reset_central_runtime_tokens() -> None:
    central_token["access_token"] = None
    central_token["refresh_token"] = None
    central_token["expires_at"] = 0.0
    state.central_auth_error = None




def _public_central_api_settings() -> dict[str, Any]:
    cfg = copy.deepcopy(settings.get("central_api", _default_central_api_settings()))
    cfg.setdefault("classic", {})
    cfg.setdefault("central", {})
    cfg["classic"].pop("password", None)
    cfg["central"].pop("client_secret", None)
    cfg["classic"]["password_configured"] = bool(settings.get("central_api", {}).get("classic", {}).get("password"))
    cfg["central"]["client_secret_configured"] = bool(settings.get("central_api", {}).get("central", {}).get("client_secret"))
    return cfg




def _sync_central_runtime_config() -> None:
    settings["central_config"] = _build_runtime_central_config(settings.get("central_api", _default_central_api_settings()), settings.get("central_config", {}))
    _reset_central_runtime_tokens()





def _central_cfg() -> dict[str, str]:
    return settings.get("central_config", {})




def _is_new_central_api() -> bool:
    return _central_cfg().get("api_version") == "new_central"




def _central_ready() -> bool:
    """Minimum config needed to make API calls."""
    cfg = _central_cfg()
    if not cfg.get("cluster_url"):
        return False
    if _is_new_central_api():
        # New Central: need client_id + client_secret to auto-fetch tokens
        return bool(cfg.get("client_id") and cfg.get("client_secret"))
    # Classic: need a token already loaded or stored
    return bool(cfg.get("access_token") or central_token.get("access_token"))




def _central_token_state() -> dict[str, str]:
    """Return {state, detail} describing the current Central API auth status.

    States: not_configured | auth_failed | token_expired | connected
    """
    cfg = _central_cfg()
    if not cfg.get("cluster_url"):
        return {"state": "not_configured", "detail": "No cluster URL — configure in Setup tab"}
    if _is_new_central_api():
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return {"state": "not_configured", "detail": "Client ID / Client Secret required for Central mode"}
    else:
        if not cfg.get("access_token") and not central_token.get("access_token"):
            if settings.get("central_api", {}).get("mode") == "classic":
                return {"state": "not_configured", "detail": "Classic mode is saved separately — use Test Connection in Setup to validate credentials."}
            return {"state": "not_configured", "detail": "No access token — configure in Setup tab"}
    tok = central_token.get("access_token")
    if not tok:
        err = state.central_auth_error or "Authentication not yet attempted"
        return {"state": "auth_failed", "detail": err}
    if time.time() >= central_token.get("expires_at", 0):
        if state.central_auth_error:
            return {"state": "auth_failed", "detail": state.central_auth_error}
        if _can_refresh():
            return {"state": "token_expired", "detail": "Token has expired — will refresh on next poll"}
        return {"state": "token_expired", "detail": "Token has expired — re-enter a valid token in Setup tab"}
    return {"state": "connected", "detail": "Token valid"}




async def _apply_central_feed(feed: dict) -> None:
    """Apply hub-provided Central data feed to local in-memory state (centralized mode)."""
    new_status = feed.get("status") or {}
    new_wireless = feed.get("wireless_clients") or {}
    new_total = feed.get("total_clients") or {}
    token_valid = bool(feed.get("token_valid", False))
    hardware_alerts = feed.get("hardware_alerts") or []

    state.central_status.clear()
    for wsite, checks in new_status.items():
        if isinstance(checks, dict):
            state.central_status[wsite] = {
                cid: dict(v) for cid, v in checks.items() if isinstance(v, dict)
            }
    state.central_wireless_clients.clear()
    state.central_wireless_clients.update({w: int(c or 0) for w, c in new_wireless.items()})

    # In centralized mode the hub polls Central and pushes total_clients (wired + wireless).
    # Populate _client_count_samples so the Sites health tab works the same way
    # it does in distributed mode (where _poll_central_once fills the samples).
    # Prefer total_clients; fall back to wireless_clients for older hub versions.
    counts_for_health = new_total or new_wireless
    if counts_for_health:
        now_cc = time.time()
        cutoff_cc = now_cc - CLIENT_COUNT_WINDOW
        for _wsite, _count in counts_for_health.items():
            _val = int(_count or 0)
            existing = _client_count_samples.get(_wsite)
            if not existing:
                # First-time feed for this site: seed CLIENT_COUNT_MIN_SAMPLES synthetic
                # backdated samples so the UI can show status immediately rather than
                # staying in "Collecting" until 3 real polls have accumulated.
                _client_count_samples[_wsite] = [
                    (now_cc - (CLIENT_COUNT_MIN_SAMPLES - i) * 60, _val)
                    for i in range(CLIENT_COUNT_MIN_SAMPLES)
                ]
            else:
                _client_count_samples[_wsite].append((now_cc, _val))
                _client_count_samples[_wsite] = [
                    s for s in _client_count_samples[_wsite] if s[0] >= cutoff_cc
                ]
        _save_client_count_baseline()

    state.hardware_alert_devices = {}
    for alert in hardware_alerts:
        if not isinstance(alert, dict):
            continue
        check_id = str(alert.get("id") or "").strip()
        if not check_id:
            continue
        sites = alert.get("sites") or {}
        site_devices: dict[str, list[str]] = {}
        for wsite, info in sites.items():
            if not isinstance(info, dict):
                continue
            devices = [str(device).strip() for device in info.get("devices") or [] if str(device).strip()]
            if devices:
                site_devices[str(wsite)] = devices
        state.hardware_alert_devices[check_id] = site_devices

    # Cache the full pre-built hardware_alerts list from the hub feed.  This is used
    # by _hw_alerts_payload() when the spoke has no locally-configured hardware_checks
    # (i.e. in hub-connected / centralized mode).  The hub already includes id, name,
    # device_type, total, and sites — exactly the shape _hw_alerts_payload() produces.
    state._hub_fed_hardware_alerts = [a for a in hardware_alerts if isinstance(a, dict) and a.get("id")]

    # Update token state so spoke Central tab shows connected status
    if token_valid:
        central_token.setdefault("access_token", "_hub_managed_")
        central_token["expires_at"] = time.time() + 3600
    else:
        central_token["access_token"] = None
        central_token["expires_at"] = 0.0
    browse_alerts = feed.get("central_browse_alerts")
    browse_insights = feed.get("central_browse_insights")
    browse_clients_by_site = feed.get("central_browse_clients_by_site")
    browse_clients = feed.get("central_browse_clients")  # individual records
    browse_devices = feed.get("central_browse_devices_by_site")
    browse_changed = False
    if isinstance(browse_alerts, list):
        state.central_browse_alerts = browse_alerts
        browse_changed = True
    if isinstance(browse_insights, list):
        state.central_browse_insights = browse_insights
        browse_changed = True
    if isinstance(browse_clients_by_site, dict):
        state.central_browse_clients_by_site = browse_clients_by_site
        browse_changed = True
    if isinstance(browse_clients, list):
        state.central_browse_clients = browse_clients
        browse_changed = True
    if isinstance(browse_devices, dict):
        state.central_browse_devices_by_site = browse_devices
        browse_changed = True
    if browse_changed:
        state._central_browse_response_cache = {}
        state._central_browse_response_cached_at = 0.0

    await broadcast({
        "type": "central_update",
        "status": _central_status_payload(),
        "wireless_clients": dict(state.central_wireless_clients),
        "hardware_alerts": _hw_alerts_payload(),
        "client_count_status": _client_count_payload(),
        "ts": time.time(),
        "token_state": _central_token_state(),
    })




async def _fetch_new_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Obtain a token for New Central via HPE GreenLake client_credentials grant."""
    cfg = _central_cfg()
    try:
        resp = await client.post(
            _NEW_CENTRAL_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not resp.is_success:
            return False, f"Token request failed (HTTP {resp.status_code}): {resp.text[:300]}"
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            return False, f"No access_token in GLP response: {resp.text[:300]}"
        expires_in = payload.get("expires_in", 7200)
        central_token["access_token"] = token
        central_token["refresh_token"] = None
        central_token["expires_at"] = time.time() + expires_in - 60
        logger.info("New Central token obtained via client_credentials (expires in %ss)", expires_in)
        return True, "Token obtained via client_credentials."
    except Exception as exc:
        return False, f"GLP token request error: {exc}"




async def _fetch_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Load/obtain the access token and verify it against a probe endpoint.

    For New Central: obtains a token via GLP client_credentials grant.
    For Classic: loads the user-pasted token from settings and probes the API.
    Returns (success, detail_message).
    """
    if _is_new_central_api():
        ok, msg = await _fetch_new_central_token(client)
        if not ok:
            state.central_auth_error = msg
            return False, msg
        # Probe to confirm the token works against the base URL
        ok, msg = await _probe_central_token(client)
        state.central_auth_error = None if ok else msg
        return ok, msg

    cfg = _central_cfg()
    token = cfg.get("access_token", "").strip()
    if not token:
        return False, "No access token configured."

    central_token["access_token"] = token
    if cfg.get("refresh_token"):
        central_token["refresh_token"] = cfg["refresh_token"]
    central_token["expires_at"] = time.time() + 7200

    ok, msg = await _probe_central_token(client)
    state.central_auth_error = None if ok else msg
    return ok, msg




async def _probe_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Probe the Central API to confirm the in-memory token is accepted."""
    cfg = _central_cfg()
    base_url = cfg["cluster_url"].rstrip("/")
    token = central_token.get("access_token", "")
    headers = {"Authorization": f"Bearer {token}"}

    if _is_new_central_api():
        # New Central v1alpha1 — sites-health is the lightest reliable endpoint
        probe_urls = [
            (f"{base_url}/network-monitoring/v1alpha1/sites-health", {}),
            (f"{base_url}/network-monitoring/v1alpha1/devices", {"limit": 1}),
        ]
    else:
        # Classic Central
        probe_urls = [
            (f"{base_url}/configuration/v2/groups", {"limit": 1, "offset": 0}),
            (f"{base_url}/monitoring/v1/alerts", {"limit": 1}),
            (f"{base_url}/monitoring/v2/alerts", {"limit": 1}),
            (f"{base_url}/platform/v1/customer_id", {}),
        ]
    last_status: int = 0
    last_body: str = ""
    for url, params in probe_urls:
        try:
            logger.info("Central probe → GET %s params=%s", url, params)
            resp = await client.get(url, headers=headers, params=params, timeout=15)
            last_status = resp.status_code
            last_body = resp.text[:400]
            logger.info("Central probe ← %s: %s", resp.status_code, last_body[:200])
            if resp.status_code == 200:
                logger.info("Aruba Central token validated via %s", url)
                return True, "Token validated successfully."
            if resp.status_code == 401:
                # Token is definitely invalid — try refresh before giving up
                central_token["access_token"] = None
                ok, msg = await _refresh_central_token(client)
                if ok:
                    return True, f"Access token was expired; successfully refreshed. {msg}"
                return False, f"Token rejected (401). Central response: {last_body}"
            if resp.status_code == 400:
                # 400 means the endpoint exists and accepted our token but wants different params.
                # That's enough to confirm the token is valid.
                logger.info("Central token confirmed via %s (400 = endpoint live, token accepted)", url)
                return True, "Token validated successfully (endpoint reachable, token accepted)."
            # 403/404 = wrong scope or endpoint missing — try next probe
            logger.info("Central probe %s returned %s — trying next", url, resp.status_code)
        except Exception as exc:
            return False, f"Connection error reaching {base_url}: {exc}"

    return False, (
        f"Could not confirm token with Central (last HTTP status: {last_status}). "
        f"Response: {last_body}. "
        "Check the Cluster URL and that the token has monitoring or configuration scope."
    )




async def _refresh_central_token(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Refresh/renew the access token.

    New Central: re-requests via GLP client_credentials (no refresh token).
    Classic: uses refresh_token grant against Central's OAuth endpoint.
    Returns (success, detail_message).
    """
    if not _can_refresh():
        return False, "Cannot refresh: missing client_id or client_secret."
    if _is_new_central_api():
        return await _fetch_new_central_token(client)
    cfg = _central_cfg()
    token_url = cfg["cluster_url"].rstrip("/") + "/oauth2/token"
    refresh_tok = cfg.get("refresh_token") or central_token.get("refresh_token", "")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": refresh_tok,
    }
    if cfg.get("customer_id"):
        data["customer_id"] = cfg["customer_id"]
    try:
        resp = await client.post(token_url, data=data, timeout=15)
        if not resp.is_success:
            return False, f"Refresh failed (HTTP {resp.status_code}): {resp.text[:300]}"
        payload = _parse_upstream_json(resp)
        new_access = payload["access_token"]
        new_refresh = payload.get("refresh_token", refresh_tok)
        central_token["access_token"] = new_access
        central_token["refresh_token"] = new_refresh
        central_token["expires_at"] = time.time() + payload.get("expires_in", 7200) - 60
        settings["central_config"]["access_token"] = new_access
        settings["central_config"]["refresh_token"] = new_refresh
        _save_settings()
        logger.info("Aruba Central token refreshed successfully")
        return True, "Token refreshed successfully."
    except UpstreamJSONError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Refresh request failed: {exc}"




async def _test_classic_central_connection(client: httpx.AsyncClient) -> tuple[bool, str]:
    classic_cfg = settings.get("central_api", {}).get("classic", {})
    base_url = str(classic_cfg.get("url", "")).strip().rstrip("/")
    username = str(classic_cfg.get("username", "")).strip()
    password = str(classic_cfg.get("password", ""))
    if not base_url or not username or not password:
        return False, "Central API not configured — enter URL, Username, and Password in Setup."
    try:
        resp = await client.get(base_url, auth=(username, password), timeout=15, follow_redirects=True)
    except Exception as exc:
        return False, f"Connection error reaching {base_url}: {exc}"
    if resp.status_code in (401, 403):
        return False, f"Classic credentials rejected (HTTP {resp.status_code})."
    if resp.status_code >= 500:
        return False, f"Classic endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
    return True, "Connected to Classic API successfully."




def _central_headers() -> dict[str, str]:
    token = central_token.get("access_token")
    if not token:
        raise HTTPException(status_code=503, detail="Aruba Central token not available — check connection settings")
    return {"Authorization": f"Bearer {token}"}




async def central_token_manager() -> None:
    """Background task: keep token valid. Runs every 5 minutes."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if _central_ready():
                    no_token = not central_token.get("access_token")
                    expiring = time.time() >= central_token.get("expires_at", 0) - 300
                    if no_token:
                        ok, msg = await _fetch_central_token(client)
                        if not ok:
                            logger.warning("Central token load failed: %s", msg)
                        # Broadcast updated token state regardless of success
                        await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(state.central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()})
                    elif expiring and _can_refresh():
                        ok, msg = await _refresh_central_token(client)
                        if not ok:
                            logger.warning("Central token refresh failed: %s", msg)
                            state.central_auth_error = f"Token refresh failed: {msg}"
                            central_token["access_token"] = None  # force re-fetch next cycle
                        else:
                            state.central_auth_error = None
                        await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(state.central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": time.time(), "token_state": _central_token_state()})
                _update_service_health("central_token", ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _update_service_health("central_token", ok=False, error=str(exc))
                logger.exception("Central token manager error: %s", exc)
            await asyncio.sleep(300)




async def _poll_central_once(client: httpx.AsyncClient) -> None:
    """Single poll cycle: fetch alerts + insights per mapped site, evaluate checks."""
    if not _central_ready() or not central_token.get("access_token"):
        return

    site_mappings: dict[str, str] = settings.get("site_mappings", {})
    monitored: list[dict[str, Any]] = settings.get("monitored_checks", [])
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    if not site_mappings or (not monitored and not hw_checks):
        return

    hw_check_ids: set[str] = {c["id"] for c in hw_checks}

    cfg = _central_cfg()
    base_url = cfg["cluster_url"].rstrip("/")
    headers = _central_headers()
    now = time.time()
    new_records: list[dict[str, Any]] = []

    # Accumulate hardware alert devices across all sites this cycle
    new_hw_devices: dict[str, dict[str, list[str]]] = {c["id"]: {} for c in hw_checks}

    for wsite, central_site in site_mappings.items():
        site_check_status: dict[str, Any] = {}

        # ── Fetch alerts for this site ────────────────────────────
        alert_type_counts: dict[str, int] = {}
        site_health: dict[str, Any] = {}

        if _is_new_central_api():
            # New Central v1alpha1: derive synthetic alert_type_counts from available endpoints.
            # Fetch sites-health, devices, and clients in parallel for the mapped site.

            # ── sites-health ──────────────────────────────────────────────
            site_id: str | None = None
            try:
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code == 401 and _can_refresh():
                    ok, _ = await _refresh_central_token(client)
                    if ok:
                        headers = _central_headers()
                    resp = await client.get(
                        f"{base_url}/network-monitoring/v1alpha1/sites-health",
                        headers=headers,
                        timeout=20,
                    )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == central_site.lower():
                            site_health = item
                            site_id = item.get("siteId") or item.get("site_id")
                            score = item.get("healthScore", item.get("health_score", 100))
                            ap_count = item.get("apCount", item.get("ap_count", 0))
                            alert_type_counts["SITE_HEALTH"] = int(score)
                            alert_type_counts["AP_COUNT"] = int(ap_count)
                            break
            except Exception as exc:
                logger.warning("New Central sites-health fetch failed for site %s: %s", central_site, exc)

            # ── devices (AP_DOWN, SWITCH_DOWN, GATEWAY_DOWN) ──────────────
            try:
                params: dict[str, Any] = {"limit": 500}
                if site_id:
                    params["filter"] = f"siteId eq '{site_id}'"
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/devices",
                    headers=headers, params=params, timeout=20,
                )
                if resp.status_code == 200:
                    ap_down = switch_down = gw_down = 0
                    for dev in resp.json().get("items", []):
                        dtype = (dev.get("deviceType") or "").upper()
                        status = (dev.get("status") or "").upper()
                        is_down = status not in ("UP", "ONLINE")
                        if dtype == "ACCESS_POINT" and is_down:
                            ap_down += 1
                        elif dtype == "SWITCH" and is_down:
                            switch_down += 1
                        elif dtype == "GATEWAY" and is_down:
                            gw_down += 1
                    alert_type_counts["AP_DOWN"] = ap_down
                    alert_type_counts["SWITCH_DOWN"] = switch_down
                    alert_type_counts["GATEWAY_DOWN"] = gw_down
            except Exception as exc:
                logger.warning("New Central devices fetch failed for site %s: %s", central_site, exc)

            # ── clients (CLIENT_COUNT) ─────────────────────────────────────
            try:
                cparams: dict[str, Any] = {}
                if site_id:
                    cparams["site-id"] = site_id
                resp = await client.get(
                    f"{base_url}/network-monitoring/v1alpha1/clients",
                    headers=headers, params=cparams, timeout=20,
                )
                if resp.status_code == 200:
                    alert_type_counts["CLIENT_COUNT"] = int(resp.json().get("count", 0))
            except Exception as exc:
                logger.warning("New Central clients fetch failed for site %s: %s", central_site, exc)

            # New Central: no insights endpoint — skip
            insight_cat_counts: dict[str, int] = {}
        else:
            for alerts_path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
                try:
                    resp = await client.get(
                        f"{base_url}{alerts_path}",
                        headers=headers,
                        params={"site": central_site, "limit": 1000},
                        timeout=20,
                    )
                    if resp.status_code == 401 and _can_refresh():
                        ok, _ = await _refresh_central_token(client)
                        if ok:
                            headers = _central_headers()
                        resp = await client.get(
                            f"{base_url}{alerts_path}",
                            headers=headers,
                            params={"site": central_site, "limit": 1000},
                            timeout=20,
                        )
                    if resp.status_code == 200:
                        data = resp.json()
                        for alert in data.get("alerts", []):
                            atype = alert.get("alert_type") or alert.get("type", "")
                            if atype:
                                alert_type_counts[atype] = alert_type_counts.get(atype, 0) + 1
                                # Collect device names for hardware checks
                                if atype in hw_check_ids:
                                    dev = (alert.get("device_name") or alert.get("hostname")
                                           or alert.get("name") or "").strip()
                                    if dev:
                                        new_hw_devices.setdefault(atype, {}).setdefault(wsite, [])
                                        if dev not in new_hw_devices[atype][wsite]:
                                            new_hw_devices[atype][wsite].append(dev)
                        break
                    if resp.status_code == 404:
                        continue
                except Exception as exc:
                    logger.warning("Central alerts fetch failed for site %s: %s", central_site, exc)
                    break

            # ── Fetch insights for this site ──────────────────────────
            insight_cat_counts: dict[str, int] = {}
            try:
                resp = await client.get(
                    f"{base_url}/aiops/v1/insights",
                    headers=headers,
                    params={"site_name": central_site, "limit": 1000},
                    timeout=20,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for insight in data.get("insights", []):
                        cat = insight.get("category") or insight.get("type", "")
                        if cat:
                            insight_cat_counts[cat] = insight_cat_counts.get(cat, 0) + 1
            except Exception as exc:
                logger.warning("Central insights fetch failed for site %s: %s", central_site, exc)

        # ── Evaluate each monitored check ─────────────────────────
        for check in monitored:
            check_type = check.get("type", "")
            check_id = check.get("id", "")
            check_name = check.get("name", check_id)
            if not check_id:
                continue

            if check_type == "alert":
                count = alert_type_counts.get(check_id, 0)
            elif check_type == "insight":
                count = insight_cat_counts.get(check_id, 0)
            else:
                continue

            status = "OK" if count > 0 else "ERROR"
            site_check_status[check_id] = {
                "status": status,
                "count": count,
                "check_name": check_name,
                "check_type": check_type,
                "ts": now,
            }
            new_records.append({
                "ts": now,
                "wsite": wsite,
                "central_site": central_site,
                "check_type": check_type,
                "check_id": check_id,
                "check_name": check_name,
                "status": status,
                "count": count,
            })

        state.central_status[wsite] = site_check_status

        # ── Fetch wireless client count for this site from Central ─
        wl_count = 0
        try:
            if _is_new_central_api():
                # New API: client count lives in site_health payload
                wl_count = int(
                    site_health.get("clientCount")
                    or site_health.get("client_count")
                    or 0
                )
            else:
                # Classic API: query wireless clients with site filter.
                # Try both "site" and "site_name" — Central uses each in
                # different API versions.
                fetched = False
                for clients_path in ["/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"]:
                    for site_param in ["site", "site_name"]:
                        resp = await client.get(
                            f"{base_url}{clients_path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1},
                            timeout=20,
                        )
                        if resp.status_code == 401 and _can_refresh():
                            ok, _ = await _refresh_central_token(client)
                            if ok:
                                headers = _central_headers()
                            resp = await client.get(
                                f"{base_url}{clients_path}",
                                headers=headers,
                                params={site_param: central_site, "limit": 1},
                                timeout=20,
                            )
                        logger.info(
                            "Central wireless clients %s ?%s=%s → %s body=%s",
                            clients_path, site_param, central_site,
                            resp.status_code, resp.text[:200],
                        )
                        if resp.status_code == 200:
                            body = resp.json()
                            wl_count = int(body.get("total") or body.get("count") or 0)
                            fetched = True
                            break
                        if resp.status_code == 404:
                            continue
                    if fetched:
                        break
        except Exception as exc:
            logger.warning("Central wireless client count fetch failed for site %s: %s", central_site, exc)
        state.central_wireless_clients[wsite] = wl_count

        _client_count_samples.setdefault(wsite, []).append((now, wl_count))
        cutoff_cc = now - CLIENT_COUNT_WINDOW
        _client_count_samples[wsite] = [
            s for s in _client_count_samples[wsite] if s[0] >= cutoff_cc
        ]
    state.hardware_alert_devices = new_hw_devices
    await _check_transitions_and_notify(now)

    # ── Persist client count baseline ─────────────────────────────
    _save_client_count_baseline()

    # ── Persist history ───────────────────────────────────────────
    if new_records:
        cutoff = _history_cutoff()
        async with history_lock:
            state.central_history[:] = [r for r in state.central_history if r["ts"] >= cutoff]
            state.central_history.extend(new_records)
        await asyncio.to_thread(_append_and_trim_history, new_records)

    await broadcast({"type": "central_update", "status": _central_status_payload(), "wireless_clients": dict(state.central_wireless_clients), "hardware_alerts": _hw_alerts_payload(), "client_count_status": _client_count_payload(), "ts": now, "token_state": _central_token_state()})
    _save_state_cache()
    # In distributed mode with new_central, also fetch browse data (alerts, insights,
    # devices, clients) filtered to this spoke's assigned sites so the hub can assemble
    # a complete multi-site view.
    if _is_new_central_api():
        try:
            await _fetch_nc_browse_for_spoke(client)
        except Exception as exc:
            logger.warning("NC browse fetch failed: %s", exc)




def _central_status_payload() -> dict[str, Any]:
    """Serialize current central_status for WS / API responses."""
    return {
        wsite: {
            check_id: {
                "status": info["status"],
                "count": info["count"],
                "check_name": info["check_name"],
                "check_type": info["check_type"],
                "ts": info["ts"],
            }
            for check_id, info in checks.items()
        }
        for wsite, checks in state.central_status.items()
    }




def _hw_alerts_payload() -> list[dict[str, Any]]:
    """Serialize hardware_alert_devices merged with check metadata for broadcast.

    In distributed mode (spoke has its own Central credentials) the spoke builds
    hardware_alert_devices from its own polling and this function assembles the
    payload from settings["hardware_checks"].

    In hub-connected (centralized) mode the spoke's settings["hardware_checks"] is
    empty — the hub computes the alerts and pushes a pre-built list via the feed,
    stored in _hub_fed_hardware_alerts.  Fall back to that list so the simulation
    view can display gateway/AP/switch status correctly.
    """
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    if not hw_checks:
        # Hub-connected mode: return the pre-built list pushed by the hub
        return list(state._hub_fed_hardware_alerts)
    site_mappings: dict[str, str] = settings.get("site_mappings", {})
    result = []
    for check in hw_checks:
        cid = check["id"]
        devices_by_wsite = state.hardware_alert_devices.get(cid, {})
        total = sum(len(devs) for devs in devices_by_wsite.values())
        sites_out = {}
        for wsite, devs in devices_by_wsite.items():
            sites_out[wsite] = {
                "site_name": site_mappings.get(wsite, wsite),
                "devices": devs,
            }
        result.append({
            "id": cid,
            "name": check.get("name") or _HW_FRIENDLY.get(cid, cid),
            "device_type": check.get("device_type") or _auto_device_type(cid),
            "total": total,
            "sites": sites_out,
        })
    return result






def _sim_clients_per_wsite(active_snap: dict[str, Any]) -> dict[str, int]:
    """Count currently-online sim clients grouped by wsite.

    Reads simulation.conf directly so the result is always fresh regardless
    of whether the /api/simulations endpoint has been called.  Falls back to
    _sim_conf_cache when the conf file is unavailable.
    """
    # Build sim_id → wsite from simulation.conf (s0..s9 buckets)
    sim_to_wsite: dict[str, str] = {}
    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    try:
        parser = configparser.ConfigParser()
        parser.read_string(sim_conf_path.read_text(encoding="utf-8"))
        for section in parser.sections():
            if parser.has_option(section, "wsite"):
                wsite_val = parser.get(section, "wsite").strip()
                if wsite_val:
                    sim_to_wsite[section] = wsite_val
    except Exception:
        # Fall back to cached data if conf is unreadable
        for sim_id, info in _sim_conf_cache.get("simulations", {}).items():
            wsite_val = str(info.get("wsite", "")).strip()
            if wsite_val:
                sim_to_wsite[sim_id] = wsite_val

    if not sim_to_wsite:
        return {}

    # Count online clients per wsite
    counts: dict[str, int] = {w: 0 for w in set(sim_to_wsite.values())}
    for client_data in active_snap.values():
        sim_id = client_data.get("simulation_id", "")
        wsite_val = sim_to_wsite.get(sim_id, "")
        if not wsite_val:
            continue
        if compute_online(client_data.get("last_seen", datetime.min.replace(tzinfo=timezone.utc))):
            counts[wsite_val] = counts.get(wsite_val, 0) + 1
    return counts




async def central_poller() -> None:
    """Background task: poll Central every CENTRAL_POLL_INTERVAL seconds."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if settings.get("hub_aruba_polling_mode") == "centralized":
                    # Polling is delegated to the hub — mark health ok so the
                    # UI doesn't show a stale warning, then sleep until next check.
                    _update_service_health("central_poller", ok=True)
                    await asyncio.sleep(300)
                    continue
                await _poll_central_once(client)
                _update_service_health("central_poller", ok=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _update_service_health("central_poller", ok=False, error=str(exc))
                logger.exception("Central poll error: %s", exc)
            await asyncio.sleep(CENTRAL_POLL_INTERVAL)




def _telemetry_filtered_browse_list(items: list[dict[str, Any]], site_field: str) -> list[dict[str, Any]]:
    """Return browse list items filtered to only this spoke's assigned Central sites.

    Prevents unassigned-site data (fetched for the local browse tab) from being
    sent to the hub and polluting its distributed-mode aggregation.
    """
    assigned: set[str] = {
        str(v).strip().lower() for v in settings.get("site_mappings", {}).values() if v
    }
    if not assigned:
        return list(items)
    return [i for i in items if str(i.get(site_field) or "").strip().lower() in assigned]




def _telemetry_filtered_browse_dict(by_site: dict[str, Any]) -> dict[str, Any]:
    """Return a by-site browse dict filtered to only this spoke's assigned Central sites."""
    assigned: set[str] = {
        str(v).strip().lower() for v in settings.get("site_mappings", {}).values() if v
    }
    if not assigned:
        return dict(by_site)
    return {k: v for k, v in by_site.items() if str(k).strip().lower() in assigned}




async def _fetch_central_client_names(wsite: str, central_site: str) -> list[str]:
    """Fetch wireless client hostnames from Central for a given site (cached 60 s)."""
    cache_key = f"{wsite}:{central_site}"
    now = time.time()
    if cache_key in _central_client_cache:
        ts, names = _central_client_cache[cache_key]
        if now - ts < _CENTRAL_CLIENT_CACHE_TTL:
            return names

    cfg = _central_cfg()
    if not cfg.get("access_token") and not cfg.get("client_id"):
        return []

    base_url = cfg["cluster_url"].rstrip("/")
    headers = _central_headers()
    names: list[str] = []

    async with httpx.AsyncClient() as client:
        for path in ["/monitoring/v2/clients/wireless", "/monitoring/v1/clients/wireless"]:
            for site_param in ["site", "site_name"]:
                try:
                    resp = await asyncio.wait_for(
                        client.get(
                            f"{base_url}{path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1000},
                            timeout=10,
                        ),
                        timeout=12,
                    )
                    if resp.status_code == 401 and _can_refresh():
                        ok, _ = await _refresh_central_token(client)
                        if ok:
                            headers = _central_headers()
                        resp = await client.get(
                            f"{base_url}{path}",
                            headers=headers,
                            params={site_param: central_site, "limit": 1000},
                            timeout=10,
                        )
                    if resp.status_code == 200:
                        body = resp.json()
                        for c in body.get("clients", []):
                            n = (c.get("name") or c.get("client_name") or
                                 c.get("username") or "").strip().lower()
                            if n:
                                names.append(n)
                        _central_client_cache[cache_key] = (now, names)
                        return names
                    if resp.status_code == 404:
                        continue
                except Exception:
                    pass

    return names
