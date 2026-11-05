"""Commands API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Command-queue helpers and shared state live in server.py; this file only defines the routes.
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

# The router object every endpoint below is registered on.
router = APIRouter()




@router.post("/api/commands")
async def create_command(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Queue a command for one device, all clients, or the proxmox agent."""
    # Read and normalise the request fields (action/type names are canonicalised).
    target = str(body.get("target", "")).strip()
    action = _normalize_command_action(body.get("action", ""))
    args = body.get("args", {})
    command_type = _normalize_command_type(body.get("type"))

    # Validate: target and action are required and args must be a JSON object.
    if not target or not action:
        raise HTTPException(status_code=422, detail="target and action are required")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")

    # Commands actually created, and how many were skipped as duplicates of queued ones.
    new_cmds: list[dict[str, Any]] = []
    deduped = 0

    # Modify the queue under the shared lock.
    async with state_lock:
        # Drop stale commands first so dedupe checks see an accurate queue.
        expired, purged = _cleanup_commands_locked()
        # target "all": queue one command per registered client.
        if target == "all":
            known = list(clients.keys())
            if not known:
                raise HTTPException(status_code=400, detail="No clients registered yet")
            for hostname in known:
                cmd, created, _expired, _purged = _enqueue_command_locked(hostname, action, args, command_type=command_type)
                # created is False when an identical command was already queued.
                if created:
                    new_cmds.append(cmd)
                else:
                    deduped += 1
        elif target == "proxmox":
            # target "proxmox": queue a single command for the proxmox agent.
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        else:
            # Otherwise the target must be a known client hostname.
            if target not in clients:
                raise HTTPException(status_code=404, detail="Client not found")
            cmd, created, _expired, _purged = _enqueue_command_locked(target, action, args, command_type=command_type)
            if created:
                new_cmds.append(cmd)
            else:
                deduped += 1
        # Snapshot the queue for the UI broadcast.
        serialized = _serialize_commands()

    # Tell UIs if the queue changed.
    if new_cmds or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    # Push the new commands immediately to any targets connected over websocket.
    if new_cmds:
        await _push_pending_commands_for_targets([cmd["target"] for cmd in new_cmds])
    return {"queued": len(new_cmds), "deduped": deduped, "ids": [c["id"] for c in new_cmds]}




@router.get("/api/commands")
async def list_commands() -> list[dict[str, Any]]:
    """Return the current in-memory command queue plus short-lived terminal results."""
    # Clean up stale entries under the lock and snapshot the queue.
    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        serialized = _serialize_commands()
    # If cleanup changed anything, tell UIs.
    if expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return serialized




@router.delete("/api/commands/pending")
async def expire_pending_for_target(target: str = Query(...)) -> dict[str, int]:
    """Expire active commands for a given target hostname before replacing a VM."""
    # Count of commands expired by this call; all share the same timestamp.
    count = 0
    now = time.time()
    # Update the queue under the shared lock, cleaning up stale entries first.
    async with state_lock:
        expired, purged = _cleanup_commands_locked(now)
        # Expire commands for this target that have not finished yet.
        for cmd in commands:
            if cmd["target"] == target and cmd["status"] in {"pending", "delivered"}:
                cmd["status"] = "expired"
                cmd["updated_at"] = now
                # Keep the expired entry visible for a while, then purge it.
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                count += 1
        # Persist only if something changed.
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    # Log and notify UIs if we expired anything, or if cleanup changed the queue.
    if count:
        logger.info("Expired %d active command(s) for target %s before VM destroy", count, target)
        await broadcast({"type": "commands_update", "commands": serialized})
    elif expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"expired": count}




@router.post("/api/commands/cancel-all")
async def cancel_all_queued_commands() -> dict[str, int]:
    """Cancel all pending/delivered commands (troubleshooting — stops queued work without deleting history)."""
    # Count of commands cancelled; all share the same timestamp.
    now = time.time()
    count = 0
    # Update the queue under the shared lock.
    async with state_lock:
        # Cancel every command that has not finished yet, noting why.
        for cmd in commands:
            if cmd.get("status") in {"pending", "delivered"}:
                cmd["status"] = "cancelled"
                cmd["updated_at"] = now
                cmd["error"] = "Manually cancelled via Cancel All"
                count += 1
        # Persist only if something changed.
        if count:
            await _async_save_commands()
        serialized = _serialize_commands()
    # Log and notify UIs when anything was cancelled.
    if count:
        logger.info("Cancel-all: %d queued command(s) cancelled by user", count)
        await broadcast({"type": "commands_update", "commands": serialized})
    return {"cancelled": count}




@router.delete("/api/commands/{cmd_id}")
async def delete_command(cmd_id: str) -> dict[str, bool]:
    """Remove a command from history."""
    # Update the queue under the shared lock.
    async with state_lock:
        # Rebuild the list without the given id, in place so other references stay valid.
        before = len(commands)
        commands[:] = [c for c in commands if c["id"] != cmd_id]
        # Nothing removed means the id did not exist.
        if len(commands) == before:
            raise HTTPException(status_code=404, detail="Command not found")
        # Persist and snapshot the new queue.
        await _async_save_commands()
        serialized = _serialize_commands()
    # Notify UIs.
    await broadcast({"type": "commands_update", "commands": serialized})
    return {"ok": True}
