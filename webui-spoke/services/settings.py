"""Settings (helpers moved verbatim from server.py; shared deps imported from server)."""
from __future__ import annotations

from server import (
    Any,
    RELAY_INTERVAL_DEFAULT,
    REPO_URL,
    SYNC_INTERVAL,
    _SETTINGS_CACHE_TTL,
    _admin_password,
    _normalize_spoke_auth_provider,
    _public_central_api_settings,
    _public_notification_settings,
    _resolved_template_spec,
    _setting_bool,
    asdict,
    central_token,
    copy,
    settings,
    state,
    time,
)



def _get_cached_settings() -> dict[str, Any]:
    now = time.monotonic()
    if state._settings_cache and (now - state._settings_cache_time) < _SETTINGS_CACHE_TTL:
        return copy.deepcopy(state._settings_cache)

    cfg = dict(settings["central_config"])
    # Strip all secrets — return only non-sensitive fields + presence flags.
    # Runtime token flags are refreshed in api_settings_get() so they never go stale.
    for secret_key in ("client_secret", "access_token", "refresh_token"):
        cfg.pop(secret_key, None)
    cfg["access_token_configured"] = bool(settings["central_config"].get("access_token") or central_token.get("access_token"))
    cfg["refresh_token_configured"] = bool(settings["central_config"].get("refresh_token") or central_token.get("refresh_token"))
    cfg["client_secret_configured"] = bool(settings["central_config"].get("client_secret"))

    state._settings_cache = {
        "repo_url": REPO_URL,
        "repo_branch": settings.get("repo_branch", ""),
        "repo_sync_interval": settings.get("repo_sync_interval", SYNC_INTERVAL),
        "session_timeout_minutes": int(settings.get("session_timeout_minutes", 30)),
        "github_token_configured": bool(settings.get("github_token")),
        "hub_managed": bool(settings.get("hub_managed", False)),
        "hub_isolation_timeout": int(settings.get("hub_isolation_timeout", 3600)),  # Expose the stored isolation timeout so the setup form can render the current safeguard value.
        "central_api": _public_central_api_settings(),
        "central_config": cfg,
        "site_mappings": settings["site_mappings"],
        "spoke_monitored_items": settings.get("spoke_monitored_items", []),
        "monitored_checks": settings["monitored_checks"],
        "hardware_checks": settings.get("hardware_checks", []),
        "usb_vidpids": settings.get("usb_vidpids", "[]"),
        "usb_missing_timeout": settings.get("usb_missing_timeout", "60"),
        "vm_image_1_template_id": settings.get("vm_image_1_template_id", settings.get("usb_linux_template_id", settings.get("usb_template_id", "100"))),
        "vm_image_1_template_spec": settings.get("vm_image_1_template_spec", _resolved_template_spec(settings, 1)),
        "vm_image_2_template_id": settings.get("vm_image_2_template_id", settings.get("usb_windows_template_id", "200")),
        "vm_image_2_template_spec": settings.get("vm_image_2_template_spec", _resolved_template_spec(settings, 2)),
        "vm_image_1_pct": settings.get("vm_image_1_pct", "50"),
        "usb_auto_provision": settings.get("usb_auto_provision", "off"),
        "use_all_dongles": _setting_bool("use_all_dongles", False),
        "usb_max_slots": settings.get("usb_max_slots", "24"),
        "cpu_provision_threshold": settings.get("cpu_provision_threshold", "80"),
        "cpu_delete_threshold": settings.get("cpu_delete_threshold", "90"),
        "mem_provision_threshold": settings.get("mem_provision_threshold", "80"),
        "mem_delete_threshold": settings.get("mem_delete_threshold", "90"),
        "vmid_start": int(settings.get("vmid_start", 0) or 0),
        "usb_ignored_vidpids": settings.get("usb_ignored_vidpids", "[]"),
        "ignored_hostnames": settings.get("ignored_hostnames", '["sim-rpi-0000"]'),
        "vm_silent_timeout": settings.get("vm_silent_timeout", "24"),
        "reclone_schedule_enabled": settings.get("reclone_schedule_enabled", "off"),
        "reclone_schedule_cron": settings.get("reclone_schedule_cron", "sunday 02:00"),
        "reclone_concurrency": settings.get("reclone_concurrency", "1"),
        "protected_vmids": settings.get("protected_vmids", ""),
        "l1_vlan_start": settings.get("l1_vlan_start", "100"),
        "l1_vlan_end": settings.get("l1_vlan_end", "199"),
        "guest_agent_watchdog_enabled": settings.get("guest_agent_watchdog_enabled", "on"),
        "watchdog_reboot_enabled": settings.get("watchdog_reboot_enabled", "on"),
        "guest_agent_grace_minutes": settings.get("guest_agent_grace_minutes", "20"),
        "guest_agent_check_interval_minutes": settings.get("guest_agent_check_interval_minutes", "10"),
        "guest_agent_reboot_after_minutes": settings.get("guest_agent_reboot_after_minutes", "10"),
        "guest_agent_reclone_after_minutes": settings.get("guest_agent_reclone_after_minutes", "30"),
        "notifications": _public_notification_settings(),
        "relay_enabled": settings.get("relay_enabled", "off"),
        "relay_server_url": settings.get("relay_server_url", ""),
        "hub_tls_verify": settings.get("hub_tls_verify", "off"),
        "relay_spoke_name": settings.get("relay_spoke_name", ""),
        "relay_tenant_hint": settings.get("relay_tenant_hint", settings.get("relay_tenant_id", "")),
        "relay_spoke_id": settings.get("relay_spoke_id", ""),
        "relay_tenant_id": settings.get("relay_tenant_id", settings.get("relay_tenant_hint", "")),
        "relay_poll_interval": settings.get("relay_poll_interval", RELAY_INTERVAL_DEFAULT),
        "relay_api_key_configured": bool(settings.get("relay_api_key")),
        "admin_password_configured": bool(_admin_password()),
        "auth_provider": _normalize_spoke_auth_provider(settings.get("auth_provider", "local")),
        "auth_ldap_url": settings.get("auth_ldap_url", ""),
        "auth_ldap_bind_dn": settings.get("auth_ldap_bind_dn", ""),
        "auth_ldap_bind_password_configured": bool(settings.get("auth_ldap_bind_password")),
        "auth_ldap_user_base": settings.get("auth_ldap_user_base", ""),
        "auth_ldap_user_filter": settings.get("auth_ldap_user_filter", "(&(objectClass=user)(sAMAccountName={username}))"),
        "auth_ldap_group_admin": settings.get("auth_ldap_group_admin", ""),
        "auth_ldap_group_viewer": settings.get("auth_ldap_group_viewer", ""),
        "auth_radius_host": settings.get("auth_radius_host", ""),
        "auth_radius_port": int(settings.get("auth_radius_port", 1812)),
        "auth_radius_secret_configured": bool(settings.get("auth_radius_secret")),
        "auth_radius_role_attr": settings.get("auth_radius_role_attr", "Filter-Id"),
        "auth_radius_admin_val": settings.get("auth_radius_admin_val", "admin"),
        "auth_tacacs_host": settings.get("auth_tacacs_host", ""),
        "auth_tacacs_port": int(settings.get("auth_tacacs_port", 49)),
        "auth_tacacs_secret_configured": bool(settings.get("auth_tacacs_secret")),
        "auth_tacacs_admin_priv": int(settings.get("auth_tacacs_admin_priv", 15)),
        "spoke_tls": settings.get("spoke_tls", "off"),
        "proxmox_api_token_configured": bool(settings.get("proxmox_api_token", "").strip()),
        "proxmox_tokens_configured": {
            hn: bool(str(tok or "").strip())
            for hn, tok in (settings.get("proxmox_tokens") or {}).items()
        },
        "proxmox_config": copy.deepcopy(settings.get("proxmox_config") or {}),
    }
    state._settings_cache_time = now
    return copy.deepcopy(state._settings_cache)





def _public_settings() -> dict[str, Any]:
    payload = _get_cached_settings()
    payload["central_config"]["access_token_configured"] = bool(
        settings["central_config"].get("access_token") or central_token.get("access_token")
    )
    payload["central_config"]["refresh_token_configured"] = bool(
        settings["central_config"].get("refresh_token") or central_token.get("refresh_token")
    )
    payload["admin_password_configured"] = bool(_admin_password())
    payload["proxmox_config"] = copy.deepcopy(settings.get("proxmox_config") or {})
    return payload





def _public_acme_settings(cfg: Any) -> dict[str, Any]:
    data = asdict(cfg)
    credentials = dict(cfg.dns_credentials or {})
    data["dns_credentials"] = {key: "" for key in credentials}
    data["dns_credentials_configured"] = {key: bool(value) for key, value in credentials.items()}
    data["cf_api_token_set"] = bool(credentials.get("cf_api_token"))
    data["he_ddns_key_set"] = bool(credentials.get("he_ddns_key"))
    return data
