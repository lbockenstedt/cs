"""Misc API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    APP_VERSION,
    Any,
    Body,
    CLIENT_COUNT_BASELINE_FILE,
    CLIENT_HISTORY_FILE,
    CLIENT_HISTORY_JSONL,
    COMMAND_QUEUE_FILE,
    FileResponse,
    HISTORY_FILE,
    HTTPException,
    INSTALLER_VERSION,
    PlainTextResponse,
    Query,
    RECLONE_STATE_FILE,
    RELAY_STATE_FILE,
    REPO_DIR,
    Request,
    STATE_CACHE_FILE,
    UPDATE_STATE_FILE,
    _acme_challenges,
    _acme_status,
    _central_status_payload,
    _central_token_state,
    _client_count_payload,
    _git_lock,
    _hw_alerts_payload,
    _normalize_toggle,
    _proxmox_status_payload,
    _public_acme_settings,
    _public_central_api_settings,
    _read_local_kill_switch,
    _relay_registration_status_from_settings,
    _relay_status_payload,
    _run_acme_request,
    _save_relay_state,
    _save_settings,
    asyncio,
    background_tasks,
    broadcast,
    central_token,
    clients,
    copy,
    gkill_switch_state,
    history_lock,
    logger,
    proxmox_state,
    reclone_state,
    relay_loop,
    relay_state,
    repo_path,
    repo_state,
    settings,
    socket,
    spoke_acme,
    state,
    state_lock,
    sync_repo_once,
    time,
    validate_platform,
)

router = APIRouter()




@router.post("/api/bootstrap")
async def api_bootstrap(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, str]:
    """One-time hub configuration — only accepted from localhost (via qm guest exec / pct exec).

    Security model:
      - Only 127.0.0.1 / ::1 can call this endpoint — enforced server-side.
      - If relay_server_url is already configured the endpoint returns 409 (idempotent lock).
      - The installer invokes this via `qm guest exec` / `pct exec` so the request never
        crosses the network; an external caller cannot reach it.
    """

    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning("Bootstrap attempt rejected from non-localhost %s", client_host)
        raise HTTPException(status_code=403, detail="bootstrap only accepted from localhost")

    if settings.get("relay_server_url", "").strip():
        raise HTTPException(status_code=409, detail="hub already configured — bootstrap is one-time only")

    hub_url = str(body.get("relay_server_url", "") or "").strip()
    tenant_id = str(body.get("relay_tenant_id", "") or "").strip()
    onboarding_psk = str(body.get("relay_onboarding_psk", "") or "").strip()

    if not hub_url:
        raise HTTPException(status_code=422, detail="relay_server_url is required")

    settings["relay_server_url"] = hub_url
    if tenant_id:
        settings["relay_tenant_id"] = tenant_id
        settings["relay_tenant_hint"] = tenant_id
    if onboarding_psk:
        settings["relay_onboarding_psk"] = onboarding_psk
    settings["relay_enabled"] = "on"
    _save_settings()

    relay_state.update({
        "enabled": True,
        "connected": False,
        "error": None,
        "registration_status": _relay_registration_status_from_settings(),
    })
    state.relay_registration_refresh_needed = True
    _save_relay_state()
    task = background_tasks.get("relay")
    if task and not task.done():
        task.cancel()
    background_tasks["relay"] = asyncio.get_event_loop().create_task(relay_loop())

    logger.info("Bootstrap: hub configured to %s (tenant: %s)", hub_url, tenant_id or "none")
    return {"status": "ok", "relay_server_url": hub_url, "relay_tenant_id": tenant_id, "has_psk": bool(onboarding_psk)}






@router.get("/api/acme")
async def api_acme_get() -> dict[str, Any]:
    cfg = spoke_acme.load_acme_config()
    data = _public_acme_settings(cfg)
    data["cert_info"] = spoke_acme.get_cert_info()
    data["spoke_tls"] = settings.get("spoke_tls", "off")
    return data




@router.post("/api/acme")
async def api_acme_update(payload: dict[str, Any]) -> dict[str, Any]:
    existing = spoke_acme.load_acme_config()
    incoming_credentials = payload.get("dns_credentials") or {}
    merged_credentials = dict(existing.dns_credentials or {})
    for key, value in incoming_credentials.items():
        if value in (None, "***"):
            continue
        merged_credentials[key] = value
    cfg = spoke_acme.AcmeConfig(
        enabled=bool(payload.get("enabled", existing.enabled)),
        domain=str(payload.get("domain", existing.domain) or "").strip(),
        email=str(payload.get("email", existing.email) or "").strip(),
        challenge=str(payload.get("challenge", existing.challenge) or existing.challenge),
        ca=str(payload.get("ca", existing.ca) or existing.ca),
        dns_provider=str(payload.get("dns_provider", existing.dns_provider) or "").strip(),
        dns_credentials=merged_credentials,
        last_renewed=existing.last_renewed,
        last_error=existing.last_error,
        cert_expiry=existing.cert_expiry,
    )
    spoke_acme.save_acme_config(cfg)
    if "spoke_tls" in payload:
        settings["spoke_tls"] = _normalize_toggle(payload.get("spoke_tls"))
        _save_settings()
    data = _public_acme_settings(cfg)
    data["cert_info"] = spoke_acme.get_cert_info()
    data["spoke_tls"] = settings.get("spoke_tls", "off")
    return data




@router.post("/api/acme/request")
async def api_acme_request() -> dict[str, Any]:
    if _acme_status.get("running"):
        return {"status": "running"}
    asyncio.create_task(_run_acme_request())
    return {"status": "started"}




@router.get("/api/acme/status")
async def api_acme_status() -> dict[str, Any]:
    return dict(_acme_status)




@router.get("/api/scripts/list")
async def api_scripts_list(platform: str = Query(...)) -> list[str]:
    platform = validate_platform(platform)
    scripts_dir = repo_path(platform)
    if not scripts_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Script directory not found for {platform}")
    return sorted(path.name for path in scripts_dir.iterdir() if path.is_file())




@router.get("/api/scripts/{platform}/{filename}")
async def api_scripts_get(platform: str, filename: str) -> FileResponse:
    platform = validate_platform(platform)
    scripts_dir = repo_path(platform).resolve()
    candidate = (scripts_dir / filename).resolve()

    if candidate.parent != scripts_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Script file not found")

    return FileResponse(candidate)




@router.get("/api/init")
async def api_init() -> dict[str, Any]:
    """Single endpoint that returns all state needed for initial page render.
    Replaces 5+ separate REST calls made on page load."""
    cfg = dict(settings["central_config"])
    for secret_key in ("client_secret", "access_token", "refresh_token"):
        cfg.pop(secret_key, None)
    cfg["access_token_configured"] = bool(settings["central_config"].get("access_token") or central_token.get("access_token"))
    cfg["refresh_token_configured"] = bool(settings["central_config"].get("refresh_token") or central_token.get("refresh_token"))
    cfg["client_secret_configured"] = bool(settings["central_config"].get("client_secret"))
    return {
        "mode": "spoke",
        "proxmox": _proxmox_status_payload(),
        "settings": {
            "central_api": _public_central_api_settings(),
            "central_config": cfg,
            "relay_enabled": settings.get("relay_enabled", "off"),
            "relay_server_url": settings.get("relay_server_url", ""),
            "hub_tls_verify": settings.get("hub_tls_verify", "off"),
            "hub_managed": bool(settings.get("hub_managed", False)),
            "hub_isolation_timeout": int(settings.get("hub_isolation_timeout", 3600)),  # Include the timeout in init settings so the setup form has the correct safeguard value before a separate settings fetch finishes.
            "proxmox_config": copy.deepcopy(settings.get("proxmox_config") or {}),
        },
        "reclone": dict(reclone_state),
        "update_all": dict(state.update_all_state),
        "central": {
            "status": _central_status_payload(),
            "wireless_clients": dict(state.central_wireless_clients),
            "hardware_alerts": _hw_alerts_payload(),
            "client_count_status": _client_count_payload(),
            "token_valid": bool(central_token.get("access_token") and time.time() < central_token.get("expires_at", 0)),
            "token_state": _central_token_state(),
        },
        "relay": _relay_status_payload(),
        "installer_version": INSTALLER_VERSION,
        "app_version": APP_VERSION,
        "hostname": socket.gethostname(),
        "kill_switch": gkill_switch_state["value"],
        "local_kill_switch": _read_local_kill_switch(),
    }




@router.get("/api/qa/summary")
async def api_qa_summary() -> dict[str, Any]:
    """Spoke-level QA summary: dongles, VMs, reporting clients, and pass/fail.

    Cross-references USB dongle count against provisioned VMs and actively
    reporting clients so Copilot (or any automated check) can assert the full
    auto-provisioning pipeline is healthy on this spoke.
    """
    async with state_lock:
        proxmox_connected = bool(proxmox_state.get("connected", False))
        present_usb: list[Any] = list(proxmox_state.get("present_usb") or [])
        usb_state: list[Any] = list(proxmox_state.get("usb_state") or [])
        vms: list[Any] = list(proxmox_state.get("vms") or [])
        reporting_clients = len(clients)

    dongle_count = len(present_usb) if present_usb else len(usb_state)
    # Only count sim-client VMs — those with a USB dongle assigned (in usb_state).
    # Templates, IoT VMs, and other non-sim VMs must not be included in this total.
    usb_vmids = {str(e.get("vmid")) for e in usb_state if e.get("vmid") is not None}
    sim_vm_count = sum(1 for vm in vms if str(vm.get("vmid", "")) in usb_vmids)
    auto_provision = _normalize_toggle(settings.get("usb_auto_provision", "off")) == "on"

    issues: list[str] = []
    if not proxmox_connected:
        issues.append("Proxmox agent is not connected")
    if auto_provision and dongle_count > 0 and sim_vm_count != dongle_count:
        issues.append(
            f"VM count ({sim_vm_count}) does not match dongle count ({dongle_count})"
        )
    if dongle_count > 0 and reporting_clients != dongle_count:
        issues.append(
            f"reporting clients ({reporting_clients}) does not match dongle count ({dongle_count})"
        )

    return {
        "proxmox_agent_connected": proxmox_connected,
        "dongle_count": dongle_count,
        "vm_count": sim_vm_count,
        "total_vm_count": len(vms),
        "reporting_clients": reporting_clients,
        "auto_provision": auto_provision,
        "pass": len(issues) == 0,
        "issues": issues,
    }




@router.post("/api/setup/clear-cache")
async def api_setup_clear_cache() -> dict[str, Any]:
    """Wipe all cached files, re-clone the repo, clear in-memory client/central state,
    then restart the WebUI service so it starts completely fresh."""
    import shutil

    # 1. Delete cached data files
    for path in [
        CLIENT_HISTORY_FILE,
        CLIENT_HISTORY_JSONL,
        STATE_CACHE_FILE,
        COMMAND_QUEUE_FILE,
        RECLONE_STATE_FILE,
        RELAY_STATE_FILE,
        UPDATE_STATE_FILE,
        HISTORY_FILE,
        CLIENT_COUNT_BASELINE_FILE,
    ]:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # 2. Clear in-memory state
    async with state_lock:
        clients.clear()
    async with history_lock:
        state.central_history.clear()
    state.central_wireless_clients.clear()

    # 3. Remove any stale git lock and wipe + re-clone the repo
    async with _git_lock:
        lock_file = REPO_DIR / ".git" / "index.lock"
        lock_file.unlink(missing_ok=True)
        try:
            shutil.rmtree(REPO_DIR, ignore_errors=True)
        except Exception as exc:
            logger.warning("clear-cache: could not remove REPO_DIR: %s", exc)
        try:
            await asyncio.to_thread(sync_repo_once)
            repo_state["synced"] = True
            repo_state["error"] = None
            repo_state["last_sync"] = time.time()
        except Exception as exc:
            logger.warning("clear-cache: re-clone failed: %s", exc)
            repo_state["error"] = str(exc)

    logger.info("Setup cache cleared by user request — restarting service")
    await broadcast({"type": "notification", "level": "info",
                     "message": "Cache cleared — service restarting in 2 seconds…"})

    # 4. Restart the service after a short delay so the response can be sent
    async def _delayed_restart() -> None:
        await asyncio.sleep(2)
        try:
            await asyncio.create_subprocess_shell(
                "sudo -n systemctl restart client-sim-dashboard",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.error("clear-cache: restart failed: %s", exc)

    asyncio.create_task(_delayed_restart())
    return {"status": "ok", "message": "Cache cleared — service restarting"}




@router.get("/.well-known/acme-challenge/{token}", include_in_schema=False)
async def acme_challenge(token: str):
    key_authorization = _acme_challenges.get(token)
    if not key_authorization:
        raise HTTPException(404)
    return PlainTextResponse(key_authorization)
