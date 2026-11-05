"""Config API routes (moved verbatim from server.py; logic imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# APIRouter groups these endpoints so server.py can mount them with include_router().
from fastapi import APIRouter
# Config helpers, models and shared state live in server.py; this file only defines routes.
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

# The router object every endpoint below is registered on.
router = APIRouter()




@router.get("/api/config", response_class=PlainTextResponse)
async def api_config(hostname: str | None = Query(default=None)) -> str:
    """Return simulation.conf as text, merged with hub overrides and, if given, one client's
    overrides.
    """
    # Start from the repo's simulation.conf.
    config_path = repo_path("configs", "simulation.conf")
    config_text = config_path.read_text(encoding="utf-8")

    # Apply hub-managed override (hub-connected mode) by serialising the merged parser back to text
    hub_override_path = REPO_DIR / "configs" / "hub-sim-overrides.conf"
    # Only merge when the hub has pushed an override file.
    if hub_override_path.exists():
        try:
            # Parse the base config preserving key case (optionxform=str), merge, and re-serialise.
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(config_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            config_text = buf.getvalue()
        except Exception as exc:
            # A broken override file is logged and ignored so clients still get the base config.
            logger.warning("Could not apply hub-sim-overrides for /api/config: %s", exc)

    # No hostname: return the shared config.
    if not hostname:
        return config_text

    # Look the client up under the shared lock; without overrides it gets the shared config.
    async with state_lock:
        client = clients.get(hostname)
        if not client or not client.get("overrides"):
            return config_text
        # Rewrite the config text with this client's per-host overrides applied.
        return apply_overrides(config_text, client)




@router.get("/api/config/overrides", response_class=PlainTextResponse)
async def api_config_overrides() -> str:
    """Return user-overrides.conf as text, with any hub-managed user overrides merged on top."""
    # Base file (empty if missing).
    overrides_path = repo_path("configs", "user-overrides.conf")
    base_text = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    # Apply hub-managed user-overrides on top
    hub_override_path = REPO_DIR / "configs" / "hub-user-overrides.conf"
    # Only merge when the hub has pushed an override file.
    if hub_override_path.exists():
        try:
            # Parse preserving key case, merge, and return the re-serialised text.
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read_string(base_text)
            _merge_ini_override(parser, hub_override_path)
            import io as _io
            buf = _io.StringIO()
            parser.write(buf)
            return buf.getvalue()
        except Exception as exc:
            # A broken override file is logged and the unmerged text is returned.
            logger.warning("Could not apply hub-user-overrides for /api/config/overrides: %s", exc)
    return base_text




@router.get("/api/config/parsed")
async def api_config_parsed() -> dict[str, dict[str, str]]:
    """Return simulation.conf (with hub overrides) as {section: {key: value}} for the UI."""
    # Parse the file preserving key case, then merge the hub override file if present.
    config_path = repo_path("configs", "simulation.conf")
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(config_path, encoding="utf-8")
    _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")
    # Convert every section into a plain dict.
    return {section: dict(parser.items(section)) for section in parser.sections()}




@router.post("/api/config/simulation")
async def api_config_simulation(update: SimulationConfigUpdate) -> dict[str, Any]:
    """Update one [section] of simulation.conf and push the change to GitHub."""
    # Only known sections may be edited.
    section = update.section.strip()
    if section not in ALLOWED_CONFIG_SECTIONS:
        raise HTTPException(status_code=422, detail="Invalid section name")

    # Normalise keys (dropping blank ones), stringify values and rewrite the section on disk.
    config_path = repo_path("configs", "simulation.conf")
    updates = {str(key).strip(): str(value) for key, value in update.updates.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, config_path, section, updates)

    # Commit and push; the git lock stops two pushes running at once.
    pushed = False
    try:
        async with _git_lock:
            pushed = await asyncio.to_thread(
                _push_to_github,
                ["configs/simulation.conf"],
                f"WebUI: update [{section}] settings",
            )
    except ValueError:
        # Push not possible (e.g. repo not configured): the local change still stands.
        pushed = False

    # When the kill switch is turned OFF, immediately push the change down to
    # all clients via the command inbox so they don't stay stuck in the
    # kill-switch loop waiting for their next exec-restart cycle (up to 5 min).
    # IMPORTANT: expand "all" to per-client commands at creation time — the
    # inbox filter matches exact hostname, so a single target="all" command
    # would never be delivered to any client.
    # Turning the kill switch off: queue a kill_switch=off command for every client.
    if section == "simulation" and updates.get("kill_switch") == "off":
        # Use known clients; fall back to "all" when none are registered yet.
        async with state_lock:
            known = list(clients.keys())
            targets = known or ["all"]
            for hostname in targets:
                _enqueue_command_locked(hostname, "kill_switch", {"value": "off"})
            serialized = _serialize_commands()
        # Tell UIs about the new commands and push them to connected clients right away.
        await broadcast({"type": "commands_update", "commands": serialized})
        await _push_pending_commands_for_targets(targets)

    return {"status": "ok", "pushed": pushed}




@router.post("/api/config/overrides/save")
async def api_config_overrides_save(update: OverridesSaveRequest) -> dict[str, Any]:
    """Save one user's flags (or the global [simulation] section) to user-overrides.conf and push.
    """
    # Make sure the local git clone exists before writing to it.
    ensure_repo_ready()
    # A username (or __global__) is required.
    username = update.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Username is required")

    # __global__ maps to the [simulation] section; other names get their own section.
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    section = "simulation" if username == "__global__" else username
    # Normalise keys (dropping blank ones), stringify values and rewrite the section on disk.
    flags = {str(key).strip(): str(value) for key, value in update.flags.items() if str(key).strip()}
    await asyncio.to_thread(_update_ini_section, overrides_path, section, flags)

    # Commit and push; a ValueError means pushing is not possible and the local change stands.
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
    # Read the file (empty if missing) and report that it came from the local copy.
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
    # Make sure the local git clone exists.
    ensure_repo_ready()
    # Write to a temp file and rename it so readers never see a half-written file.
    overrides_path = REPO_DIR / "configs" / "user-overrides.conf"
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = overrides_path.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(overrides_path)
    # Commit and push; a ValueError means pushing is not possible and the local change stands.
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
    # Make sure the local git clone exists.
    ensure_repo_ready()
    # Write to a temp file and rename it so readers never see a half-written file.
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
    # Delete the override file if present and force the config cache to reload.
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
    # Make sure the local git clone exists.
    ensure_repo_ready()
    # Write to a temp file and rename it so readers never see a half-written file.
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(p)
    return {"ok": True}




@router.delete("/api/config/hub-user-override")
async def api_clear_hub_user_override() -> dict[str, Any]:
    """Remove hub-managed user-overrides.conf override — reverts to GitHub file."""
    # Delete the override file if present.
    p = REPO_DIR / "configs" / "hub-user-overrides.conf"
    if p.exists():
        p.unlink()
    return {"ok": True, "cleared": True}
