"""Commands API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    Body,
    COMMAND_RESULT_RETENTION_SECS,
    HTTPException,
    Query,
    _async_save_commands,
    _cleanup_commands_locked,
    _enqueue_command_locked,
    _normalize_command_action,
    _normalize_command_type,
    _push_pending_commands_for_targets,
    _serialize_commands,
    broadcast,
    clients,
    commands,
    logger,
    state_lock,
    time,
)

router = APIRouter()




@router.post("/api/commands")
async def create_command(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Queue a command for one device, all clients, or the proxmox agent."""
    target = str(body.get("target", "")).strip()
    action = _normalize_command_action(body.get("action", ""))
    args = body.get("args", {})
    command_type = _normalize_command_type(body.get("type"))

    if not target or not action:
        raise HTTPException(status_code=422, detail="target and action are required")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")

    new_cmds: list[dict[str, Any]] = []
    deduped = 0

    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        if target == "all":
            known = list(clients.keys())
            if not known:
                raise HTTPException(status_code=400, detail="No clients registered yet")
            for hostname in known:
                cmd, created, _expired, _purged = _enqueue_command_locked(hostname, action, args, command_type=command_type)
                if created:
                    new_cmds.append(cmd)
                else:
                    deduped += 1
        elif target == "proxmox":
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        else:
            if target not in clients:
                raise HTTPException(status_code=404, detail="Client not found")
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        serialized = _serialize_commands()

    if new_cmds or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    if new_cmds:
        await _push_pending_commands_for_targets([cmd["target"] for cmd in new_cmds])
    return {"queued": len(new_cmds), "deduped": deduped, "ids": [c["id"] for c in new_cmds]}




@router.get("/api/commands")
async def list_commands() -> list[dict[str, Any]]:
    """Return the current in-memory command queue plus short-lived terminal results."""
    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        serialized = _serialize_commands()
    if expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return serialized




@router.delete("/api/commands/pending")
async def expire_pending_for_target(target: str = Query(...)) -> dict[str, int]:
    """Expire active commands for a given target hostname before replacing a VM."""
    count = 0
    now = time.time()
    async with state_lock:
        expired, purged = _cleanup_commands_locked(now)
        for cmd in commands:
            if cmd["target"] == target and cmd["status"] in {"pending", "delivered"}:
                cmd["status"] = "expired"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                count += 1
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    if count:
        logger.info("Expired %d active command(s) for target %s before VM destroy", count, target)
        await broadcast({"type": "commands_update", "commands": serialized})
    elif expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"expired": count}




@router.post("/api/commands/cancel-all")
async def cancel_all_queued_commands() -> dict[str, int]:
    """Cancel all pending/delivered commands (troubleshooting — stops queued work without deleting history)."""
    now = time.time()
    count = 0
    async with state_lock:
        for cmd in commands:
            if cmd.get("status") in {"pending", "delivered"}:
                cmd["status"] = "cancelled"
                cmd["updated_at"] = now
                cmd["error"] = "Manually cancelled via Cancel All"
                count += 1
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    if count:
        logger.info("Cancel-all: %d queued command(s) cancelled by user", count)
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"cancelled": count}




@router.delete("/api/commands/{cmd_id}")
async def delete_command(cmd_id: str) -> dict[str, bool]:
    """Remove a command from history."""
    async with state_lock:
        before = len(commands)
        commands[:] = [c for c in commands if c["id"] != cmd_id]
        if len(commands) == before:
            raise HTTPException(status_code=404, detail="Command not found")
        await _async_save_commands()
        serialized = _serialize_commands()
    await broadcast({"type": "commands_update", "commands": serialized})
    return {"ok": True}
