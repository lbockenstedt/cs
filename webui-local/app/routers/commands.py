"""Command-queue endpoints: queue work for spokes, list/clear the queue and read audit history.

Spokes poll the hub for queued commands; every queued or cleared command is also written
to the tenant's audit log.
"""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# Time helpers for command expiry; typing hints for loose payloads.
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# FastAPI router, dependency injection and HTTP errors.
from fastapi import APIRouter, Depends, HTTPException
# Request-body validation models.
from pydantic import BaseModel, Field

# auth guards access; store persists commands/audit; models describe them.
from .. import auth, store
from ..data_models import AuditEntry, Command, User

# Router mounted by app.main under /api.
router = APIRouter()


class CreateCommandRequest(BaseModel):
    """Body for POST /commands: which spoke, what command type, target component and arguments."""
    tenant_id: str
    spoke_id: str
    type: str
    target: str = "spoke"
    payload: dict[str, Any] = Field(default_factory=dict)


def _serialize_command(command: Command) -> dict[str, Any]:
    """Return a command as JSON-friendly data using its public field names (spoke_id, not
    island_id).
    """
    return command.model_dump(mode="json", by_alias=True)


def _get_approved_spoke(tenant_id: str, spoke_id: str):
    """Return the spoke, or raise 404 if missing and 409 if it has not been approved yet."""
    # Load the spoke within the tenant.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    # Unapproved spokes are not trusted to receive commands.
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")
    return spoke


def _queue_command(
    tenant_id: str,
    spoke_id: str,
    command_type: str,
    payload: dict[str, Any],
    current_user: User,
    execution_mode: str = "centralized",
) -> Command:
    """Create a command for a spoke, add an audit entry for it, and return the command.

    Commands expire after 24 hours so an offline spoke will not run stale work much later.
    """
    # Build the queued command with a 24-hour expiry.
    command = Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type=command_type,
        payload=payload,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    # Put it in the queue the spoke polls.
    store.enqueue_command(command)
    # Record who queued what, and how it will be executed, in the audit log.
    store.append_audit(
        AuditEntry(
            spoke_id=spoke_id,
            tenant_id=tenant_id,
            task_type=command_type,
            execution_mode=execution_mode,
            status="pending",
            detail=f"Queued command {command_type}",
            initiated_by=current_user.username,
            result={"payload": payload, "target": command.target},
        )
    )
    return command


@router.post("/commands")
def create_command(req: CreateCommandRequest, current_user: User = Depends(auth.get_current_user)):
    """Queue an arbitrary command for an approved spoke in a tenant the caller can access."""
    # Caller must belong to the tenant, and the spoke must exist and be approved.
    auth.require_tenant_access(req.tenant_id, current_user)
    spoke = _get_approved_spoke(req.tenant_id, req.spoke_id)
    # Build the command with a 24-hour expiry.
    command = Command(
        spoke_id=req.spoke_id,
        tenant_id=req.tenant_id,
        type=req.type,
        target=req.target,
        payload=req.payload,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    # Add it to the queue.
    store.enqueue_command(command)
    # Audit it; execution mode comes from the spoke's per-feature setting when one exists.
    store.append_audit(
        AuditEntry(
            spoke_id=req.spoke_id,
            tenant_id=req.tenant_id,
            task_type=req.type,
            execution_mode=spoke.processing_mode.resolve(req.type) if hasattr(spoke.processing_mode, req.type) else "centralized",
            status="pending",
            detail=f"Queued command {req.type}",
            initiated_by=current_user.username,
            result={"payload": req.payload, "target": req.target},
        )
    )
    # Return the new command id and its initial status ("queued").
    return {"id": command.id, "status": command.status}


@router.post("/{tenant_id}/spokes/{spoke_id}/repo-sync")
def repo_sync_spoke(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """Ask a spoke to pull the latest configuration repository from GitHub."""
    # Tenant access check, then ensure the spoke is approved.
    auth.require_tenant_access(tenant_id, current_user)
    spoke = _get_approved_spoke(tenant_id, spoke_id)
    # Queue a repo_sync command, recording whether it runs centrally or on the spoke.
    command = _queue_command(
        tenant_id,
        spoke_id,
        "repo_sync",
        {},
        current_user,
        execution_mode=spoke.processing_mode.resolve("repo_sync"),
    )
    return {"id": command.id, "status": command.status}


@router.get("/{tenant_id}/commands")
def list_commands(
    tenant_id: str,
    spoke_id: Optional[str] = None,
    current_user: User = Depends(auth.get_current_user),
):
    """List queued/past commands for a tenant, optionally only for one spoke."""
    # Caller must have access to the tenant.
    auth.require_tenant_access(tenant_id, current_user)
    # A filter for an unknown spoke is a 404 rather than an empty list.
    if spoke_id and not store.get_spoke(tenant_id, spoke_id):
        raise HTTPException(status_code=404, detail="Spoke not found")
    # Serialise each command for the JSON response.
    return [_serialize_command(command) for command in store.list_commands(tenant_id, spoke_id)]


@router.delete("/{tenant_id}/commands")
def clear_commands(
    tenant_id: str,
    spoke_id: Optional[str] = None,
    current_user: User = Depends(auth.get_current_user),
):
    """Delete pending commands for a tenant (or one spoke) and audit the action. Admins only."""
    auth.require_tenant_access(tenant_id, current_user)
    # Clearing the queue is destructive — require admin or superadmin role
    role = current_user.get_role(tenant_id) if not current_user.is_superadmin else "superadmin"
    # Viewers are refused.
    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin role required to clear command queue")
    # A filter for an unknown spoke is a 404.
    if spoke_id and not store.get_spoke(tenant_id, spoke_id):
        raise HTTPException(status_code=404, detail="Spoke not found")
    # Remove the queued commands and get the number removed.
    cleared = store.clear_command_queue(tenant_id, spoke_id)
    # Record the clear-out in the audit log.
    store.append_audit(
        AuditEntry(
            spoke_id=spoke_id or "all",
            tenant_id=tenant_id,
            task_type="clear_command_queue",
            execution_mode="hub",
            status="executed",
            detail=f"Cleared {cleared} command(s) from queue",
            initiated_by=current_user.username,
            result={"cleared": cleared, "spoke_id": spoke_id or "all"},
        )
    )
    return {"ok": True, "cleared": cleared}


class ProxmoxCommandRequest(BaseModel):
    """Body for proxmox-command: the action name, its arguments and the target agent."""
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    target: str = "proxmox"


@router.post("/{tenant_id}/spokes/{spoke_id}/proxmox-command")
def proxmox_command(
    tenant_id: str,
    spoke_id: str,
    req: ProxmoxCommandRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a Proxmox action (e.g. start/stop a VM) to be relayed by the spoke's Proxmox agent.

    Restricted to explicit tenant members because it controls real infrastructure.
    """
    # Remote control needs explicit tenant membership (superadmins allowed but logged).
    auth.require_tenant_member(tenant_id, current_user)
    # The spoke must exist and be approved.
    _get_approved_spoke(tenant_id, spoke_id)
    # Wrap the action and arguments in a "proxmox" command with a 24-hour expiry.
    command = Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type="proxmox",
        target=req.target,
        payload={"action": req.action, "args": req.args},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    # Queue it and record it in the audit log as a relayed action.
    store.enqueue_command(command)
    store.append_audit(
        AuditEntry(
            spoke_id=spoke_id,
            tenant_id=tenant_id,
            task_type="proxmox",
            execution_mode="relay",
            status="pending",
            detail=f"Proxmox command: {req.action}",
            initiated_by=current_user.username,
            result={"action": req.action, "args": req.args, "target": req.target},
        )
    )
    return {"id": command.id, "status": command.status, "action": req.action}


@router.get("/{tenant_id}/spokes/{spoke_id}/audit")
def get_spoke_audit(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return the audit history for one spoke."""
    # Caller must have access to the tenant, and the spoke must exist.
    auth.require_tenant_access(tenant_id, current_user)
    if not store.get_spoke(tenant_id, spoke_id):
        raise HTTPException(status_code=404, detail="Spoke not found")
    # Serialise each audit entry for the JSON response.
    return [entry.model_dump(mode="json", by_alias=True) for entry in store.get_audit(tenant_id, spoke_id)]
