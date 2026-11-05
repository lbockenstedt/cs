"""VM backup and template reseed endpoints, backed by Azure Blob Storage.

Backups and reseeds are run by the spokes. The hub sends them commands, keeps in-memory
job state, accepts progress reports from the local spoke process and broadcasts progress
to browsers. Failed reseeds are retried with increasing delays.
"""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# asyncio schedules delayed reseed retries.
import asyncio
# Timestamps, command expiry and blob dates.
from datetime import datetime, timedelta, timezone
# Parses the HTTP-date format used in blob Last-Modified values.
from email.utils import parsedate_to_datetime
# os reads the installer API key from the environment.
import os
# Type hints.
from typing import Any, Optional
# uuid4 makes short reseed job ids.
from uuid import uuid4
# defusedxml forbids DTD/entity resolution — the Azure Blob list response is
# third-party XML fetched over HTTPS; a crafted response with an external
# entity could otherwise read local files / SSRF (XXE).
import defusedxml.ElementTree as ET

# httpx calls the Azure Blob REST API.
import httpx
# FastAPI router, dependencies, errors and request access.
from fastapi import APIRouter, Depends, HTTPException, Request
# Pydantic request models.
from pydantic import BaseModel

# auth checks users; crypto handles secrets and SAS tokens; store persists config.
from .. import auth, crypto, store
# Data models used here.
from ..data_models import BackupConfig, Command, User
# Send commands to spokes and broadcast progress to browsers.
from ..ws import send_spoke_command, ws_broadcast

# Router mounted by app.main.
router = APIRouter()
# In-memory backup/reseed jobs by job id (lost on restart).
backup_jobs: dict[str, dict[str, Any]] = {}
# Addresses treated as local for the progress callback.
_LOCALHOSTS = {"127.0.0.1", "::1", "localhost"}
# Seconds to wait before reseed retry 1, 2 and 3.
_RESEED_RETRY_DELAYS = {1: 300, 2: 900, 3: 1800}
# Reseed per-spoke status values meaning success or failure.
_RESEED_SUCCESS_STATES = {"done", "completed"}
_RESEED_ERROR_STATES = {"error", "failed"}


def _get_azure_key(backup_config: BackupConfig) -> str:
    """Decrypt and return the Azure storage key. Raises HTTPException 503 if not available."""
    # Stored encrypted key; missing means not configured.
    enc = (backup_config.azure_key_enc or "").strip()
    if not enc:
        raise HTTPException(status_code=503, detail="Azure storage key is not configured")
    # Decrypt it; failure (e.g. wrong hub key) is reported as 503.
    try:
        return crypto.decrypt_value(enc)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to decrypt Azure key: {exc}") from exc


class TriggerBackupRequest(BaseModel):
    """Request body to start a backup; a blank key means use the stored one."""
    azure_key: str


class SetAzureKeyRequest(BaseModel):
    """Request body to store a new Azure storage key."""
    azure_key: str


class ReseedRequest(BaseModel):
    """Request body to reseed a template from a backup blob onto spokes.

    spoke_ids may be ["all"] for every approved spoke; vm_id is the target VM id.
    """
    tenant_id: str
    template_name: str
    latest_blob: str
    spoke_ids: list[str]
    vm_id: int = 100


class BackupProgressPayload(BaseModel):
    """Progress report for a backup (per VM) or reseed (per spoke) job."""
    job_id: str
    vm_id: Optional[int] = None
    status: str
    pct: int = 0
    size: Optional[int] = None
    file: Optional[str] = None
    error: Optional[str] = None
    spoke_id: Optional[str] = None
    step: Optional[str] = None


def _require_superadmin(current_user: User) -> None:
    """Raise 403 unless the user is a superadmin."""
    if not current_user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin required")


def _get_backup_job(job_id: str) -> dict[str, Any]:
    """Return the in-memory job or raise 404."""
    job = backup_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup job not found")
    return job


def _xml_local_name(tag: str) -> str:
    """Strip the XML namespace ("{ns}Name" -> "Name") from an element tag."""
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _is_local_request(request: Request) -> bool:
    """Return True if the request came from this machine."""
    # Client address, or empty if unknown.
    client = request.client.host if request.client else ""
    return client in _LOCALHOSTS


def _parse_blob_entries(xml_text: str) -> list[dict[str, Any]]:
    """Parse an Azure "List Blobs" XML response into name/size/last_modified dicts."""
    # Parse the XML safely; malformed responses are a 502.
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail=f"Invalid Azure Blob API response: {exc}")

    # Walk every element and pick out the Blob entries.
    blobs: list[dict[str, Any]] = []
    for blob in root.iter():
        if _xml_local_name(blob.tag) != "Blob":
            continue
        # Defaults in case some properties are missing.
        entry: dict[str, Any] = {"name": "", "size": 0, "last_modified": ""}
        # Read the name and the size/date properties from the blob's children.
        for child in blob:
            tag = _xml_local_name(child.tag)
            if tag == "Name":
                entry["name"] = child.text or ""
            elif tag == "Properties":
                for prop in child:
                    prop_tag = _xml_local_name(prop.tag)
                    # Content-Length is the blob size; bad values count as 0.
                    if prop_tag == "Content-Length":
                        try:
                            entry["size"] = int(prop.text or "0")
                        except ValueError:
                            entry["size"] = 0
                    # Last-Modified is an HTTP-date string.
                    elif prop_tag == "Last-Modified":
                        entry["last_modified"] = prop.text or ""
        # Keep only blobs with a name.
        if entry["name"]:
            blobs.append(entry)
    return blobs


def _build_reseed_command_payload(
    backup_config: BackupConfig,
    job_id: str,
    template_name: str,
    blob_url: str,
    vm_id: int,
) -> dict[str, Any]:
    """Build the reseed command payload sent to each spoke (up to 3 retries)."""
    return {
        "cmd_type": "reseed",
        "job_id": job_id,
        "blob_url": blob_url,
        "vm_id": vm_id,
        "template_name": template_name,
        "azure_account": backup_config.azure_account,
        "azure_container": backup_config.azure_container,
        "retry_max": 3,
    }


def _blob_last_modified_key(blob: dict[str, Any]) -> datetime:
    """Return a blob's Last-Modified as a UTC datetime for sorting (oldest if missing/invalid)."""
    # Missing date sorts first.
    value = str(blob.get("last_modified") or "").strip()
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    # Parse the HTTP-date; bad values sort first.
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return datetime.min.replace(tzinfo=timezone.utc)
    # Make sure the result is timezone-aware UTC.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_spoke_command(command_id: str, command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload in the spoke command envelope."""
    return {
        "id": command_id,
        "target": "spoke",
        "type": command_type,
        "payload": payload,
    }


async def _send_or_queue_reseed_command(
    tenant_id: str,
    spoke_id: str,
    job_id: str,
    command_payload: dict[str, Any],
    *,
    retry_count: int = 0,
) -> str:
    """Send a reseed command to the spoke now, or queue it for 24 hours if it is offline.

    Returns:
        "sent" or "queued".
    """
    # Command id includes the retry number so each attempt is distinct.
    command_id = f"{job_id}-{spoke_id}-{retry_count}"
    command = _build_spoke_command(command_id, "reseed", dict(command_payload))
    # Try to send live first.
    if await send_spoke_command(tenant_id, spoke_id, command):
        return "sent"

    # Spoke offline: queue it so it is delivered when the spoke reconnects.
    store.enqueue_command(
        Command(
            id=command_id,
            spoke_id=spoke_id,
            tenant_id=tenant_id,
            type="reseed",
            target="spoke",
            payload=dict(command_payload),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    return "queued"


def _refresh_backup_job_status(job: dict[str, Any]) -> None:
    """Recompute a job's overall status from its per-spoke or per-VM states."""
    # Reseed jobs: based on each spoke's status.
    if job.get("type") == "reseed":
        spoke_states = [state.get("status") for state in job.get("spoke_status", {}).values()]
        # No spokes counts as completed.
        if not spoke_states:
            job["status"] = "completed"
        # All succeeded.
        elif all(state in _RESEED_SUCCESS_STATES for state in spoke_states):
            job["status"] = "completed"
        # All finished: failed if any spoke failed.
        elif all(state in _RESEED_SUCCESS_STATES | _RESEED_ERROR_STATES for state in spoke_states):
            job["status"] = "failed" if any(state in _RESEED_ERROR_STATES for state in spoke_states) else "completed"
        # Some still in progress.
        else:
            job["status"] = "running"
        return

    # Backup jobs: once every VM is done or errored, mark failed if any errored.
    vm_states = list(job.get("vm_status", {}).values())
    if vm_states and all(state["status"] in {"done", "error"} for state in vm_states):
        job["status"] = "failed" if any(state["status"] == "error" for state in vm_states) else "completed"


async def _retry_reseed_after_delay(job_id: str, tenant_id: str, spoke_id: str, retry_count: int) -> None:
    """Retry a failed reseed on one spoke after the delay for that retry number.

    Does nothing if the job or spoke state changed in the meantime.
    """
    # No delay defined means no more retries.
    delay = _RESEED_RETRY_DELAYS.get(retry_count)
    if delay is None:
        return

    # Wait, then make sure the job still exists.
    await asyncio.sleep(delay)
    job = backup_jobs.get(job_id)
    if not job or job.get("type") != "reseed":
        return

    # Only retry if the spoke is still waiting for this same retry.
    spoke_state = job.get("spoke_status", {}).get(spoke_id)
    if not spoke_state or spoke_state.get("status") != "retrying" or spoke_state.get("retry_count") != retry_count:
        return

    # Send or queue the reseed again.
    dispatch_status = await _send_or_queue_reseed_command(
        tenant_id,
        spoke_id,
        job_id,
        job["command_payload"],
        retry_count=retry_count,
    )
    # Record the new state and when the retry happened.
    spoke_state.update(
        {
            "status": "running" if dispatch_status == "sent" else "queued",
            "step": "retry_sent" if dispatch_status == "sent" else "retry_queued",
            "last_retry_at": datetime.utcnow().isoformat(),
        }
    )
    # Recompute the overall job status and broadcast the job to connected browsers.
    _refresh_backup_job_status(job)
    await ws_broadcast({"type": "backup_progress", "job": job})


@router.get("/backup/config", response_model=BackupConfig)
def get_backup_config(_: User = Depends(auth.get_current_user)):
    """Return the backup configuration (any logged-in user)."""
    return store.load_backup_config()


@router.post("/backup/config", response_model=BackupConfig)
def save_backup_config(payload: BackupConfig, current_user: User = Depends(auth.get_current_user)):
    """Save the backup configuration (superadmin only)."""
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)
    # Save and return it.
    store.save_backup_config(payload)
    return payload


@router.post("/backup/config/key")
def set_azure_key(
    payload: SetAzureKeyRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Encrypt and store the Azure storage key (superadmin only)."""
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)
    # Reject blank keys.
    key = payload.azure_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="azure_key must not be empty")
    # Encrypt; failure (e.g. no hub key) is a 503.
    try:
        encrypted = crypto.encrypt_value(key)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Store the encrypted key in the backup config.
    config = store.load_backup_config()
    config.azure_key_enc = encrypted
    store.save_backup_config(config)
    return {"detail": "Azure key encrypted and saved"}


@router.get("/backup/installer/sas-token")
async def get_installer_sas_token(request: Request):
    """
    Generate a short-lived read-only SAS token for the installer to download blobs.
    Authenticated via X-Installer-Key header (must match INSTALLER_API_KEY env var).
    """
    # Installer access must be configured on the hub.
    installer_key = os.environ.get("INSTALLER_API_KEY", "").strip()
    if not installer_key:
        raise HTTPException(status_code=503, detail="INSTALLER_API_KEY not configured on hub")
    # The caller's key must match.
    provided = (request.headers.get("X-Installer-Key") or "").strip()
    if not provided or provided != installer_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Installer-Key")
    # Load the hub's backup settings (Azure account, container, per-spoke VM lists).
    backup_config = store.load_backup_config()
    # Decrypt the stored Azure key (503 if missing).
    azure_key = _get_azure_key(backup_config)
    # Read+list SAS for the container, valid for 2 hours.
    try:
        sas_url = crypto.generate_blob_container_sas(
            account_name=backup_config.azure_account,
            account_key=azure_key,
            container=backup_config.azure_container,
            permissions="rl",
            hours=2,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate SAS token: {exc}") from exc
    # Return the SAS URL and container details.
    return {
        "sas_url": sas_url,
        "account": backup_config.azure_account,
        "container": backup_config.azure_container,
        "expires_in_hours": 2,
    }


@router.post("/backup/trigger/{tenant_id}/{spoke_id}")
async def trigger_backup(
    tenant_id: str,
    spoke_id: str,
    payload: TriggerBackupRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Start a backup of a spoke's configured VMs to Azure Blob Storage (superadmin only).

    The Azure key is sent to the spoke once and then cleared from memory.
    """
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)

    # The spoke must exist and be approved.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")

    # This spoke must have VMs configured for backup.
    backup_config = store.load_backup_config()
    spoke_config = backup_config.spokes.get(spoke_id)
    if not spoke_config or not spoke_config.vm_ids:
        raise HTTPException(status_code=400, detail="No VMs configured for this spoke")

    # Use the key from the request, or the stored one if blank.
    azure_key = payload.azure_key.strip()
    if not azure_key:
        azure_key = _get_azure_key(backup_config)

    # Timestamp-based job id and the VMs to back up.
    job_id = f"backup-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    vm_ids = list(spoke_config.vm_ids)
    # Command for the spoke; blobs are stored under "<spoke>/<tenant>".
    command_payload = {
        "cmd_type": "backup",
        "vm_ids": vm_ids,
        "azure_account": backup_config.azure_account,
        "azure_container": backup_config.azure_container,
        "azure_key": azure_key,
        "retention": backup_config.retention,
        "spoke_id": spoke_id,
        "blob_prefix": f"{spoke_id}/{tenant_id}",
        "job_id": job_id,
    }
    # Create the job with every VM pending.
    backup_jobs[job_id] = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "spoke_id": spoke_id,
        "vm_ids": vm_ids,
        "started_at": datetime.utcnow().isoformat(),
        "status": "running",
        "vm_status": {
            vm_id: {"status": "pending", "pct": 0, "size": None, "file": None}
            for vm_id in vm_ids
        },
    }
    # Wrap it as a spoke command.
    command = _build_spoke_command(job_id, "backup", command_payload)

    # Send it live, then always clear the key from the request, payload and local variable.
    sent = False
    try:
        sent = await send_spoke_command(tenant_id, spoke_id, command)
    finally:
        payload.azure_key = ""
        command_payload.pop("azure_key", None)
        azure_key = ""

    # Spoke offline: drop the job and report 409 (backups are not queued).
    if not sent:
        backup_jobs.pop(job_id, None)
        raise HTTPException(status_code=409, detail="Spoke is not connected")

    # Return the job id so the caller can poll status.
    return {"job_id": job_id, "status": "triggered"}


@router.get("/backup/status")
def get_backup_status(current_user: User = Depends(auth.get_current_user)):
    """Return all in-memory backup/reseed jobs (superadmin only)."""
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)
    # List every job.
    return list(backup_jobs.values())


@router.get("/backup/status/{job_id}")
def get_backup_job_status(job_id: str, current_user: User = Depends(auth.get_current_user)):
    """Return one backup/reseed job (superadmin only)."""
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)
    # Return the job (404 if unknown).
    return _get_backup_job(job_id)


@router.get("/backup/templates")
async def list_templates(_: User = Depends(auth.get_current_user)):
    """List available VM templates in the backup container, newest blob per template."""
    # Load the hub's backup settings (Azure account, container, per-spoke VM lists).
    backup_config = store.load_backup_config()
    # Container URL (listing relies on public container read access).
    url = f"https://{backup_config.azure_account}.blob.core.windows.net/{backup_config.azure_container}"

    # List the container; HTTP errors are a 502.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params={"restype": "container", "comp": "list"})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure Blob API error: {exc.response.status_code}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Azure Blob API request failed: {exc}")

    # Template name -> its newest blob.
    templates: dict[str, dict[str, Any]] = {}
    for blob in _parse_blob_entries(response.text):
        # Template name is the first path part (or the whole name if no folder).
        parts = blob["name"].split("/")
        template_name = parts[0] if len(parts) > 1 else blob["name"]
        # Keep the most recently modified blob for each template.
        current = templates.get(template_name)
        if current is None or _blob_last_modified_key(blob) > _blob_last_modified_key(current):
            templates[template_name] = {
                "name": template_name,
                "latest_blob": blob["name"],
                "size": blob["size"],
                "last_modified": blob["last_modified"],
            }

    # Return templates sorted by name.
    return {"templates": sorted(templates.values(), key=lambda template: template["name"])}


@router.post("/backup/reseed")
async def trigger_reseed(
    req: ReseedRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Reseed a template from a backup blob onto one or more spokes (tenant admins).

    Commands are sent live or queued for offline spokes.
    """
    # Tenant admins only.
    tenant_id = req.tenant_id
    auth.require_tenant_admin(tenant_id, current_user)
    # Load the hub's backup settings (Azure account, container, per-spoke VM lists).
    backup_config = store.load_backup_config()

    # "all" means every approved spoke in the tenant.
    if req.spoke_ids == ["all"]:
        spoke_ids = [spoke.id for spoke in store.list_spokes(tenant_id) if spoke.status == "approved"]
    # Otherwise each listed spoke must exist and be approved.
    else:
        spoke_ids = []
        for spoke_id in req.spoke_ids:
            # The spoke must exist and be approved.
            spoke = store.get_spoke(tenant_id, spoke_id)
            if not spoke:
                raise HTTPException(status_code=404, detail=f"Spoke not found: {spoke_id}")
            if spoke.status != "approved":
                raise HTTPException(status_code=409, detail=f"Spoke is not approved: {spoke_id}")
            spoke_ids.append(spoke_id)

    # Nothing to do.
    if not spoke_ids:
        raise HTTPException(status_code=400, detail="No approved spokes available for reseed")

    # Short job id and the full blob URL.
    job_id = str(uuid4())[:8]
    blob_url = (
        f"https://{backup_config.azure_account}.blob.core.windows.net/"
        f"{backup_config.azure_container}/{req.latest_blob}"
    )
    # Command payload sent to each spoke.
    command_payload = _build_reseed_command_payload(
        backup_config,
        job_id,
        req.template_name,
        blob_url,
        req.vm_id,
    )

    # Create the job with every spoke pending.
    backup_jobs[job_id] = {
        "job_id": job_id,
        "type": "reseed",
        "tenant_id": tenant_id,
        "template_name": req.template_name,
        "latest_blob": req.latest_blob,
        "blob_url": blob_url,
        "vm_id": req.vm_id,
        "started_at": datetime.utcnow().isoformat(),
        "status": "running",
        "command_payload": command_payload,
        "spoke_status": {
            spoke_id: {"status": "pending", "step": None, "retry_count": 0, "error": None}
            for spoke_id in spoke_ids
        },
    }

    # Send or queue the command for each spoke and record the outcome.
    job = backup_jobs[job_id]
    for spoke_id in spoke_ids:
        dispatch_status = await _send_or_queue_reseed_command(tenant_id, spoke_id, job_id, command_payload)
        job["spoke_status"][spoke_id].update(
            {
                "status": "running" if dispatch_status == "sent" else "queued",
                "step": "command_sent" if dispatch_status == "sent" else "queued_offline",
            }
        )

    # Return the job id and spoke count.
    return {"job_id": job_id, "spoke_count": len(spoke_ids)}


@router.post("/backup/progress")
async def update_backup_progress(payload: BackupProgressPayload, request: Request):
    """Receive a progress update for a backup or reseed job from the local spoke process.

    Only accepted from localhost.
    """
    # Only local processes may report progress.
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Local access required")

    # Look up the job (404 if unknown).
    job = _get_backup_job(payload.job_id)
    # Reseed jobs: progress is per spoke.
    if job.get("type") == "reseed":
        if not payload.spoke_id:
            raise HTTPException(status_code=400, detail="spoke_id is required for reseed progress")

        # The spoke must belong to this job.
        spoke_state = job.get("spoke_status", {}).get(payload.spoke_id)
        if spoke_state is None:
            raise HTTPException(status_code=404, detail="Spoke not found in reseed job")

        # On failure, schedule a retry (up to 3) and mark the spoke as retrying.
        next_status = payload.status
        retry_count = int(spoke_state.get("retry_count", 0))
        if payload.status in _RESEED_ERROR_STATES:
            retry_count += 1
            if retry_count <= 3:
                next_status = "retrying"
                asyncio.create_task(
                    _retry_reseed_after_delay(job["job_id"], job["tenant_id"], payload.spoke_id, retry_count)
                )

        # Record the spoke's new state.
        spoke_state.update(
            {
                "status": next_status,
                "step": payload.step,
                "error": payload.error,
                "retry_count": retry_count,
                "updated_at": datetime.utcnow().isoformat(),
            }
        )
        # Recompute the overall job status and broadcast the job to connected browsers.
        _refresh_backup_job_status(job)
        await ws_broadcast({"type": "backup_progress", "job": job})
        # Done.
        return {"status": "ok"}

    # Backup jobs: progress is per VM.
    if payload.vm_id is None:
        raise HTTPException(status_code=400, detail="vm_id is required for backup progress")

    # The VM must belong to this job.
    vm_state = job["vm_status"].get(payload.vm_id)
    if vm_state is None:
        raise HTTPException(status_code=404, detail="VM not found in backup job")

    # Record the VM's progress.
    vm_state.update(
        {
            "status": payload.status,
            "pct": payload.pct,
            "size": payload.size,
            "file": payload.file,
            "error": payload.error,
        }
    )

    # Recompute the overall job status and broadcast the job to connected browsers.
    _refresh_backup_job_status(job)
    await ws_broadcast({"type": "backup_progress", "job": job})
    # Done.
    return {"status": "ok"}


@router.get("/backup/blobs/{tenant_id}/{spoke_id}")
async def list_backup_blobs(tenant_id: str, spoke_id: str, current_user: User = Depends(auth.get_current_user)):
    """List the backup blobs stored for a spoke in this tenant (superadmin only)."""
    # Backup administration is superadmin-only.
    _require_superadmin(current_user)

    # The spoke must exist and be approved.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")

    # Load the hub's backup settings (Azure account, container, per-spoke VM lists).
    backup_config = store.load_backup_config()
    # Backups for this spoke/tenant live under this prefix.
    prefix = f"{spoke_id}/{tenant_id}"
    url = f"https://{backup_config.azure_account}.blob.core.windows.net/{backup_config.azure_container}"

    # List the container filtered by the prefix; HTTP errors are a 502.
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                url,
                params={"restype": "container", "comp": "list", "prefix": prefix},
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Azure Blob API error: {exc.response.status_code}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Azure Blob API request failed: {exc}")

    # Return the parsed blob list.
    return _parse_blob_entries(response.text)
