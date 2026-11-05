"""Tenant-scoped spoke management endpoints."""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# Any is used for loosely structured dict values.
from typing import Any

# FastAPI router, dependency injection and HTTP errors.
from fastapi import APIRouter, Depends, HTTPException
# Request-body validation models.
from pydantic import BaseModel, Field

# auth guards access; store persists spokes; Spoke/User are the data models.
from .. import auth, store
from ..data_models import Spoke, User

# Router mounted by app.main under /api.
router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    """Body for replacing a spoke's desired configuration."""
    config: dict[str, Any] = Field(default_factory=dict)


class LabelUpdateRequest(BaseModel):
    """Body for renaming a spoke's display label."""
    label: str


class AssignedSiteUpdateRequest(BaseModel):
    """Body for setting the Central site names a spoke is assigned to."""
    assigned_sites: list[str] = Field(default_factory=list)


def _serialize_spoke(spoke: Spoke, include_config: bool = True) -> dict[str, Any]:
    """Return a spoke as a JSON-friendly dict; ``include_config`` controls whether config is
    included.
    """
    # Fields always shown in the UI.
    data = {
        "id": spoke.id,
        "tenant_id": spoke.tenant_id,
        "hostname": spoke.hostname,
        "label": spoke.label,
        "spoke_name": spoke.spoke_name,
        "assigned_sites": spoke.assigned_sites,
        "status": spoke.status,
        "seed_config": spoke.seed_config,
        "processing_mode": spoke.processing_mode,
        "config_version": spoke.config_version,
        "applied_config_version": spoke.applied_config_version,
        "last_config_applied_at": spoke.last_config_applied_at,
        "last_seen": spoke.last_seen,
        "telemetry": spoke.telemetry,
        "created_at": spoke.created_at,
    }
    # The full config can be large, so callers may leave it out.
    if include_config:
        data["config"] = spoke.config
    return data


def _get_spoke(tenant_id: str, spoke_id: str) -> Spoke:
    """Return the spoke or raise 404 if it does not exist in this tenant."""
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    return spoke


def _require_tenant_admin(tenant_id: str, user: User) -> User:
    """Ensure the user may administer the tenant (superadmin or tenant "admin" role), else 403."""
    # First make sure the user can see the tenant at all.
    auth.require_tenant_access(tenant_id, user)
    # Superadmins always qualify.
    if user.is_superadmin:
        return user
    # Members with any role other than admin (e.g. viewer) are refused.
    if user.get_role(tenant_id) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


@router.get("/sites")
def list_sites_legacy():
    """Legacy endpoint: list every approved spoke across all tenants as "sites".

    Kept for older frontends; workspace_id carries the tenant id.
    """
    # Collect approved spokes from every tenant.
    results = []
    for tenant in store.list_tenants():
        for spoke in store.list_spokes(tenant.id):
            # Pending/revoked spokes are not shown.
            if spoke.status != "approved":
                continue
            # Legacy shape expected by older UI code.
            results.append(
                {
                    "id": spoke.id,
                    "hostname": spoke.hostname,
                    "label": spoke.label,
                    "spoke_name": spoke.spoke_name,
                    "status": spoke.status,
                    "last_seen": spoke.last_seen,
                    "telemetry": spoke.telemetry,
                    "workspace_id": spoke.tenant_id,
                }
            )
    return results


@router.get("/{tenant_id}/spokes")
def list_tenant_spokes(tenant_id: str, current_user: User = Depends(auth.get_current_user)):
    """List all spokes in a tenant the caller can access."""
    auth.require_tenant_access(tenant_id, current_user)
    return [_serialize_spoke(spoke) for spoke in store.list_spokes(tenant_id)]


@router.get("/{tenant_id}/spokes/{spoke_id}")
def get_spoke_detail(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return one spoke's details, including its config."""
    auth.require_tenant_access(tenant_id, current_user)
    return _serialize_spoke(_get_spoke(tenant_id, spoke_id))


@router.post("/{tenant_id}/spokes/{spoke_id}/revoke")
def revoke_spoke(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """Revoke a spoke so it can no longer talk to the hub. Admins only."""
    # Check admin rights and that the spoke exists before revoking.
    _require_tenant_admin(tenant_id, current_user)
    _get_spoke(tenant_id, spoke_id)
    store.revoke_spoke(tenant_id, spoke_id)
    return {"status": "revoked"}


@router.delete("/{tenant_id}/spokes/{spoke_id}")
def delete_spoke(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """Permanently delete a spoke record. Admins only."""
    # Check admin rights and that the spoke exists before deleting.
    _require_tenant_admin(tenant_id, current_user)
    _get_spoke(tenant_id, spoke_id)
    store.delete_spoke(tenant_id, spoke_id)
    return {"status": "deleted"}


@router.patch("/{tenant_id}/spokes/{spoke_id}/config")
def update_spoke_config(
    tenant_id: str,
    spoke_id: str,
    payload: ConfigUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Replace a spoke's desired config and bump its version so the spoke pulls the change. Admins
    only.
    """
    # Admin check and spoke lookup.
    _require_tenant_admin(tenant_id, current_user)
    spoke = _get_spoke(tenant_id, spoke_id)
    # Unapproved spokes cannot receive configuration.
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")

    # Store the new config and increase the version the spoke compares against.
    spoke.config = payload.config
    spoke.config_version += 1
    store.save_spoke(spoke)
    return _serialize_spoke(spoke)


@router.patch("/{tenant_id}/spokes/{spoke_id}/label")
def update_spoke_label(
    tenant_id: str,
    spoke_id: str,
    payload: LabelUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Change a spoke's display label. Admins only."""
    # Admin check and spoke lookup, then save the new label.
    _require_tenant_admin(tenant_id, current_user)
    spoke = _get_spoke(tenant_id, spoke_id)
    spoke.label = payload.label
    store.save_spoke(spoke)
    return _serialize_spoke(spoke)


@router.patch("/tenant/{tenant_id}/spokes/{spoke_id}/assigned-site")
def update_spoke_assigned_site(
    tenant_id: str,
    spoke_id: str,
    payload: AssignedSiteUpdateRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Set the Central sites a spoke serves. Admins only."""
    # Admin check and spoke lookup.
    _require_tenant_admin(tenant_id, current_user)
    spoke = _get_spoke(tenant_id, spoke_id)
    # Trim names and drop blanks.
    spoke.assigned_sites = [s.strip() for s in (payload.assigned_sites or []) if s.strip()]
    # Keep the legacy single-site field in sync (first site, or empty).
    spoke.assigned_site = spoke.assigned_sites[0] if spoke.assigned_sites else ""
    store.save_spoke(spoke)
    return _serialize_spoke(spoke)
