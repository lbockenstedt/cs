"""T3 wireless simulation management endpoints.

Provides MAC profile CRUD and push-to-spoke for T3 devices, plus
global OUI pool management (superadmin).

MAC profile flow:
  1. Admin builds profile in hub UI  →  PUT /{tenant_id}/spokes/{spoke_id}/t3/mac-profile
  2. Hub stores profile + queues t3_mac_update command for spoke
  3. Spoke picks up command via inbox → writes mac_config.json locally
  4. T3 update_script.sh pulls GET /api/scripts/t3/mac_config.json from spoke
  5. wireless.sh detects hash change → gen_macs.sh regenerates interfaces
"""
# Allow modern type-hint syntax on older Python versions.
from __future__ import annotations

# csv/io read and write OUI pool CSV files in memory.
import csv
import io
# Time helpers for command expiry; Any for loose dict values.
from datetime import datetime, timedelta, timezone
from typing import Any

# FastAPI router, dependencies, HTTP errors and file-upload support.
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
# BaseModel validates the MAC profile request body.
from pydantic import BaseModel

# auth guards access; store persists profiles/pool/commands; models describe them.
from .. import auth, store
from ..data_models import Command, MacProfile, MacProfileEntry, User
# ws_broadcast notifies browsers when a profile is saved.
from ..ws import ws_broadcast

# Router mounted by app.main under /api.
router = APIRouter()


def _now() -> datetime:
    """Return the current UTC time (used to compute command expiry)."""
    return datetime.now(timezone.utc)


def _get_approved_spoke(tenant_id: str, spoke_id: str):
    """Return the spoke, or raise 404 if missing and 409 if not yet approved."""
    # Load the spoke within the tenant.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    # Only approved spokes may receive commands.
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")
    return spoke


class MacProfileRequest(BaseModel):
    """Body for saving a MAC profile: the list of vendor entries."""
    entries: list[MacProfileEntry]

    @property
    def total_interfaces(self) -> int:
        """Total number of interfaces requested across all entries."""
        return sum(e.count for e in self.entries)


def _serialize_profile(profile: MacProfile) -> dict[str, Any]:
    """Return a MAC profile as a JSON-friendly dict, including its interface total."""
    return {
        "spoke_id": profile.spoke_id,
        "tenant_id": profile.tenant_id,
        "entries": [e.model_dump() for e in profile.entries],
        "total_interfaces": profile.total_interfaces,
        "updated_at": profile.updated_at,
        "updated_by": profile.updated_by,
    }


# ── MAC Profile endpoints ─────────────────────────────────────────────────────

@router.get("/{tenant_id}/spokes/{spoke_id}/t3/mac-profile")
def get_mac_profile(
    tenant_id: str,
    spoke_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Get the stored MAC profile for a spoke's T3 devices."""
    # Caller must have tenant access; spoke must exist and be approved.
    _require_tenant_access(tenant_id, current_user)
    _get_approved_spoke(tenant_id, spoke_id)
    # Load the saved profile; 404 if none has been saved yet.
    profile = store.get_mac_profile(tenant_id, spoke_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No MAC profile saved for this spoke yet")
    return _serialize_profile(profile)


@router.put("/{tenant_id}/spokes/{spoke_id}/t3/mac-profile")
async def save_mac_profile(
    tenant_id: str,
    spoke_id: str,
    payload: MacProfileRequest,
    current_user: User = Depends(auth.get_current_user),
):
    """Save a MAC profile and queue a t3_mac_update command to push it to the spoke."""
    # Only tenant admins can change profiles; spoke must be approved.
    _require_tenant_admin(tenant_id, current_user)
    _get_approved_spoke(tenant_id, spoke_id)

    # Total interfaces must be between 1 and 25 (the T3 device's supported maximum).
    total = payload.total_interfaces
    if total == 0:
        raise HTTPException(status_code=400, detail="Profile must contain at least one interface")
    if total > 25:
        raise HTTPException(status_code=400, detail=f"Total interface count {total} exceeds maximum of 25")

    # Build and store the profile, recording who changed it.
    profile = MacProfile(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        entries=payload.entries,
        updated_by=current_user.username,
    )
    store.save_mac_profile(tenant_id, spoke_id, profile)

    # Queue the push command so the spoke picks it up on next inbox poll
    mac_config_list = [e.model_dump() for e in payload.entries]
    # Wrap the entries in a 24-hour t3_mac_update command.
    command = Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type="t3_mac_update",
        payload={"mac_config": mac_config_list},
        expires_at=_now() + timedelta(hours=24),
    )
    store.enqueue_command(command)

    # Let open browsers know the profile was saved and queued.
    await ws_broadcast({
        "type": "t3_mac_profile_saved",
        "tenant_id": tenant_id,
        "spoke_id": spoke_id,
        "total_interfaces": total,
        "entries": len(payload.entries),
        "command_id": command.id,
    })

    # Tell the caller what was queued.
    return {
        "status": "queued",
        "command_id": command.id,
        "total_interfaces": total,
        "entries": len(payload.entries),
        "message": f"MAC profile saved and push queued for spoke. {total} interfaces across {len(payload.entries)} vendor entries.",
    }


@router.delete("/{tenant_id}/spokes/{spoke_id}/t3/mac-profile")
def delete_mac_profile(
    tenant_id: str,
    spoke_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Remove the stored MAC profile for a spoke."""
    # Admin and spoke checks, then delete the stored profile.
    _require_tenant_admin(tenant_id, current_user)
    _get_approved_spoke(tenant_id, spoke_id)
    store.delete_mac_profile(tenant_id, spoke_id)
    return {"status": "deleted"}


@router.post("/{tenant_id}/spokes/{spoke_id}/t3/push-mac")
async def push_mac_profile(
    tenant_id: str,
    spoke_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Re-queue a push of the existing saved MAC profile (e.g. after a spoke reconnects)."""
    # Admin and spoke checks.
    _require_tenant_admin(tenant_id, current_user)
    _get_approved_spoke(tenant_id, spoke_id)
    # There must be a saved profile to re-send.
    profile = store.get_mac_profile(tenant_id, spoke_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No MAC profile saved — use PUT to create one first")

    # Queue the saved entries again as a new 24-hour t3_mac_update command.
    mac_config_list = [e.model_dump() for e in profile.entries]
    command = Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type="t3_mac_update",
        payload={"mac_config": mac_config_list},
        expires_at=_now() + timedelta(hours=24),
    )
    store.enqueue_command(command)
    return {"status": "queued", "command_id": command.id, "total_interfaces": profile.total_interfaces}


# ── T3 device visibility ──────────────────────────────────────────────────────

@router.get("/{tenant_id}/spokes/{spoke_id}/t3/devices")
def get_t3_devices(
    tenant_id: str,
    spoke_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Return T3 devices visible in the spoke's telemetry."""
    # Tenant access and spoke checks.
    _require_tenant_access(tenant_id, current_user)
    spoke = _get_approved_spoke(tenant_id, spoke_id)
    # T3 info is a section of the spoke's latest telemetry (empty if not reported).
    t3_section = spoke.telemetry.get("t3") or {}
    # Return the device list plus whether MAC config/OUI pool files are present on the spoke.
    return {
        "devices": t3_section.get("devices", []),
        "device_count": t3_section.get("device_count", 0),
        "mac_config_present": t3_section.get("mac_config_present", False),
        "mac_config_hash": t3_section.get("mac_config_hash", ""),
        "oui_pool_present": t3_section.get("oui_pool_present", False),
    }


# ── OUI Pool management (superadmin) ─────────────────────────────────────────

@router.get("/oui-pool")
def get_oui_pool(_: User = Depends(auth.get_current_user)):
    """Return the global OUI reference pool."""
    # Any logged-in user can read the pool.
    return store.get_oui_pool_raw()


@router.put("/oui-pool")
def save_oui_pool(
    entries: list[dict[str, Any]],
    _: User = Depends(auth.require_superadmin),
):
    """Replace the global OUI pool (superadmin only)."""
    # Basic validation: each entry needs vendor, oui, device_type
    # Every entry must have a vendor and OUI; report the first bad index.
    for i, e in enumerate(entries):
        if not e.get("vendor") or not e.get("oui"):
            raise HTTPException(
                status_code=400,
                detail=f"Entry {i}: 'vendor' and 'oui' are required fields",
            )
    # Replace the stored pool and report how many entries were saved.
    store.save_oui_pool_raw(entries)
    return {"status": "saved", "count": len(entries)}


@router.post("/oui-pool/import-csv")
async def import_oui_pool_csv(
    file: UploadFile = File(...),
    _: User = Depends(auth.require_superadmin),
):
    """Import oui_pool.csv (vendor,oui,device_type format) to replace the pool."""
    # Read the uploaded file and decode it, replacing invalid bytes instead of failing.
    content = await file.read()
    text = content.decode("utf-8", errors="replace")
    # Parse rows into dicts using the header line for keys.
    reader = csv.DictReader(io.StringIO(text))
    # Valid entries to save and messages for skipped rows.
    entries = []
    errors = []
    for i, row in enumerate(reader, start=2):  # start=2 because row 1 is header
        # Trim fields; lower-case the OUI so formats are consistent.
        vendor = (row.get("vendor") or "").strip()
        oui = (row.get("oui") or "").strip().lower()
        device_type = (row.get("device_type") or "").strip()
        # Rows missing vendor or OUI are skipped and reported.
        if not vendor or not oui:
            errors.append(f"Row {i}: missing vendor or oui — skipped")
            continue
        entries.append({"vendor": vendor, "oui": oui, "device_type": device_type})

    # An import that produced nothing is rejected so the pool is not wiped by accident.
    if not entries:
        raise HTTPException(status_code=400, detail="No valid entries found in CSV")

    # Replace the pool and report counts plus any skipped rows.
    store.save_oui_pool_raw(entries)
    return {
        "status": "imported",
        "count": len(entries),
        "errors": errors,
        "message": f"Imported {len(entries)} OUI entries" + (f" ({len(errors)} skipped)" if errors else ""),
    }


@router.get("/oui-pool/export-csv")
def export_oui_pool_csv(_: User = Depends(auth.get_current_user)):
    """Export the OUI pool as CSV."""
    # Load the pool and write it as CSV into an in-memory buffer.
    entries = store.get_oui_pool_raw()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["vendor", "oui", "device_type"])
    writer.writeheader()
    # One CSV row per entry, with blanks for missing fields.
    for e in entries:
        writer.writerow({
            "vendor": e.get("vendor", ""),
            "oui": e.get("oui", ""),
            "device_type": e.get("device_type", ""),
        })
    # Response lets us return raw CSV text with custom headers.
    from fastapi.responses import Response
    # Serve as a downloadable file named oui_pool.csv.
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=oui_pool.csv"},
    )


# ── Push OUI pool to a spoke ──────────────────────────────────────────────────

@router.post("/{tenant_id}/spokes/{spoke_id}/t3/push-oui-pool")
async def push_oui_pool_to_spoke(
    tenant_id: str,
    spoke_id: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Queue a t3_oui_pool_update command to push the global OUI pool to a spoke."""
    # Admin and spoke checks.
    _require_tenant_admin(tenant_id, current_user)
    _get_approved_spoke(tenant_id, spoke_id)
    # A pool must exist before it can be pushed.
    pool = store.get_oui_pool_raw()
    if not pool:
        raise HTTPException(status_code=404, detail="No OUI pool saved — upload one first via PUT /api/oui-pool")
    # Queue the whole pool for the spoke as a 24-hour t3_oui_pool_update command.
    command = Command(
        spoke_id=spoke_id,
        tenant_id=tenant_id,
        type="t3_oui_pool_update",
        payload={"oui_pool": pool},
        expires_at=_now() + timedelta(hours=24),
    )
    store.enqueue_command(command)
    return {"status": "queued", "command_id": command.id, "pool_entries": len(pool)}
