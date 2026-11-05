"""Workspace API endpoints; a workspace currently maps one-to-one to a tenant."""
# FastAPI tools: router, dependency injection, HTTP errors and status-code constants.
from fastapi import APIRouter, Depends, HTTPException, status

# auth enforces login/tenant access; store reads tenant records from persistent storage.
from .. import auth, store
# User model type used for the authenticated-user dependency.
from ..data_models import User

# Router mounted by app.main for the workspace endpoints.
router = APIRouter()


@router.get("/workspaces")
def list_workspaces():
    """List all workspaces; returns an empty list because global workspaces are not implemented."""
    # Nothing to list yet, so return an empty list.
    return []


@router.post("/workspaces", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def create_workspace():
    """Create a workspace; placeholder that replies 501 Not Implemented."""
    # Body explains to the caller why the request did not succeed.
    return {"detail": "Not implemented yet"}


@router.get("/{tenant_id}/workspaces")
def get_tenant_workspaces(tenant_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return the workspace(s) for a tenant, which is just the tenant itself presented as one item.
    """
    # Stop here with 403 if the logged-in user may not see this tenant.
    auth.require_tenant_access(tenant_id, current_user)
    # Look up the tenant record; a missing tenant becomes a 404 for the caller.
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    # Wrap the tenant in the list shape the frontend expects for workspaces.
    return {
        "items": [
            {
                "id": tenant.id,
                "name": tenant.name,
                "tenant_id": tenant.id,
                "aruba_cid": tenant.aruba_cid,
                "created_at": tenant.created_at,
            }
        ]
    }
