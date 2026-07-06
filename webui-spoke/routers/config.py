"""Config API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    ALLOWED_CONFIG_SECTIONS,
    Any,
    ConfOverrideBody,
    HTTPException,
    OverridesSaveRequest,
    PlainTextResponse,
    Query,
    REPO_DIR,
    SimulationConfigUpdate,
    _enqueue_command_locked,
    _git_lock,
    _merge_ini_override,
    _push_pending_commands_for_targets,
    _push_to_github,
    _serialize_commands,
    _sim_conf_cache,
    _update_ini_section,
    apply_overrides,
    asyncio,
    broadcast,
    clients,
    configparser,
    datetime,
    ensure_repo_ready,
    logger,
    repo_path,
    state_lock,
    timezone,
)

router = APIRouter()




@router.get("/api/config", response_class=PlainTextResponse)
async def api_config(hostname: str | None = Query(default=None)) -> str:
    config_path = repo_path("configs", "simulation.conf")
    config_text = config_path.read_text(encoding="utf-8")

    # Apply hub-managed override (hub-connected mode) by serialising the merged parser back to text
    hub_override_path = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    if hub_override_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(config_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            config_text = buf.getvalue()
        except Exception as exc:
            logger.warning("Could not apply hub-sim-overrides for /api/config: %s", exc)

    if not hostname:
        return config_text

    async with state_lock:
        client = clients.get(hostname)
        if not client or not client.get("overrides"):
            return config_text
        return apply_overrides(config_text, client)




@router.get("/api/config/overrides", response_class=PlainTextResponse)
async def api_config_overrides() -> str:
    overrides_path = repo_path("configs", "user-overrides.conf")
    base_text = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    # Apply hub-managed user-overrides on top
    hub_override_path = REPO_DIR / "configs" / "hub-user-overrides.conf"
    if hub_override_path.exists():
        try:
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(base_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            return buf.getvalue()
        except Exception as exc:
            logger.warning("Could not apply hub-user-overrides for /api/config/overrides: %s", exc)
    return base_text




@router.get("/api/config/parsed")
async def api_config_parsed() -> dict[str, dict[str, str]]:
    config_path = repo_path("configs", "simulation.conf")
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(config_path, encoding="utf-8")
    _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")
    return {section: dict(parser.items(section)) for section in parser.sections()}




@router.post("/api/config/simulation")
async def api_config_simulation(update: SimulationConfigUpdate) -> dict[str, Any]:
    section = update.section.strip()
    if section not in ALLOWED_CONFIG_SECTIONS:
        raise HTTPException(status_code=422, detail="Invalid section name")

    config_path = repo_path("configs", "simulation.conf")
    updates = {str(key).strip(): str(value) for key, value in update.updates.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, config_path, section, updates)

    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/simulation.conf"],
                f"WebUI: update [{section}] settings",
            )
    except ValueError:
        pushed = False

    # When the kill switch is turned OFF, immediately push the change down to
    # all clients via the command inbox so they don't stay stuck in the
    # kill-switch loop waiting for their next exec-restart cycle (up to 5 min).
    # IMPORTANT: expand "all" to per-client commands at creation time — the
    # inbox filter matches exact hostname, so a single target="all" command
    # would never be delivered to any client.
    if section == "simulation" and updates.get("kill_switch") == "off":
        async with state_lock:
            known = list(clients.keys())
            targets = known or ["all"]
            for hostname in targets:
                _enqueue_command_locked(hostname, "kill_switch", {"value": "off"})
            serialized = _serialize_commands()
        await broadcast({"type": "commands_update", "commands": serialized})
        await _push_pending_commands_for_targets(targets)

    return {"status": "ok", "pushed": pushed}




@router.post("/api/config/overrides/save")
async def api_config_overrides_save(update: OverridesSaveRequest) -> dict[str, Any]:
    ensure_repo_ready()
    username = update.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")

    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    section = "simulation" if username == "__global__" else username
    flags = {str(key).strip(): str(value) for key, value in update.flags.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, overrides_path, section, flags)

    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/user-overrides.conf"],
                f"WebUI: update overrides for {username}",
            )
    except ValueError:
        pushed = False

    return {"status": "ok", "pushed": pushed}




@router.get("/api/config/user-overrides-conf")
async def api_get_user_overrides_conf() -> dict[str, str]:
    """Return user-overrides.conf content as JSON {content, mode}."""
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    content = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    return {
        "content": content,
        "mode": "local",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }




@router.put("/api/config/user-overrides-conf")
async def api_put_user_overrides_conf(body: ConfOverrideBody) -> dict[str, Any]:
    """Write the entire user-overrides.conf and push to GitHub."""
    ensure_repo_ready()
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = overrides_path.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(overrides_path)
    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/user-overrides.conf"],
                "WebUI: update user-overrides.conf",
            )
    except ValueError:
        pushed = False
    return {"ok": True, "pushed": pushed}




@router.get("/api/config/hub-sim-override", response_class=PlainTextResponse)
async def api_get_hub_sim_override() -> str:
    """Return the current hub-managed simulation.conf override, or empty string."""
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    return p.read_text(encoding="utf-8") if p.exists() else ""




@router.put("/api/config/hub-sim-override")
async def api_set_hub_sim_override(body: ConfOverrideBody) -> dict[str, Any]:
    """Write hub-managed simulation.conf override locally (standalone mode).

    In hub-connected mode this file is managed by the hub via config_update;
    this endpoint supports direct editing from the spoke UI when disconnected.
    """
    ensure_repo_ready()
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(p)
    _sim_conf_cache["sim_mtime"] = -1.0  # Invalidate cache
    return {"ok": True}




@router.delete("/api/config/hub-sim-override")
async def api_clear_hub_sim_override() -> dict[str, Any]:
    """Remove hub-managed simulation.conf override — reverts to GitHub file."""
    p = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    if p.exists():
        p.unlink()
    _sim_conf_cache["sim_mtime"] = -1.0
    return {"ok": True, "cleared": True}




@router.get("/api/config/hub-user-override", response_class=PlainTextResponse)
async def api_get_hub_user_override() -> str:
    """Return the current hub-managed user-overrides.conf override, or empty string."""
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    return p.read_text(encoding="utf-8") if p.exists() else ""




@router.put("/api/config/hub-user-override")
async def api_set_hub_user_override(body: ConfOverrideBody) -> dict[str, Any]:
    """Write hub-managed user-overrides.conf override locally (standalone mode)."""
    ensure_repo_ready()
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(p)
    return {"ok": True}




@router.delete("/api/config/hub-user-override")
async def api_clear_hub_user_override() -> dict[str, Any]:
    """Remove hub-managed user-overrides.conf override — reverts to GitHub file."""
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    if p.exists():
        p.unlink()
    return {"ok": True, "cleared": True}
