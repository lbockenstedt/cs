"""Aruba Central connectivity, webhook, browse, and monitored-item routes for the aggregate router package."""
from __future__ import annotations

from fastapi import APIRouter
from ._common import *  # noqa: F401,F403 -- shared helpers/models/state

router = APIRouter()



@router.post("/{tenant_id}/aggregate/test-central")
async def test_central_connection(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Test Aruba Central credentials and return token status + discovered sites."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    if not tenant.aruba_config_enc:
        return {"ok": False, "error": "No Aruba Central credentials configured for this tenant."}
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to decrypt credentials: {exc}"}
    client = ArubaClient(cfg)
    if not client.is_configured():
        return {"ok": False, "error": "Cluster URL is not set."}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, verify=True) as hc:
            token = await client._ensure_token(hc)
            # For new_central, also return the raw API response to aid debugging
            raw_response = None
            if client.api_version == "new_central":
                try:
                    async with httpx.AsyncClient(timeout=30) as dbg:
                        raw_response = await client._get(dbg, "/network-monitoring/v1alpha1/sites-health")
                except Exception as raw_exc:
                    raw_response = {"error": str(raw_exc)}
            sites = await client.list_sites()
        result: dict[str, Any] = {
            "ok": True,
            "token_obtained": True,
            "api_version": client.api_version,
            "cluster_url": client.cluster_url,
            "sites_discovered": len(sites),
            "sites": [s.get("name") for s in sites if isinstance(s, dict)],
        }
        if raw_response is not None:
            result["raw_sites_response"] = raw_response
        return result
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:500]
        except Exception:
            pass
        detail = f"HTTP {exc.response.status_code} from {exc.request.url}: {body or exc.response.reason_phrase}"
        logger.warning("test-central HTTP error: %s", detail)
        return {"ok": False, "token_obtained": False, "error": detail}
    except httpx.ConnectError as exc:
        detail = f"Connection error: {exc}" if str(exc) else f"Could not connect to {client.cluster_url} — check the cluster URL and network access"
        logger.warning("test-central connect error: %s", detail)
        return {"ok": False, "token_obtained": False, "error": detail}
    except httpx.TimeoutException as exc:
        detail = f"Timeout connecting to {client.cluster_url}"
        logger.warning("test-central timeout: %s", exc)
        return {"ok": False, "token_obtained": False, "error": detail}
    except Exception as exc:
        detail = str(exc) or repr(exc)
        logger.exception("test-central unexpected error")
        return {"ok": False, "token_obtained": False, "error": detail}


@router.post("/{tenant_id}/aggregate/register-central-webhook")
async def register_central_webhook(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Register the hub as a Central webhook receiver."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    cfg = _load_aruba_config(tenant)
    client = ArubaClient(cfg)
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="Aruba Central cluster URL is not configured.")

    endpoint_url = _central_webhook_endpoint_url(resolved_tenant_id)
    api_key = secrets.token_urlsafe(32)
    webhook_name = f"ClientSim Hub - {tenant.name}" if tenant.name else "ClientSim Hub"
    existing_webhook_id = str(cfg.get("webhook_id") or "").strip()
    if existing_webhook_id:
        await client.delete_webhook(existing_webhook_id)
    created = await client.register_webhook(webhook_name, endpoint_url, api_key)
    webhook_id = str(created.get("id") or created.get("webhookId") or created.get("webhook_id") or "").strip()
    if not webhook_id:
        raise HTTPException(status_code=502, detail="Central did not return a webhook ID.")
    cfg["webhook_id"] = webhook_id
    cfg["webhook_api_key"] = api_key
    _persist_aruba_config(tenant, cfg)
    return {"ok": True, "webhook_id": webhook_id, "endpoint_url": endpoint_url}


@router.delete("/{tenant_id}/aggregate/register-central-webhook")
async def deregister_central_webhook(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Remove the hub webhook from Central."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    endpoint_url = _central_webhook_endpoint_url(resolved_tenant_id)
    if not tenant.aruba_config_enc:
        return {"ok": True, "registered": False, "endpoint_url": endpoint_url}
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt Aruba Central credentials: {exc}") from exc
    webhook_id = str(cfg.get("webhook_id") or "").strip()
    if webhook_id and str(cfg.get("cluster_url") or "").strip():
        cfg["cluster_url"] = _validated_cluster_url_or_400(cfg.get("cluster_url", ""))
        client = ArubaClient(cfg)
        if client.is_configured():
            await client.delete_webhook(webhook_id)
    cfg.pop("webhook_id", None)
    cfg.pop("webhook_api_key", None)
    _persist_aruba_config(tenant, cfg)
    return {"ok": True, "registered": False, "endpoint_url": endpoint_url}


@router.get("/{tenant_id}/aggregate/register-central-webhook")
async def get_central_webhook_status(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Get current webhook registration status."""
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    webhook_id = ""
    webhook_api_key = ""
    if tenant.aruba_config_enc:
        try:
            cfg = decrypt_dict(tenant.aruba_config_enc)
            webhook_id = str(cfg.get("webhook_id") or "").strip()
            webhook_api_key = str(cfg.get("webhook_api_key") or "").strip()
        except Exception:
            webhook_id = ""
            webhook_api_key = ""
    return {
        "registered": bool(webhook_id),
        "webhook_id": webhook_id,
        "webhook_api_key": webhook_api_key,
        "endpoint_url": _central_webhook_endpoint_url(resolved_tenant_id),
    }


@router.get("/aggregate/api-server")
def get_aggregate_api_server(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    spokes = [
        {
            "tenant_id": resolved_tenant_id,
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "spoke_online": _is_online(spoke),
            "last_seen": spoke.last_seen,
            "api_server": _telemetry_dict(spoke, "api_server"),
        }
        for spoke in _approved_spokes(resolved_tenant_id)
    ]
    spokes.sort(key=lambda item: str(item.get("spoke_name") or "").lower())
    return {"tenant_id": resolved_tenant_id, "spokes": spokes}


@router.get("/aggregate/central")
def get_aggregate_central(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    return _aggregate_central_payload(resolved_tenant_id)


@router.get("/{tenant_id}/aggregate/central-sites-config")
def get_tenant_aggregate_central_sites_config(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    return _normalize_central_sites_config(store.get_tenant_central_sites_config(resolved_tenant_id))


@router.post("/{tenant_id}/aggregate/central-sites-config")
def set_tenant_aggregate_central_sites_config(
    tenant_id: str,
    payload: CentralSitesConfigPayload,
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    # Merge with existing stored config so unrelated fields (e.g. monitored_items) are preserved
    existing = store.get_tenant_central_sites_config(resolved_tenant_id) or {}
    merged = dict(existing)
    merged.update(_normalize_central_sites_config(payload.model_dump()))
    store.set_tenant_central_sites_config(resolved_tenant_id, merged)
    return _normalize_central_sites_config(merged)


@router.get("/aggregate/central-status")
async def get_aggregate_central_status(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    """Aggregate Aruba Central status across spokes.
    Centralized mode: from hub's own polling. Distributed mode: from spoke relay telemetry.
    """
    resolved_tenant_id = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    mode = _central_mode(tenant)
    spokes = _approved_spokes(resolved_tenant_id)

    if mode == "centralized":
        from ...tasks import _cache_updated_at, _hub_central_status

        is_stale = time.time() - _cache_updated_at.get(resolved_tenant_id, 0) > 300
        tenant_data = {} if is_stale else _hub_central_status.get(resolved_tenant_id, {})
        token_valid = False if is_stale else bool(tenant_data.get("token_valid", False))
        token_state = "stale" if is_stale else tenant_data.get("token_state", "not_configured")
        aggregate_status = {} if is_stale or not isinstance(tenant_data.get("status"), dict) else tenant_data.get("status", {})
        wireless_clients = {} if is_stale or not isinstance(tenant_data.get("wireless_clients"), dict) else tenant_data.get("wireless_clients", {})
        hardware_alerts = [] if is_stale or not isinstance(tenant_data.get("hardware_alerts"), list) else tenant_data.get("hardware_alerts", [])
        client_count_status = {} if is_stale or not isinstance(tenant_data.get("client_count_status"), dict) else tenant_data.get("client_count_status", {})
        central_sites_config = _normalize_central_sites_config(store.get_tenant_central_sites_config(resolved_tenant_id))
        tenant_spokes_data = tenant_data.get("spokes", {}) if isinstance(tenant_data.get("spokes"), dict) else {}
        spoke_map = {spoke.id: spoke for spoke in spokes}
        ordered_spoke_ids = [spoke.id for spoke in spokes]
        ordered_spoke_ids.extend(spoke_id for spoke_id in tenant_spokes_data if spoke_id not in spoke_map)
        spokes_out = []
        for spoke_id in ordered_spoke_ids:
            spoke = spoke_map.get(spoke_id)
            spoke_data = tenant_spokes_data.get(spoke_id, {}) if isinstance(tenant_spokes_data.get(spoke_id), dict) else {}
            site_mappings = spoke_data.get("site_mappings", {}) if isinstance(spoke_data.get("site_mappings"), dict) else {}
            # Fall back to tenant-level config when spoke cache is empty (e.g. fresh start
            # before the background task has run). In centralized mode the hub monitors all
            # sites on behalf of every spoke, so the tenant config is always authoritative.
            if not site_mappings:
                site_mappings = dict(central_sites_config.get("site_mappings") or {}) if isinstance(central_sites_config.get("site_mappings"), dict) else {}
            status = spoke_data.get("status", {}) if isinstance(spoke_data.get("status"), dict) else {}
            wireless = spoke_data.get("wireless_clients", {}) if isinstance(spoke_data.get("wireless_clients"), dict) else {}
            hw_alerts = spoke_data.get("hardware_alerts", []) if isinstance(spoke_data.get("hardware_alerts"), list) else []
            sites = []
            for wsite, central_site in site_mappings.items():
                site_status = status.get(wsite, {}) if isinstance(status.get(wsite), dict) else {}
                ok = sum(1 for value in site_status.values() if isinstance(value, dict) and value.get("status") == "OK")
                fail = sum(1 for value in site_status.values() if isinstance(value, dict) and value.get("status") == "ERROR")
                unk = max(len(site_status) - ok - fail, 0)
                sites.append({
                    "wsite": wsite,
                    "central_site": central_site,
                    "check_ok": ok,
                    "check_fail": fail,
                    "check_unknown": unk,
                    "wireless_clients": wireless.get(wsite),
                    "status_map": site_status,
                })
            spokes_out.append({
                "spoke_id": spoke_id,
                "spoke_name": (spoke.spoke_name or spoke.hostname) if spoke else spoke_id,
                "hostname": spoke.hostname if spoke else "",
                "assigned_sites": spoke.assigned_sites if spoke else [s for s in [str(spoke_data.get("assigned_site") or "").strip()] if s],
                "spoke_online": _is_online(spoke) if spoke else False,
                "last_seen": spoke.last_seen if spoke else None,
                "sites": sites,
                "hardware_alerts": hw_alerts,
                "client_count_status": spoke_data.get("client_count_status", client_count_status),
            })
        return {
            "tenant_id": resolved_tenant_id,
            "mode": "centralized",
            "token_valid": token_valid,
            "token_state": token_state,
            "status": aggregate_status,
            "wireless_clients": wireless_clients,
            "hardware_alerts": hardware_alerts,
            "central_sites_config": central_sites_config,
            "client_count_status": client_count_status,
            "spokes": spokes_out,
        }

    spokes_out = []
    aggregate_client_count_status: dict[str, Any] = {}
    for spoke in spokes:
        central = _central_telemetry(spoke)
        site_mappings = central.get("site_mappings", {})
        status = central.get("status", {})
        wireless = central.get("wireless_clients", {})
        hw_alerts = central.get("hardware_alerts", [])
        client_count_status = central.get("client_count_status", {}) if isinstance(central.get("client_count_status"), dict) else {}
        for wsite, info in client_count_status.items():
            if wsite not in aggregate_client_count_status and isinstance(info, dict):
                aggregate_client_count_status[wsite] = info
        token_valid_spoke = bool(central.get("token_valid", False))
        token_state_spoke = central.get("token_state", {})
        sites = []
        for wsite, central_site in site_mappings.items():
            site_status = status.get(wsite, {})
            ok = sum(1 for value in site_status.values() if isinstance(value, dict) and value.get("status") == "OK")
            fail = sum(1 for value in site_status.values() if isinstance(value, dict) and value.get("status") == "ERROR")
            unk = max(len(site_status) - ok - fail, 0)
            sites.append({
                "wsite": wsite,
                "central_site": central_site,
                "check_ok": ok,
                "check_fail": fail,
                "check_unknown": unk,
                "wireless_clients": wireless.get(wsite),
                "status_map": site_status,
            })
        spokes_out.append({
            "spoke_id": spoke.id,
            "spoke_name": spoke.spoke_name or spoke.hostname,
            "hostname": spoke.hostname,
            "assigned_sites": spoke.assigned_sites,
            "spoke_online": _is_online(spoke),
            "last_seen": spoke.last_seen,
            "token_valid": token_valid_spoke,
            "token_state": token_state_spoke,
            "sites": sites,
            "hardware_alerts": hw_alerts,
            "client_count_status": client_count_status,
        })
    return {
        "tenant_id": resolved_tenant_id,
        "mode": "distributed",
        "token_valid": None,
        "token_state": None,
        "client_count_status": aggregate_client_count_status,
        "spokes": spokes_out,
    }


@router.get("/central/available")
async def hub_central_available(
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    """Return Aruba Central alert, insight, and hardware catalogs for the hub UI."""
    resolved_tid = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tid)
    if not tenant.aruba_config_enc:
        return {"alerts": [], "insights": [], "hardware": [], "warning": "Central not configured on hub."}
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
        cfg["cluster_url"] = validate_cluster_url(cfg.get("cluster_url", ""))
    except Exception as exc:
        logger.warning("Unable to read Aruba config for tenant %s: %s", resolved_tid, exc)
        return {"alerts": [], "insights": [], "hardware": [], "warning": "Could not read Central API config."}

    client = ArubaClient(cfg)
    if not client.is_configured():
        return {"alerts": [], "insights": [], "hardware": [], "warning": "Central not configured on hub."}
    try:
        return await client.available_checks()
    except Exception as exc:
        logger.warning("Unable to fetch Central catalog for tenant %s: %s", resolved_tid, exc)
        return {"alerts": [], "insights": [], "hardware": [], "warning": str(exc)}


@router.get("/central/devices")
async def hub_central_devices(
    site: str = Query(..., description="Site name to filter devices by"),
    tenant_id: Optional[str] = Query(None),
    current_user: User = Depends(auth.get_current_user),
):
    """Fetch network devices from Central API filtered by site name. Hub-side endpoint for CNX mode."""
    resolved_tid = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tid)

    if not tenant.aruba_config_enc:
        return {"devices": [], "count": 0, "warning": "Central API not configured on hub."}

    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception:
        return {"devices": [], "count": 0, "warning": "Could not decrypt Central API config."}

    api_version = cfg.get("api_version", "classic")
    if api_version != "new_central":
        return {"devices": [], "count": 0, "warning": "Device list requires New Central API mode."}

    cluster_url = (cfg.get("cluster_url") or "").rstrip("/")
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")

    if not all([cluster_url, client_id, client_secret]):
        return {"devices": [], "count": 0, "warning": "Central API credentials incomplete."}

    cfg["cluster_url"] = _validated_cluster_url_or_400(cluster_url)
    aruba_client = ArubaClient(cfg)

    try:
        async with httpx.AsyncClient() as client:
            access_token = await aruba_client._ensure_token(client)
            headers = aruba_client._headers(access_token)

            site_id = None
            site_lookup_error = ""
            try:
                sh_resp = await client.get(
                    f"{aruba_client.cluster_url}/network-monitoring/v1alpha1/sites-health",
                    headers=headers,
                    timeout=20,
                )
                if sh_resp.status_code != 200:
                    site_lookup_error = f"Sites health fetch failed: {sh_resp.status_code}"
                else:
                    for item in sh_resp.json().get("items", []):
                        sname = item.get("siteName") or item.get("site_name") or ""
                        if sname.lower() == site.lower():
                            site_id = item.get("siteId") or item.get("site_id")
                            break
            except Exception as exc:
                logger.warning("Central site lookup failed for tenant %s site %s: %s", resolved_tid, site, exc)
                site_lookup_error = str(exc)

            if site_lookup_error:
                return {
                    "devices": [],
                    "count": 0,
                    "warning": "Failed to look up site in Central.",
                    "error": site_lookup_error,
                }
            if not site_id:
                return {"devices": [], "count": 0, "warning": f"Site '{site}' not found in Central."}

            params: dict[str, Any] = {"limit": 500}
            if site_id:
                params["filter"] = f"siteId eq '{site_id}'"

            dev_resp = await client.get(
                f"{aruba_client.cluster_url}/network-monitoring/v1alpha1/devices",
                headers=headers,
                params=params,
                timeout=20,
            )
            if dev_resp.status_code != 200:
                return {"devices": [], "count": 0, "warning": f"Devices fetch failed: {dev_resp.status_code}"}

            raw_devices = dev_resp.json().get("items", [])
            if site_id:
                raw_devices = [d for d in raw_devices if d.get("siteId") == site_id]

            devices = [
                {
                    "name": d.get("deviceName") or d.get("id") or "—",
                    "type": d.get("deviceType", "—"),
                    "model": d.get("model", "—"),
                    "ip": d.get("ipv4") or d.get("ip", "—"),
                    "mac": d.get("macAddress") or d.get("mac", "—"),
                    "status": d.get("status", "—"),
                    "site": d.get("siteId", "—"),
                    "serial": d.get("serialNumber") or d.get("serial", "—"),
                    "sw_ver": d.get("softwareVersion") or d.get("firmwareVersion") or d.get("swVersion", "—"),
                }
                for d in raw_devices
            ]

            return {"devices": devices, "count": len(devices)}

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Error fetching Central devices for tenant %s site %s: %s", resolved_tid, site, exc)
        return {"devices": [], "count": 0, "warning": "Error fetching devices.", "error": str(exc)}


@router.get("/central/site-alerts")
async def hub_central_site_alerts(
    site: str = Query(..., description="Site name to fetch alerts for"),
    tenant_id: Optional[str] = Query(None),
    current_user: User = Depends(auth.get_current_user),
):
    """Fetch site alerts from Central API. Hub-side endpoint."""
    resolved_tid = _resolve_tenant_id(tenant_id, current_user)
    tenant = _get_tenant(resolved_tid)

    if not tenant.aruba_config_enc:
        return {"alerts": [], "count": 0, "warning": "Central API not configured on hub."}

    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception:
        return {"alerts": [], "count": 0, "warning": "Could not decrypt Central API config."}

    api_version = cfg.get("api_version", "classic")
    cluster_url = (cfg.get("cluster_url") or "").rstrip("/")
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")

    if not all([cluster_url, client_id, client_secret]):
        return {"alerts": [], "count": 0, "warning": "Central API credentials incomplete."}

    cfg["cluster_url"] = _validated_cluster_url_or_400(cluster_url)
    aruba_client = ArubaClient(cfg)

    try:
        async with httpx.AsyncClient() as client:
            access_token = await aruba_client._ensure_token(client)
            headers = aruba_client._headers(access_token)

            alerts: list[dict[str, Any]] = []
            ts_now = int(time.time())

            if api_version == "new_central":
                site_id = None
                health_score = None
                site_found = False
                site_lookup_error = ""
                try:
                    sh_resp = await client.get(
                        f"{aruba_client.cluster_url}/network-monitoring/v1alpha1/sites-health",
                        headers=headers,
                        timeout=20,
                    )
                    if sh_resp.status_code != 200:
                        site_lookup_error = f"Sites health fetch failed: {sh_resp.status_code}"
                    else:
                        for item in sh_resp.json().get("items", []):
                            sname = item.get("siteName") or item.get("site_name") or ""
                            if sname.lower() == site.lower():
                                site_found = True
                                site_id = item.get("siteId") or item.get("site_id")
                                health_score = int(item.get("healthScore", item.get("health_score", 100)))
                                break
                except Exception as exc:
                    logger.warning("Central alerts site lookup failed for tenant %s site %s: %s", resolved_tid, site, exc)
                    site_lookup_error = str(exc)

                if site_lookup_error:
                    return {
                        "alerts": [],
                        "count": 0,
                        "warning": "Failed to look up site in Central.",
                        "error": site_lookup_error,
                    }
                if not site_found:
                    return {"alerts": [], "count": 0, "warning": f"Site '{site}' not found in Central."}

                if health_score is not None and health_score < 100:
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

                if site_id:
                    device_fetch_error = ""
                    try:
                        params: dict[str, Any] = {"limit": 500, "filter": f"siteId eq '{site_id}'"}
                        dev_resp = await client.get(
                            f"{aruba_client.cluster_url}/network-monitoring/v1alpha1/devices",
                            headers=headers,
                            params=params,
                            timeout=20,
                        )
                        if dev_resp.status_code != 200:
                            device_fetch_error = f"Devices fetch failed: {dev_resp.status_code}"
                        else:
                            _TYPE_MAP = {
                                "ACCESS_POINT": ("AP_DOWN", "AP Down"),
                                "SWITCH": ("SWITCH_DOWN", "Switch Down"),
                                "GATEWAY": ("GATEWAY_DOWN", "Gateway Down"),
                            }
                            for dev in dev_resp.json().get("items", []):
                                if dev.get("siteId") != site_id:
                                    continue
                                status = (dev.get("status") or "").upper()
                                if status in ("UP", "ONLINE"):
                                    continue
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
                        logger.warning("Central device alert fetch failed for tenant %s site %s: %s", resolved_tid, site, exc)
                        device_fetch_error = str(exc)
                    if device_fetch_error:
                        return {
                            "alerts": alerts,
                            "count": len(alerts),
                            "warning": "Failed to fetch site devices from Central.",
                            "error": device_fetch_error,
                        }
            else:
                thirty_days_ago = ts_now - 30 * 86400
                alerts_fetch_error = ""
                for path in ["/monitoring/v1/alerts", "/monitoring/v2/alerts"]:
                    try:
                        resp = await client.get(
                            f"{aruba_client.cluster_url}{path}",
                            headers=headers,
                            params={"site": site, "limit": 500, "from_timestamp": thirty_days_ago},
                            timeout=20,
                        )
                        if resp.status_code == 200:
                            for alert in resp.json().get("alerts", []):
                                alert_site = alert.get("site_name") or alert.get("site") or ""
                                if alert_site and alert_site.lower() != site.lower():
                                    continue
                                alerts.append({
                                    "type": alert.get("alert_type") or alert.get("type", ""),
                                    "name": alert.get("alert_type_name") or alert.get("alert_type", ""),
                                    "severity": alert.get("severity", ""),
                                    "state": alert.get("state", ""),
                                    "site": alert.get("site_name") or site,
                                    "device": alert.get("device_name") or alert.get("hostname", ""),
                                    "ts": alert.get("timestamp") or alert.get("raised_at", ""),
                                    "message": alert.get("details") or alert.get("description", ""),
                                })
                            alerts_fetch_error = ""
                            break
                        if resp.status_code == 404:
                            continue
                        alerts_fetch_error = f"{path} returned {resp.status_code}"
                        logger.warning("Central alerts fetch failed for tenant %s site %s via %s: %s", resolved_tid, site, path, resp.status_code)
                    except Exception as exc:
                        logger.warning("Central alerts fetch failed for tenant %s site %s via %s: %s", resolved_tid, site, path, exc)
                        alerts_fetch_error = str(exc)
                        continue

                if alerts_fetch_error and not alerts:
                    return {
                        "alerts": [],
                        "count": 0,
                        "warning": "Failed to fetch site alerts from Central.",
                        "error": alerts_fetch_error,
                    }

            warning = None if alerts else "No alerts detected for this site."
            return {"alerts": alerts, "count": len(alerts), "warning": warning}

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Error fetching Central alerts for tenant %s site %s: %s", resolved_tid, site, exc)
        return {"alerts": [], "count": 0, "warning": "Error fetching alerts.", "error": str(exc)}


@router.get("/central/browse")
async def hub_central_browse(
    tenant_id: Optional[str] = Query(default=None),
    force: bool = Query(default=False),
    current_user: User = Depends(auth.get_current_user),
):
    """Return Central browse data from the in-memory cache (refreshed every 5 min by background task).
    force=true performs a synchronous refresh before returning the current payload."""
    resolved_tid = _resolve_tenant_id(tenant_id, current_user)

    # Load disk cache into memory if cold (e.g. first request after restart)
    if resolved_tid not in _central_browse_cache:
        disk = _load_browse_disk_cache(resolved_tid)
        if disk:
            _central_browse_cache[resolved_tid] = disk
            _central_browse_cache_ts[resolved_tid] = disk.get("cached_at", 0)

    cached = _central_browse_cache.get(resolved_tid)

    # Also refresh when clients is empty but clients_by_site has data — stale disk cache from old format
    def _needs_client_refresh(d: dict | None) -> bool:
        if not isinstance(d, dict):
            return False
        return not d.get("clients") and bool(d.get("clients_by_site"))

    if force or _has_legacy_client_summary_rows(cached) or _needs_client_refresh(cached):
        await _refresh_central_browse(resolved_tid)
        cached = _central_browse_cache.get(resolved_tid)

    if cached:
        normalized_cached = _normalize_browse_cache(cached) or {}
        if normalized_cached != cached:
            _central_browse_cache[resolved_tid] = normalized_cached
            _save_browse_disk_cache(resolved_tid, normalized_cached)
        return {**normalized_cached, "cached": True}

    # Nothing in memory or on disk yet — do a blocking fetch (first-ever load)
    await _refresh_central_browse(resolved_tid)
    cached = _central_browse_cache.get(resolved_tid, {})
    normalized_cached = _normalize_browse_cache(cached) or {}
    if normalized_cached != cached:
        _central_browse_cache[resolved_tid] = normalized_cached
        _save_browse_disk_cache(resolved_tid, normalized_cached)
    return {**normalized_cached, "cached": False}


@router.get("/{tenant_id}/aggregate/monitored-items")
async def get_monitored_items(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return all monitored items for a tenant (viewer-accessible for button state)."""
    resolved_tenant_id = _require_tenant_access(tenant_id, current_user)
    cfg = store.get_tenant_central_sites_config(resolved_tenant_id)
    items = cfg.get("monitored_items") if isinstance(cfg.get("monitored_items"), list) else []
    return {"items": items}


@router.post("/{tenant_id}/aggregate/monitored-items")
async def add_monitored_item(
    tenant_id: str,
    body: MonitoredItemCreate,
    current_user: User = Depends(auth.get_current_user),
):
    """Add an item to the monitored items list (idempotent by type + identifier)."""
    import uuid as _uuid
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    cfg = store.get_tenant_central_sites_config(resolved_tenant_id)
    items: list[dict[str, Any]] = list(cfg.get("monitored_items") or [])
    existing = next(
        (item for item in items if isinstance(item, dict)
         and item.get("type") == body.type
         and item.get("identifier") == body.identifier),
        None,
    )
    if existing:
        return {"item": existing, "created": False}
    new_item: dict[str, Any] = {
        "id": str(_uuid.uuid4()),
        "type": str(body.type),
        "name": str(body.name),
        "identifier": str(body.identifier),
        "added_at": time.time(),
        "consecutive_failures": 0,
        "last_seen": None,
        "last_notified": None,
        "status": "ok",
    }
    items.append(new_item)
    cfg["monitored_items"] = items
    store.set_tenant_central_sites_config(resolved_tenant_id, cfg)
    return {"item": new_item, "created": True}


@router.delete("/{tenant_id}/aggregate/monitored-items/{item_id}")
async def delete_monitored_item(
    tenant_id: str,
    item_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Remove a monitored item by ID."""
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    cfg = store.get_tenant_central_sites_config(resolved_tenant_id)
    items = [
        item for item in (cfg.get("monitored_items") or [])
        if isinstance(item, dict) and item.get("id") != item_id
    ]
    cfg["monitored_items"] = items
    store.set_tenant_central_sites_config(resolved_tenant_id, cfg)
    return {"ok": True}


@router.post("/aggregate/central")
async def update_aggregate_central(
    payload: CentralUpdateRequest,
    tenant_id: Optional[str] = Query(default=None),
    current_user: User = Depends(auth.get_current_user),
):
    requested_tenant_id = payload.tenant_id or tenant_id
    resolved_tenant_id = _require_tenant_admin(_resolve_tenant_id(requested_tenant_id, current_user), current_user)
    tenant = _get_tenant(resolved_tenant_id)
    mode = str(payload.mode or "distributed").strip().lower()
    if mode not in MODE_VALUES:
        raise HTTPException(status_code=400, detail="mode must be centralized or distributed")

    existing_cfg: dict[str, Any] = {}
    if tenant.aruba_config_enc:
        try:
            existing_cfg = decrypt_dict(tenant.aruba_config_enc)
        except Exception:
            existing_cfg = {}
    incoming = payload.hub_central_config.model_dump()
    cfg = {
        "api_version": str(incoming.get("api_version") or existing_cfg.get("api_version") or "classic"),
        "cluster_url": str(incoming.get("cluster_url") or "").strip(),
        "client_id": str(incoming.get("client_id") or "").strip(),
        "customer_id": str(incoming.get("customer_id") or "").strip(),
        "workspace_id": str(incoming.get("workspace_id") or "").strip(),
    }
    client_secret = str(incoming.get("client_secret") or "")
    if client_secret:
        cfg["client_secret"] = client_secret
    elif existing_cfg.get("client_secret"):
        cfg["client_secret"] = existing_cfg["client_secret"]
    # access_token: prefer newly submitted value; fall back to existing encrypted value
    access_token = str(incoming.get("access_token") or "").strip()
    if access_token:
        cfg["access_token"] = access_token
    elif existing_cfg.get("access_token"):
        cfg["access_token"] = existing_cfg["access_token"]
    for key in ("refresh_token", "webhook_id", "webhook_api_key"):
        if key == "refresh_token" and access_token:
            continue
        if existing_cfg.get(key):
            cfg[key] = existing_cfg[key]

    tenant.aruba_cid = cfg.get("customer_id") or tenant.aruba_cid
    tenant.central_browse_interval_minutes = max(1, min(60, payload.central_browse_interval_minutes or 5))
    # Safeguard: only update the encrypted config if the form actually contains values.
    # If all fields are empty (e.g. a masked form was submitted without changes), keep
    # the existing encrypted config rather than wiping it.  Use a dedicated DELETE
    # endpoint to intentionally clear the config.
    _has_new_aruba_values = (
        any(str(value).strip() for key, value in cfg.items() if key != "api_version")
        or bool(cfg.get("client_secret"))
    )
    if _has_new_aruba_values:
        tenant.aruba_config_enc = encrypt_dict(cfg)
    elif not tenant.aruba_config_enc:
        # No existing config and nothing new — store None (first-time empty save)
        tenant.aruba_config_enc = None
    # else: keep existing aruba_config_enc unchanged to prevent accidental wipe
    tenant.default_processing_mode.aruba_polling = mode
    store.save_tenant(tenant)

    # Bump config_version on all approved spokes so the new Central config
    # is queued as a config_update command on the next relay cycle.
    for spoke in _approved_spokes(resolved_tenant_id):
        spoke.config_version += 1
        store.save_spoke(spoke)
        store.ensure_config_update_command(resolved_tenant_id, spoke.id)

    if mode == "centralized" and tenant.aruba_config_enc:
        central_sites_config = _normalize_central_sites_config(store.get_tenant_central_sites_config(resolved_tenant_id))
        if cfg.get("api_version") == "new_central":
            if not central_sites_config.get("monitored_checks"):
                central_sites_config["monitored_checks"] = [dict(item) for item in DEFAULT_NEW_CENTRAL_MONITORED_CHECKS]
            if not central_sites_config.get("hardware_checks"):
                central_sites_config["hardware_checks"] = [dict(item) for item in DEFAULT_NEW_CENTRAL_HARDWARE_CHECKS]
        try:
            discover_client = ArubaClient(cfg)
            discovered_sites = await discover_client.list_sites() if discover_client.is_configured() else []
        except Exception as exc:
            logger.warning("Unable to auto-discover Aruba Central sites for tenant %s: %s", resolved_tenant_id, exc)
            discovered_sites = []
        existing_wsites = {str(name).strip().casefold() for name in central_sites_config.get("site_mappings", {})}
        existing_central = {str(name).strip().casefold() for name in central_sites_config.get("site_mappings", {}).values()}
        excluded = {str(s).strip().casefold() for s in central_sites_config.get("excluded_sites", []) if s}
        for site in discovered_sites:
            site_name = str((site or {}).get("name") or "").strip()
            if not site_name:
                continue
            normalized = site_name.casefold()
            if normalized in existing_wsites or normalized in existing_central or normalized in excluded:
                continue
            central_sites_config.setdefault("site_mappings", {})[site_name] = site_name
            existing_wsites.add(normalized)
            existing_central.add(normalized)
        store.set_tenant_central_sites_config(resolved_tenant_id, central_sites_config)

    return _aggregate_central_payload(resolved_tenant_id)


@router.post("/{tenant_id}/aggregate/central-clear-secrets")
async def clear_aggregate_central_secrets(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    resolved_tenant_id = _require_tenant_admin(tenant_id, current_user)
    tenant = _get_tenant(resolved_tenant_id)
    if not tenant.aruba_config_enc:
        return _serialize_hub_central_config(tenant)
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to decrypt Aruba Central credentials: {exc}") from exc
    cfg.pop("client_secret", None)
    cfg.pop("access_token", None)
    cfg.pop("refresh_token", None)
    _persist_aruba_config(tenant, cfg)
    return _serialize_hub_central_config(tenant)
