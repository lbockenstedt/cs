"""Tenant settings management endpoints."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# asyncio saves spokes in parallel threads.
import asyncio
# asdict turns the ACME config dataclass into a dict.
from dataclasses import asdict
# Path for the data directory used by ACME.
from pathlib import Path
# Type hints for request models and helpers.
from typing import Any, Literal, Optional, Union

# FastAPI router, dependency injection and HTTP errors.
from fastapi import APIRouter, Depends, HTTPException
# Pydantic request models.
from pydantic import BaseModel, Field

# ACME (Let's Encrypt) certificate management.
from .. import acme as acme_manager
# auth checks permissions; store loads and saves tenants and spokes.
from .. import auth, store
# App settings (data directory).
from ..config import get_settings
# Encrypt/decrypt stored secret settings.
from ..crypto import decrypt_dict, encrypt_dict
# Data models used here.
from ..data_models import Spoke, ProcessingMode, Tenant, User

# Router mounted by app.main.
router = APIRouter()


# Where a feature runs: on the hub (centralized) or on each spoke (distributed).
ModeValue = Literal["centralized", "distributed"]
# Features whose processing mode can be set per spoke.
PROCESSING_FEATURES = ["aruba_polling", "teams_webhook", "email", "heartbeat", "gkill", "schedules", "repo_sync"]


class ArubaSettingsRequest(BaseModel):
    """Aruba Central API credentials and browse interval for a tenant."""
    cluster_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    customer_id: str = ""
    workspace_id: str = ""
    api_version: str = "classic"
    central_browse_interval_minutes: int = 5


class NotificationSettingsRequest(BaseModel):
    """Teams webhook and SMTP e-mail notification settings."""
    # to_emails accepts a list or a comma-separated string.
    teams_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    to_emails: Union[list[str], str] = Field(default_factory=list)
    from_email: str = "hub@localhost"
    enabled: bool = False


class GithubSettingsRequest(BaseModel):
    """GitHub token and simulation repo used by spokes."""
    github_token: str = ""
    sim_repo_url: str = ""
    sim_repo_branch: str = "main"


class TenantSettingsPatchRequest(BaseModel):
    """Partial update of tenant settings; None means leave unchanged."""
    use_all_dongles: Optional[bool] = None


class ProcessingModeUpdateRequest(BaseModel):
    """Spoke processing mode: a global mode plus optional per-feature overrides."""
    global_mode: ModeValue = "centralized"
    aruba_polling: Optional[ModeValue] = None
    teams_webhook: Optional[ModeValue] = None
    email: Optional[ModeValue] = None
    heartbeat: Optional[ModeValue] = None
    gkill: Optional[ModeValue] = None
    schedules: Optional[ModeValue] = None
    repo_sync: Optional[ModeValue] = None


class TenantProcessingModesRequest(BaseModel):
    """Tenant-level processing modes; None means leave unchanged."""
    central_api: Optional[ModeValue] = None
    teams: Optional[ModeValue] = None
    email: Optional[ModeValue] = None


def _get_tenant(tenant_id: str) -> Tenant:
    """Return the tenant or raise 404."""
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _get_spoke(tenant_id: str, spoke_id: str) -> Spoke:
    """Return the spoke or raise 404."""
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    return spoke


def _require_tenant_admin(tenant_id: str, user: User) -> User:
    """Require tenant access with the admin role; superadmins always pass."""
    # Must have access to the tenant at all.
    auth.require_tenant_access(tenant_id, user)
    # Superadmins bypass the role check.
    if user.is_superadmin:
        return user
    # Everyone else needs the admin role in this tenant.
    if user.get_role(tenant_id) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def _require_any_admin(user: User) -> User:
    """Require admin in at least one tenant, or superadmin (403 otherwise)."""
    # Allowed if superadmin or admin of any tenant.
    if user.is_superadmin or any(role.get("role") == "admin" for role in user.tenant_roles):
        return user
    # Otherwise forbidden.
    raise HTTPException(status_code=403, detail="Admin role required")


# DNS provider credential keys that are secret and must be masked in responses.
_SECRET_DNS_CREDENTIAL_KEYS = {
    "cf_api_token",
    "cf_api_key",
    "he_ddns_key",
    "godaddy_api_key",
    "godaddy_api_secret",
    "do_token",
    "porkbun_api_key",
    "porkbun_secret_key",
    "gcloud_service_account_json",
    "dnsimple_token",
    "azure_client_secret",
    "route53_secret_key",
    "namecheap_api_key",
}


def _masked_dns_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    """Return DNS credentials with secret values replaced by "***" (non-secrets shown as-is)."""
    # Build the masked copy key by key; empty secrets stay empty to show "not set".
    data: dict[str, str] = {}
    for key, value in (credentials or {}).items():
        text = "" if value is None else str(value)
        data[key] = "***" if key in _SECRET_DNS_CREDENTIAL_KEYS and text else text
    return data


def _configured_dns_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    """Return each DNS credential key as "***" if it has a value, else "" (set/not-set map)."""
    return {key: ("***" if value else "") for key, value in (credentials or {}).items()}


def _merge_dns_credentials(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge incoming DNS credentials into existing ones without wiping stored secrets.

    Blank or "***" (masked) values mean "keep the existing value".
    """
    # Start from a copy of the existing credentials.
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        # Skip unchanged/masked values; overwrite the rest.
        if value in (None, "", "***"):
            continue
        merged[key] = value
    # Return the merged credentials.
    return merged


def _serialize_processing_summary(tenant: Tenant) -> dict[str, Any]:
    """Summarise the tenant default and each spoke's configured and effective processing modes."""
    # One entry per spoke.
    spokes = []
    for spoke in store.list_spokes(tenant.id):
        # Per-feature overrides as set, and the mode each feature actually resolves to.
        feature_overrides = {feature: getattr(spoke.processing_mode, feature) for feature in PROCESSING_FEATURES}
        effective_modes = {feature: spoke.processing_mode.resolve(feature) for feature in PROCESSING_FEATURES}
        # Record this spoke's summary.
        spokes.append(
            {
                "spoke_id": spoke.id,
                "hostname": spoke.hostname,
                "global_mode": spoke.processing_mode.global_mode,
                "feature_overrides": feature_overrides,
                "effective_modes": effective_modes,
            }
        )
    # Tenant id, tenant default mode and the per-spoke list.
    return {
        "tenant_id": tenant.id,
        "default_mode": tenant.default_processing_mode.global_mode,
        "spokes": spokes,
    }


def _normalize_notification_config(payload: NotificationSettingsRequest) -> dict[str, Any]:
    """Convert notification settings into the stored config dict."""
    # Accept a comma-separated string and turn it into a clean list.
    to_emails = payload.to_emails
    if isinstance(to_emails, str):
        to_emails = [item.strip() for item in to_emails.split(",") if item.strip()]
    # Store the webhook under both key names for compatibility with older readers.
    return {
        "enabled": payload.enabled,
        "teams_webhook": payload.teams_webhook_url,
        "teams_webhook_url": payload.teams_webhook_url,
        "smtp_host": payload.smtp_host,
        "smtp_port": payload.smtp_port,
        "smtp_user": payload.smtp_user,
        "smtp_pass": payload.smtp_pass,
        "to_emails": to_emails,
        "from_email": payload.from_email,
    }


def _serialize_aruba_config(tenant: Tenant) -> dict[str, Any]:
    """Return the tenant's Aruba config for the UI with secrets reduced to set/not-set flags."""
    # Not configured: report that plus the browse interval.
    if not tenant.aruba_config_enc:
        return {"configured": False, "central_browse_interval_minutes": tenant.central_browse_interval_minutes}
    # Decrypt the stored config; report unreadable configs instead of failing.
    try:
        cfg = decrypt_dict(tenant.aruba_config_enc)
    except Exception:
        return {"configured": True, "error": "unreadable", "central_browse_interval_minutes": tenant.central_browse_interval_minutes}
    # Non-secret fields as-is; secrets only as booleans.
    return {
        "configured": True,
        "cluster_url": cfg.get("cluster_url", ""),
        "client_id": cfg.get("client_id", ""),
        "customer_id": cfg.get("customer_id", ""),
        "workspace_id": cfg.get("workspace_id", ""),
        "api_version": cfg.get("api_version", "classic"),
        "client_secret_configured": bool(cfg.get("client_secret")),
        "access_token_configured": bool(cfg.get("access_token")),
        "refresh_token_configured": bool(cfg.get("refresh_token")),
        "webhook_registered": bool(cfg.get("webhook_id")),
        "central_browse_interval_minutes": tenant.central_browse_interval_minutes,
    }


def _serialize_notification_config(tenant: Tenant) -> dict[str, Any]:
    """Return the tenant's notification config for the UI with secrets as set/not-set flags."""
    # Not configured.
    if not tenant.notification_config_enc:
        return {"configured": False}
    # Decrypt the stored config; report unreadable configs instead of failing.
    try:
        cfg = decrypt_dict(tenant.notification_config_enc)
    except Exception:
        return {"configured": True, "error": "unreadable"}
    # Accept either webhook key name.
    teams_webhook = cfg.get("teams_webhook") or cfg.get("teams_webhook_url") or ""
    # Accept a list or a comma-separated string of recipients.
    to_emails = cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [item.strip() for item in to_emails.split(",") if item.strip()]
    # Non-secret fields as-is; webhook and password only as booleans.
    return {
        "configured": True,
        "enabled": bool(cfg.get("enabled")),
        "teams_webhook_configured": bool(teams_webhook),
        "smtp_host": cfg.get("smtp_host", ""),
        "smtp_port": cfg.get("smtp_port", 587),
        "smtp_user": cfg.get("smtp_user", ""),
        "smtp_pass_configured": bool(cfg.get("smtp_pass") or cfg.get("smtp_password")),
        "to_emails": to_emails,
        "from_email": cfg.get("from_email", "hub@localhost"),
    }


def _serialize_github_config(tenant: Tenant) -> dict[str, Any]:
    """Return the tenant's GitHub config for the UI with the token as a set/not-set flag."""
    # Not configured: defaults.
    if not tenant.github_config_enc:
        return {
            "configured": False,
            "sim_repo_url": "",
            "sim_repo_branch": "main",
            "github_token_configured": False,
        }
    # Decrypt the stored config; report unreadable configs with defaults.
    try:
        cfg = decrypt_dict(tenant.github_config_enc)
    except Exception:
        return {
            "configured": True,
            "error": "unreadable",
            "sim_repo_url": "",
            "sim_repo_branch": "main",
            "github_token_configured": False,
        }
    # Repo URL and branch (default main), token only as a boolean.
    return {
        "configured": True,
        "sim_repo_url": cfg.get("sim_repo_url", ""),
        "sim_repo_branch": cfg.get("sim_repo_branch", "main") or "main",
        "github_token_configured": bool(cfg.get("github_token")),
    }


def _processing_mode_from_payload(payload: ProcessingModeUpdateRequest) -> ProcessingMode:
    """Build a ProcessingMode model from the request payload."""
    return ProcessingMode(**payload.model_dump())


async def _save_config_updates_to_approved_spokes(tenant_id: str, spokes: list[Spoke]) -> None:
    """Save the given spokes in parallel, then queue a config_update for each."""
    # save_spoke() is synchronous and rewrites spoke state on disk, so persist all
    # version bumps before enqueuing config_update commands that read them back.
    await asyncio.gather(*[asyncio.to_thread(store.save_spoke, spoke) for spoke in spokes])
    # Queue (or refresh) a config_update command for each spoke.
    for spoke in spokes:
        store.ensure_config_update_command(tenant_id, spoke.id)


def _push_config_updates_to_approved_spokes(tenant_id: str) -> int:
    """Bump the config version of every approved spoke and push the new config to them.

    Returns:
        Number of spokes updated.
    """
    # Bump the config version of each approved spoke.
    spokes = []
    for spoke in store.list_spokes(tenant_id):
        if spoke.status != "approved":
            continue
        spoke.config_version += 1
        spokes.append(spoke)

    # Nothing to push.
    if not spokes:
        return 0

    # Save and queue the updates (runs the async helper from this sync code).
    asyncio.run(_save_config_updates_to_approved_spokes(tenant_id, spokes))
    # Report how many spokes were updated.
    return len(spokes)


@router.get("/acme/status")
def get_acme_status(current_user: User = Depends(auth.get_current_user)):
    """Return the ACME certificate status."""
    # Hub-wide setting: any admin of any tenant (or a superadmin) may use it.
    _require_any_admin(current_user)
    # Current ACME status.
    return acme_manager.get_acme_status()


@router.get("/{tenant_id}/settings")
def get_tenant_settings(tenant_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return all settings for a tenant with secrets masked."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Basic tenant info plus each settings section.
    return {
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "aruba_cid": tenant.aruba_cid,
            "created_at": tenant.created_at,
            "created_by": tenant.created_by,
        },
        "aruba": _serialize_aruba_config(tenant),
        "notifications": _serialize_notification_config(tenant),
        "github": _serialize_github_config(tenant),
        "use_all_dongles": bool((tenant.hub_config or {}).get("use_all_dongles", False)),
        "processing_mode": tenant.default_processing_mode.model_dump(),
        "processing_modes": tenant.processing_modes,
    }


@router.patch("/{tenant_id}/settings")
def patch_tenant_settings(
    tenant_id: str,
    payload: TenantSettingsPatchRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Update simple tenant settings and push changes to approved spokes."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Work on a copy of hub_config so we can tell whether anything changed.
    next_hub_config = dict(tenant.hub_config or {})
    changed = False

    # Apply use_all_dongles if provided and note whether it changed.
    if payload.use_all_dongles is not None:
        next_hub_config["use_all_dongles"] = bool(payload.use_all_dongles)
        changed = changed or next_hub_config != (tenant.hub_config or {})

    # Save only if something changed.
    if changed:
        tenant.hub_config = next_hub_config
        # Persist the tenant.
        store.save_tenant(tenant)

    # Push to spokes only when the config changed.
    pushed_count = 0
    if changed:
        # Push the new config to approved spokes and remember how many were updated.
        pushed_count = _push_config_updates_to_approved_spokes(tenant_id)

    # Return the current value and how many spokes were updated.
    return {
        "use_all_dongles": bool((tenant.hub_config or {}).get("use_all_dongles", False)),
        "pushed_to_spokes": pushed_count,
    }


@router.post("/{tenant_id}/settings/aruba")
def update_aruba_settings(
    tenant_id: str,
    payload: ArubaSettingsRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save the tenant's Aruba Central settings and push them to approved spokes."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Read the existing config so webhook registration details are kept.
    existing_cfg: dict[str, Any] = {}
    if tenant.aruba_config_enc:
        try:
            existing_cfg = decrypt_dict(tenant.aruba_config_enc)
        except Exception:
            existing_cfg = {}
    # New config from the request (browse interval is stored separately).
    cfg = payload.model_dump(exclude={"central_browse_interval_minutes"})
    # Keep the existing webhook id/key; they are not part of the form.
    for key in ("webhook_id", "webhook_api_key"):
        if existing_cfg.get(key):
            cfg[key] = existing_cfg[key]
    # Encrypt and store; update the customer id if given.
    tenant.aruba_config_enc = encrypt_dict(cfg)
    tenant.aruba_cid = payload.customer_id or tenant.aruba_cid
    # Clamp the browse interval to 1-60 minutes.
    tenant.central_browse_interval_minutes = max(1, min(60, payload.central_browse_interval_minutes))
    # Persist the tenant.
    store.save_tenant(tenant)
    # Push the new config to the tenant's approved spokes.
    _push_config_updates_to_approved_spokes(tenant_id)
    # Return the masked view.
    return _serialize_aruba_config(tenant)


@router.post("/{tenant_id}/settings/notifications")
def update_notification_settings(
    tenant_id: str,
    payload: NotificationSettingsRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save the tenant's notification settings and push them to approved spokes."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Encrypt and store the normalised config.
    tenant.notification_config_enc = encrypt_dict(_normalize_notification_config(payload))
    # Persist the tenant.
    store.save_tenant(tenant)
    # Push the new config to the tenant's approved spokes.
    _push_config_updates_to_approved_spokes(tenant_id)
    # Return the masked view.
    return _serialize_notification_config(tenant)


@router.post("/{tenant_id}/settings/github")
def update_github_settings(
    tenant_id: str,
    payload: GithubSettingsRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save the tenant's GitHub settings and push them to approved spokes.

    Blank fields keep the stored values, so the token need not be re-entered.
    """
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Read the existing config so blank fields can fall back to it.
    existing: dict[str, Any] = {}
    if tenant.github_config_enc:
        try:
            existing = decrypt_dict(tenant.github_config_enc)
        except Exception:
            existing = {}

    # Repo URL and branch, falling back to stored values (branch defaults to main).
    cfg = {
        "sim_repo_url": str(payload.sim_repo_url or existing.get("sim_repo_url") or "").strip(),
        "sim_repo_branch": str(payload.sim_repo_branch or existing.get("sim_repo_branch") or "main").strip() or "main",
    }
    # Use a new token if given, otherwise keep the stored one.
    github_token = str(payload.github_token or "")
    if github_token:
        cfg["github_token"] = github_token
    elif existing.get("github_token"):
        cfg["github_token"] = existing["github_token"]

    # Store only if something is set; otherwise keep the current value.
    tenant.github_config_enc = encrypt_dict(cfg) if any(str(value).strip() for value in cfg.values()) else tenant.github_config_enc
    # Persist the tenant.
    store.save_tenant(tenant)
    # Push the new config to approved spokes and remember how many were updated.
    pushed_count = _push_config_updates_to_approved_spokes(tenant_id)
    # Return the masked view and push count.
    return {**_serialize_github_config(tenant), "pushed_to_spokes": pushed_count}


@router.delete("/{tenant_id}/settings/aruba")
def clear_aruba_settings(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Intentionally clear the Central API (Aruba) configuration."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Remove the stored config.
    tenant.aruba_config_enc = None
    # Persist the tenant.
    store.save_tenant(tenant)
    # Confirm the setting was cleared.
    return {"cleared": True}


@router.delete("/{tenant_id}/settings/github")
def clear_github_settings(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Intentionally clear the GitHub configuration."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Remove the stored config.
    tenant.github_config_enc = None
    # Persist the tenant.
    store.save_tenant(tenant)
    # Confirm the setting was cleared.
    return {"cleared": True}


@router.delete("/{tenant_id}/settings/notifications")
def clear_notification_settings(
    tenant_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Intentionally clear the notification configuration."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Remove the stored config.
    tenant.notification_config_enc = None
    # Persist the tenant.
    store.save_tenant(tenant)
    # Confirm the setting was cleared.
    return {"cleared": True}


@router.get("/{tenant_id}/settings/processing-mode")
def get_default_processing_mode(tenant_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return the tenant's default processing mode."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Return it as a plain dict.
    return tenant.default_processing_mode.model_dump()


@router.post("/{tenant_id}/settings/processing-mode")
def update_default_processing_mode(
    tenant_id: str,
    payload: ProcessingModeUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Set the tenant's default processing mode."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Replace the default mode with the payload.
    tenant.default_processing_mode = _processing_mode_from_payload(payload)
    # Persist the tenant.
    store.save_tenant(tenant)
    # Return the new mode.
    return tenant.default_processing_mode.model_dump()


@router.patch("/hub/tenants/{tenant_id}/processing-modes")
def update_tenant_processing_modes(
    tenant_id: str,
    payload: TenantProcessingModesRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Update tenant-level processing modes and push them to approved spokes."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Apply only the provided keys to a copy of the current modes.
    updated = dict(tenant.processing_modes or {})
    for key, value in payload.model_dump(exclude_none=True).items():
        updated[key] = value
    # Store the merged modes.
    tenant.processing_modes = updated
    # Persist the tenant.
    store.save_tenant(tenant)

    # Push the new config to approved spokes and remember how many were updated.
    pushed_count = _push_config_updates_to_approved_spokes(tenant_id)

    # Return the modes and how many spokes were updated.
    return {"processing_modes": tenant.processing_modes, "pushed_to_spokes": pushed_count}


@router.patch("/{tenant_id}/spokes/{spoke_id}/processing-mode")
def update_spoke_processing_mode(
    tenant_id: str,
    spoke_id: str,
    payload: ProcessingModeUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Set one spoke's processing mode."""
    # Only tenant admins (or superadmins) may use this endpoint.
    _require_tenant_admin(tenant_id, current_user)
    # Load the spoke (404 if missing).
    spoke = _get_spoke(tenant_id, spoke_id)
    # Replace its processing mode and save.
    spoke.processing_mode = _processing_mode_from_payload(payload)
    store.save_spoke(spoke)
    # Return the new mode.
    return spoke.processing_mode.model_dump()


@router.get("/{tenant_id}/processing-summary")
def get_processing_summary(tenant_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return the processing-mode summary for a tenant (any tenant member)."""
    # Any tenant member may read it.
    auth.require_tenant_access(tenant_id, current_user)
    # Load the tenant (404 if missing).
    tenant = _get_tenant(tenant_id)
    # Build and return the summary.
    return _serialize_processing_summary(tenant)


@router.get("/settings/acme")
def get_acme_settings(current_user: User = Depends(auth.get_current_user)):
    """Return the ACME settings with secrets masked and current cert info."""
    # Hub-wide setting: any admin of any tenant (or a superadmin) may use it.
    _require_any_admin(current_user)
    # Load the stored ACME config.
    cfg = acme_manager.load_acme_config()
    # Return the config as a dict with secret credentials masked, plus current cert details.
    data = asdict(cfg)
    data["dns_credentials"] = _masked_dns_credentials(cfg.dns_credentials)
    data["dns_credentials_configured"] = _configured_dns_credentials(cfg.dns_credentials)
    data["cert_info"] = acme_manager.get_cert_info()
    return data


@router.post("/settings/acme")
def save_acme_settings(payload: dict[str, Any], current_user: User = Depends(auth.get_current_user)):
    """Save ACME settings; omitted fields keep their stored values.

    Masked or blank DNS credentials keep the stored secrets.
    """
    # Hub-wide setting: any admin of any tenant (or a superadmin) may use it.
    _require_any_admin(current_user)
    # Load the current config to fill in omitted fields.
    existing = acme_manager.load_acme_config()
    # Build the new config; status fields (last renewal, errors, logs) are kept as-is.
    cfg = acme_manager.AcmeConfig(
        enabled=bool(payload.get("enabled", existing.enabled)),
        domain=str(payload.get("domain", existing.domain) or "").strip(),
        email=str(payload.get("email", existing.email) or "").strip(),
        challenge=str(payload.get("challenge", existing.challenge) or existing.challenge),
        ca=str(payload.get("ca", existing.ca) or existing.ca),
        dns_provider=str(payload.get("dns_provider", existing.dns_provider) or "").strip(),
        dns_credentials=_merge_dns_credentials(existing.dns_credentials, payload.get("dns_credentials") or {}),
        last_renewed=existing.last_renewed,
        last_error=existing.last_error,
        cert_expiry=existing.cert_expiry,
        last_log=existing.last_log,
        last_log_at=existing.last_log_at,
    )
    # Save it.
    acme_manager.save_acme_config(cfg)
    # Return the config as a dict with secret credentials masked, plus current cert details.
    data = asdict(cfg)
    data["dns_credentials"] = _masked_dns_credentials(cfg.dns_credentials)
    data["dns_credentials_configured"] = _configured_dns_credentials(cfg.dns_credentials)
    data["cert_info"] = acme_manager.get_cert_info()
    return data


@router.post("/settings/acme/request")
async def request_acme_certificate(current_user: User = Depends(auth.get_current_user)):
    """Request a certificate now using the saved ACME settings."""
    # Hub-wide setting: any admin of any tenant (or a superadmin) may use it.
    _require_any_admin(current_user)
    # Load the config and run the request, storing results in the data directory.
    cfg = acme_manager.load_acme_config()
    return await acme_manager.request_certificate(cfg, Path(get_settings().data_dir))
