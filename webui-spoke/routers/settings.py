"""Settings API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    Body,
    HTTPException,
    HUB_LOCAL_ALLOWED_KEYS,
    RELAY_INTERVAL_DEFAULT,
    SettingsUpdate,
    _HW_FRIENDLY,
    _auto_device_type,
    _broadcast_proxmox_state,
    _broadcast_relay_state,
    _clamp_relay_interval,
    _clear_provision_halt_state,
    _ensure_json_list,
    _legacy_template_id,
    _normalize_central_api_settings,
    _normalize_relay_enabled,
    _normalize_spoke_auth_provider,
    _normalize_toggle,
    _normalize_vmid_spec,
    _parse_json_list,
    _parse_protected_vmids,
    _parse_reclone_schedule,
    _persisted,
    _primary_template_id,
    _public_settings,
    _relay_registration_status_from_settings,
    _relay_status_payload,
    _resolved_template_spec,
    _save_relay_state,
    _save_settings,
    _spoke_sessions,
    _sync_central_runtime_config,
    _validate_template_specs,
    asyncio,
    background_tasks,
    broadcast,
    central_token,
    contextlib,
    proxmox_state,
    re,
    relay_loop,
    relay_state,
    settings,
    state,
    sync_repo,
    time,
)

router = APIRouter()




@router.get("/api/settings")
async def api_settings_get() -> dict[str, Any]:
    return _public_settings()




@router.post("/api/settings")
async def api_settings_update(update: SettingsUpdate) -> dict[str, Any]:
    changed_branch = False
    relay_config_changed = False
    auth_provider_changed = False
    autoprov_disabled = False
    update_data = update.model_dump(exclude_none=True)

    if settings.get("hub_managed"):
        non_relay = set(update_data.keys()) - HUB_LOCAL_ALLOWED_KEYS
        if non_relay:
            raise HTTPException(status_code=403, detail="Settings are hub-managed. Only relay settings can be changed locally.")

    if update.repo_branch is not None:
        branch = update.repo_branch.strip()
        if not branch or not re.match(r'^[a-zA-Z0-9._/\-]+$', branch):
            raise HTTPException(status_code=422, detail="Invalid branch name")
        settings["repo_branch"] = branch
        changed_branch = True

    if update.github_token is not None:
        settings["github_token"] = update.github_token.strip()

    if update.relay_server_url is not None:
        settings["relay_server_url"] = update.relay_server_url.strip()
        relay_config_changed = True

    if update.hub_tls_verify is not None:
        settings["hub_tls_verify"] = _normalize_relay_enabled(update.hub_tls_verify)
        relay_config_changed = True

    if update.relay_spoke_name is not None:
        settings["relay_spoke_name"] = update.relay_spoke_name.strip()
        relay_config_changed = True

    if update.relay_tenant_hint is not None:
        tenant_id = update.relay_tenant_hint.strip()
        settings["relay_tenant_hint"] = tenant_id
        settings["relay_tenant_id"] = tenant_id
        relay_config_changed = True

    if update.relay_api_key is not None:
        settings["relay_api_key"] = update.relay_api_key.strip()
        relay_config_changed = True

    if update.relay_spoke_id is not None:
        settings["relay_spoke_id"] = update.relay_spoke_id.strip()
        relay_config_changed = True

    if update.relay_tenant_id is not None:
        tenant_id = update.relay_tenant_id.strip()
        settings["relay_tenant_id"] = tenant_id
        settings["relay_tenant_hint"] = tenant_id
        relay_config_changed = True

    if update.relay_enabled is not None:
        settings["relay_enabled"] = _normalize_relay_enabled(update.relay_enabled)
        relay_config_changed = True

    if update.relay_poll_interval is not None:
        settings["relay_poll_interval"] = _clamp_relay_interval(update.relay_poll_interval)
        relay_config_changed = True

    if update.hub_isolation_timeout is not None:  # Accept timeout edits from the setup UI so operators can tune when stale hub contact pauses pushes.
        settings["hub_isolation_timeout"] = max(300, min(86400, int(update.hub_isolation_timeout)))  # Clamp the safeguard window so operators stay within the supported 5-minute to 24-hour range.
        relay_config_changed = True  # Treat timeout edits as relay changes so isolation status is re-broadcast immediately when the threshold moves.
        _save_settings()  # Persist the new timeout right away so the safeguard survives crashes even before the handler reaches its shared save call.

    if update.admin_password is not None:
        settings["admin_password"] = update.admin_password.strip()
        _spoke_sessions.clear()

    if update.session_timeout_minutes is not None:
        settings["session_timeout_minutes"] = max(5, min(1440, int(update.session_timeout_minutes)))

    if update.auth_provider is not None:
        next_provider = _normalize_spoke_auth_provider(update.auth_provider)
        if next_provider != _normalize_spoke_auth_provider(settings.get("auth_provider", "local")):
            auth_provider_changed = True
        settings["auth_provider"] = next_provider

    for key in (
        "auth_ldap_url",
        "auth_ldap_bind_dn",
        "auth_ldap_bind_password",
        "auth_ldap_user_base",
        "auth_ldap_user_filter",
        "auth_ldap_group_admin",
        "auth_ldap_group_viewer",
        "auth_radius_host",
        "auth_radius_secret",
        "auth_radius_role_attr",
        "auth_radius_admin_val",
        "auth_tacacs_host",
        "auth_tacacs_secret",
    ):
        value = getattr(update, key)
        if value is not None:
            settings[key] = str(value).strip()

    if update.auth_radius_port is not None:
        settings["auth_radius_port"] = max(1, min(65535, int(update.auth_radius_port)))

    if update.auth_tacacs_port is not None:
        settings["auth_tacacs_port"] = max(1, min(65535, int(update.auth_tacacs_port)))

    if update.auth_tacacs_admin_priv is not None:
        settings["auth_tacacs_admin_priv"] = max(0, int(update.auth_tacacs_admin_priv))

    if auth_provider_changed:
        _spoke_sessions.clear()

    if relay_config_changed:
        relay_state.update({
            "enabled": settings.get("relay_enabled") == "on" and bool(settings.get("relay_server_url")),
            "connected": False,
            "error": None,
            "registration_status": _relay_registration_status_from_settings(),
        })
        state.relay_registration_refresh_needed = bool(relay_state["enabled"])
        _save_relay_state()
        # Kick the relay loop immediately instead of waiting for the next poll interval
        task = background_tasks.get("relay")
        if task and not task.done():
            task.cancel()
        background_tasks["relay"] = asyncio.get_event_loop().create_task(relay_loop())

    if update.central_api is not None:
        merged_api = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
        mode = str(update.central_api.get("mode", merged_api.get("mode", "classic"))).strip().lower()
        if mode not in {"classic", "central"}:
            raise HTTPException(status_code=422, detail="central_api.mode must be 'classic' or 'central'")
        merged_api["mode"] = mode

        classic_update = update.central_api.get("classic")
        if isinstance(classic_update, dict):
            for key in ("url", "username"):
                if key in classic_update:
                    merged_api["classic"][key] = str(classic_update.get(key, "")).strip()
            if "password" in classic_update:
                merged_api["classic"]["password"] = str(classic_update.get("password", ""))

        central_update = update.central_api.get("central")
        if isinstance(central_update, dict):
            for key in ("url", "client_id", "customer_id"):
                if key in central_update:
                    merged_api["central"][key] = str(central_update.get(key, "")).strip()
            if "client_secret" in central_update:
                merged_api["central"]["client_secret"] = str(central_update.get("client_secret", ""))

        settings["central_api"] = merged_api
        _sync_central_runtime_config()

    if update.central_config is not None:
        merged = dict(settings["central_config"])
        # Only update keys that are explicitly provided so omitted secrets are preserved.
        for key in ("cluster_url", "client_id", "customer_id", "api_version"):
            if key in update.central_config:
                merged[key] = update.central_config[key].strip()
        for secret_key in ("client_secret", "access_token", "refresh_token"):
            if secret_key in update.central_config:
                merged[secret_key] = update.central_config.get(secret_key, "").strip()
        # Switching to New Central — clear stale classic tokens from runtime
        if merged.get("api_version") == "new_central":
            central_token["access_token"] = None
            central_token["refresh_token"] = None
            central_token["expires_at"] = 0.0
        settings["central_config"] = merged
        merged_api = _normalize_central_api_settings(settings.get("central_api", {}), merged)
        if merged.get("api_version") == "new_central":
            merged_api["mode"] = "central"
            merged_api["central"].update({
                "url": merged.get("cluster_url", "").strip(),
                "client_id": merged.get("client_id", "").strip(),
                "client_secret": merged.get("client_secret", ""),
                "customer_id": merged.get("customer_id", "").strip(),
            })
        settings["central_api"] = merged_api
        # Classic: load new tokens into runtime state immediately
        if merged.get("api_version", "classic") == "classic":
            central_token["access_token"] = merged.get("access_token") or None
            central_token["refresh_token"] = merged.get("refresh_token") or None
            central_token["expires_at"] = time.time() + 7200 if merged.get("access_token") else 0.0

    if update.site_mappings is not None:
        settings["site_mappings"] = {k.strip(): v.strip() for k, v in update.site_mappings.items() if k.strip()}

    if update.monitored_checks is not None:
        settings["monitored_checks"] = [
            {"type": c.get("type", ""), "id": c.get("id", ""), "name": c.get("name", c.get("id", ""))}
            for c in update.monitored_checks
            if c.get("type") and c.get("id")
        ]

    if update.hardware_checks is not None:
        settings["hardware_checks"] = [
            {
                "id": c.get("id", ""),
                "name": c.get("name") or _HW_FRIENDLY.get(c.get("id", ""), c.get("id", "")),
                "device_type": c.get("device_type") or _auto_device_type(c.get("id", "")),
            }
            for c in update.hardware_checks
            if c.get("id")
        ]

    if update.notifications is not None:
        merged_notif = dict(settings.get("notifications", {}))
        merged_notif.update(update.notifications)
        # Ensure smtp_to is always a list
        if isinstance(merged_notif.get("smtp_to"), str):
            merged_notif["smtp_to"] = [a.strip() for a in merged_notif["smtp_to"].split(",") if a.strip()]
        settings["notifications"] = merged_notif

    if update.repo_sync_interval is not None:
        interval = max(60, min(86400, update.repo_sync_interval))  # clamp 1min–24hr
        settings["repo_sync_interval"] = interval

    if update.usb_vidpids is not None:
        settings["usb_vidpids"] = _ensure_json_list(update.usb_vidpids.strip(), "usb_vidpids")

    if update.usb_missing_timeout is not None:
        settings["usb_missing_timeout"] = str(max(1, int(update.usb_missing_timeout.strip() or "60")))

    if any(value is not None for value in (
        update.usb_template_id,
        update.vm_image_1_template_id,
        update.vm_image_1_template_spec,
        update.vm_image_2_template_id,
        update.vm_image_2_template_spec,
    )):
        spec1_raw = update.vm_image_1_template_spec
        if spec1_raw is None:
            spec1_raw = update.vm_image_1_template_id
        if spec1_raw is None:
            spec1_raw = update.usb_template_id
        spec2_raw = update.vm_image_2_template_spec
        if spec2_raw is None:
            spec2_raw = update.vm_image_2_template_id

        try:
            spec1 = _resolved_template_spec(settings, 1) if spec1_raw is None else _normalize_vmid_spec(spec1_raw, field_name="vm_image_1_template_spec")
            spec2 = _resolved_template_spec(settings, 2) if spec2_raw is None else _normalize_vmid_spec(spec2_raw, field_name="vm_image_2_template_spec")
            _validate_template_specs(spec1, spec2)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        settings["vm_image_1_template_spec"] = spec1
        settings["vm_image_2_template_spec"] = spec2
        settings["vm_image_1_template_id"] = _primary_template_id(spec1, _legacy_template_id(settings, 1))
        settings["vm_image_2_template_id"] = _primary_template_id(spec2, _legacy_template_id(settings, 2))

    if update.vm_image_1_pct is not None:
        settings["vm_image_1_pct"] = str(max(0, min(100, int(update.vm_image_1_pct.strip() or "50"))))

    if update.usb_auto_provision is not None:
        settings["usb_auto_provision"] = _normalize_toggle(update.usb_auto_provision)
        autoprov_disabled = settings["usb_auto_provision"] != "on"

    if update.use_all_dongles is not None:
        settings["use_all_dongles"] = bool(update.use_all_dongles)  # validated: always boolean

    if update.usb_max_slots is not None:
        settings["usb_max_slots"] = str(max(1, min(256, int(update.usb_max_slots.strip() or "24"))))

    def _clamp_pct(val: str, default: str) -> str:
        return str(max(0, min(100, int(val.strip() or default))))

    if update.cpu_provision_threshold is not None:
        settings["cpu_provision_threshold"] = _clamp_pct(update.cpu_provision_threshold, "80")
    if update.cpu_delete_threshold is not None:
        settings["cpu_delete_threshold"] = _clamp_pct(update.cpu_delete_threshold, "90")
    if update.mem_provision_threshold is not None:
        settings["mem_provision_threshold"] = _clamp_pct(update.mem_provision_threshold, "80")
    if update.mem_delete_threshold is not None:
        settings["mem_delete_threshold"] = _clamp_pct(update.mem_delete_threshold, "90")

    if update.vmid_start is not None:
        settings["vmid_start"] = max(0, int(update.vmid_start))

    if update.usb_ignored_vidpids is not None:
        settings["usb_ignored_vidpids"] = _ensure_json_list(update.usb_ignored_vidpids.strip(), "usb_ignored_vidpids")

    if update.ignored_hostnames is not None:
        settings["ignored_hostnames"] = _ensure_json_list(update.ignored_hostnames.strip(), "ignored_hostnames")

    if update.vm_silent_timeout is not None:
        settings["vm_silent_timeout"] = str(max(1, int(update.vm_silent_timeout.strip() or "24")))

    if update.reclone_schedule_enabled is not None:
        settings["reclone_schedule_enabled"] = _normalize_toggle(update.reclone_schedule_enabled)

    if update.reclone_schedule_cron is not None:
        cron_value = update.reclone_schedule_cron.strip().lower() or "sunday 02:00"
        if _parse_reclone_schedule(cron_value) is None:
            raise HTTPException(status_code=422, detail="reclone_schedule_cron must be in '<day> HH:MM' format")
        settings["reclone_schedule_cron"] = cron_value

    if update.reclone_concurrency is not None:
        settings["reclone_concurrency"] = str(max(1, int(update.reclone_concurrency.strip() or "1")))

    if update.protected_vmids is not None:
        # Normalize to a clean comma-separated list of ints and ranges (e.g. "101, 100-90000")
        raw = str(update.protected_vmids or "")
        parsed_strs = []
        for entry in _parse_protected_vmids(raw):
            if isinstance(entry, tuple):
                lo, hi = entry
                parsed_strs.append(f"{lo}-{hi}")
            else:
                parsed_strs.append(str(entry))
        settings["protected_vmids"] = ", ".join(parsed_strs)

    if update.l1_vlan_start is not None:
        settings["l1_vlan_start"] = str(max(1, min(4094, int(update.l1_vlan_start.strip() or "100"))))

    if update.l1_vlan_end is not None:
        settings["l1_vlan_end"] = str(max(1, min(4094, int(update.l1_vlan_end.strip() or "199"))))

    if update.guest_agent_watchdog_enabled is not None:
        settings["guest_agent_watchdog_enabled"] = _normalize_toggle(update.guest_agent_watchdog_enabled)
    if update.guest_agent_grace_minutes is not None:
        settings["guest_agent_grace_minutes"] = str(max(1, int(update.guest_agent_grace_minutes.strip() or "20")))
    if update.guest_agent_check_interval_minutes is not None:
        settings["guest_agent_check_interval_minutes"] = str(max(1, int(update.guest_agent_check_interval_minutes.strip() or "10")))
    if update.guest_agent_reboot_after_minutes is not None:
        settings["guest_agent_reboot_after_minutes"] = str(max(1, int(update.guest_agent_reboot_after_minutes.strip() or "10")))
    if update.guest_agent_reclone_after_minutes is not None:
        settings["guest_agent_reclone_after_minutes"] = str(max(1, int(update.guest_agent_reclone_after_minutes.strip() or "30")))
    if update.watchdog_reboot_enabled is not None:
        settings["watchdog_reboot_enabled"] = _normalize_toggle(update.watchdog_reboot_enabled)

    if update.proxmox_api_token is not None:
        token = update.proxmox_api_token.strip()
        settings["proxmox_api_token"] = token
        _persisted["proxmox_api_token"] = token

    if update.spoke_tls is not None:
        settings["spoke_tls"] = _normalize_toggle(update.spoke_tls)

    _save_settings()

    if autoprov_disabled:
        _clear_provision_halt_state()

    if changed_branch:
        if "sync_repo" in background_tasks:
            background_tasks["sync_repo"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_tasks["sync_repo"]
        background_tasks["sync_repo"] = asyncio.create_task(sync_repo())

    # Re-filter unknown_usb immediately so subsequent proxmox_update broadcasts don't
    # restore devices the user just certified or ignored.
    if update.usb_vidpids is not None or update.usb_ignored_vidpids is not None:
        _new_certified: set[str] = {
            str(item.get("vidpid", "")).strip().lower()
            for item in _parse_json_list(settings.get("usb_vidpids", "[]"))
            if isinstance(item, dict) and item.get("vidpid")
        }
        _new_ignored: set[str] = {
            str(v).strip().lower()
            for v in _parse_json_list(settings.get("usb_ignored_vidpids", "[]"))
            if str(v).strip()
        }
        _exclude = _new_certified | _new_ignored
        proxmox_state["unknown_usb"] = [
            d for d in proxmox_state.get("unknown_usb", [])
            if str(d.get("vidpid", "")).strip()
            and str(d.get("vidpid", "")).strip().lower() not in _exclude
        ]

    payload = await api_settings_get()
    await broadcast({"type": "settings_update", "settings": payload})
    if autoprov_disabled:
        await _broadcast_proxmox_state()
    if relay_config_changed:
        await _broadcast_relay_state()
    return {"status": "ok", "settings": payload}




@router.post("/api/settings/clear/{provider}")
async def api_settings_clear(provider: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    provider_key = provider.strip().lower()
    changed_branch = False
    relay_config_changed = False
    relay_payload: dict[str, Any] | None = None

    if provider_key == "github":
        changed_branch = bool(settings.get("repo_branch"))
        settings["repo_branch"] = ""
        settings["github_token"] = ""
    elif provider_key == "relay":
        settings.update({
            "relay_enabled": "off",
            "relay_server_url": "",
            "hub_tls_verify": "off",
            "relay_spoke_name": "",
            "relay_tenant_hint": "",
            "relay_api_key": "",
            "relay_spoke_id": "",
            "relay_tenant_id": "",
            "relay_poll_interval": RELAY_INTERVAL_DEFAULT,
        })
        relay_state.update({
            "enabled": False,
            "connected": False,
            "last_sync": None,
            "error": None,
            "registration_status": "unregistered",
            "api_key_configured": bool(settings.get("relay_api_key")),
        })
        # NOTE(refactor): the original code assigned `relay_registration_refresh_needed = False`
        # here WITHOUT a `global` declaration, so it was a write-only no-op local that never
        # affected the module global. Preserved verbatim as a local (renamed to avoid colliding
        # with the migrated state.relay_registration_refresh_needed) to keep behavior identical.
        _relay_registration_refresh_needed_local_noop = False  # noqa: F841 (preserved dead store)
        _save_relay_state()
        relay_config_changed = True
        relay_payload = _relay_status_payload()
    elif provider_key == "central":
        requested_mode = str((payload or {}).get("mode") or settings.get("central_api", {}).get("mode", "classic")).strip().lower()
        if requested_mode not in {"classic", "central"}:
            raise HTTPException(status_code=422, detail="mode must be 'classic' or 'central'")
        central_api_cfg = _normalize_central_api_settings(settings.get("central_api", {}), settings.get("central_config", {}))
        central_api_cfg["mode"] = requested_mode
        if requested_mode == "classic":
            central_api_cfg["classic"] = {"url": "", "username": "", "password": ""}
        else:
            central_api_cfg["central"] = {"url": "", "client_id": "", "client_secret": "", "customer_id": ""}
        settings["central_api"] = central_api_cfg
        _sync_central_runtime_config()
    else:
        raise HTTPException(status_code=404, detail=f"Unknown settings provider: {provider}")

    _save_settings()

    if changed_branch:
        if "sync_repo" in background_tasks:
            background_tasks["sync_repo"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_tasks["sync_repo"]
        background_tasks["sync_repo"] = asyncio.create_task(sync_repo())

    payload = await api_settings_get()
    await broadcast({"type": "settings_update", "settings": payload})
    if relay_config_changed and relay_payload is not None:
        await _broadcast_relay_state()

    response: dict[str, Any] = {"status": "ok", "provider": provider_key, "settings": payload}
    if relay_payload is not None:
        response["relay_status"] = relay_payload
    return response
