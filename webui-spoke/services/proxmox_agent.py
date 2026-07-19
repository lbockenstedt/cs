"""Proxmox Agent (helpers moved verbatim from server.py; shared deps imported from server)."""
from __future__ import annotations

from server import (
    APP_VERSION,
    Any,
    BASE_DIR,
    CLIENT_SIM_REPO_RAW,
    COMMAND_EXPIRE_SECS,
    COMMAND_MAX,
    COMMAND_QUEUE_FILE,
    COMMAND_RESULT_RETENTION_SECS,
    DELETE_GATE_COOLDOWN_S,
    HTTPException,
    JSONResponse,
    PROXMOX_LOG_MAX,
    PROXMOX_WS_GRACE_SECS,
    Path,
    RECLONE_STATE_FILE,
    REPO_BRANCH,
    REPO_DIR,
    REPO_URL,
    UPDATE_CHECK_INTERVAL,
    UPDATE_STATE_FILE,
    VMID_AUDIT_INTERVAL_S,
    VM_WATCHDOG_FILE,
    VM_WATCHDOG_INTERVAL_SECS,
    VM_WATCHDOG_TIMEOUT_SECS,
    WEBUI_VMID,
    WebSocket,
    _INSTALLER_PATH,
    _RESOURCE_SAMPLE_WINDOW,
    _SIM_TAG_PREFIX,
    _atomic_write_json,
    _auto_recovery_pending_vmids,
    _autoprov_enabled,
    _autoprov_gate_log,
    _broadcast_relay_state,
    _build_registration_config,
    _client_os_counts,
    _command_trace,
    _current_provision_halt,
    _debug_event,
    _default_provision_run_state,
    _derive_provision_run_item_status,
    _ensure_relay_spoke_id,
    _get_repo_version,
    _git,
    _git_lock,
    _hostname_vm_set_number,
    _is_protected_vmid,
    _legacy_template_id,
    _merge_ini_override,
    _normalize_relay_enabled,
    _normalize_toggle,
    _parse_json_list,
    _parse_ts,
    _pending_delete_vmids,
    _persisted,
    _prepare_delete_vm_args,
    _primary_template_id,
    _proxmox_agent_vm_map,
    _proxmox_token_provision_queues,
    _record_resource_samples,
    _relay_diag_append,
    _relay_spoke_id_needs_rotation,
    _resolved_template_spec,
    _resource_1h_average,
    _resource_estimated_average,
    _sanitize_vm_set_override,
    _save_relay_state,
    _save_settings,
    _save_state_cache,
    _setting_bool,
    _setting_int,
    _sync_repo_now,
    _trace,
    _vm_has_checked_in,
    _vm_pending_checkin,
    _vmid_gap_audit_last_run,
    _vnc_sessions,
    _write_atomic_str,
    approved_proxmox_agents,
    asyncio,
    broadcast,
    client_ws_connections,
    clients,
    commands,
    configparser,
    contextlib,
    datetime,
    hashlib,
    httpx,
    iso_utcnow,
    json,
    logger,
    os,
    pending_proxmox_agents,
    proxmox_log_buffer,
    proxmox_state,
    proxmox_states,
    re,
    reclone_run_lock,
    reclone_state,
    relay_state,
    service_health,
    settings,
    shutil,
    socket,
    ssl,
    state,
    state_lock,
    subprocess,
    sync_repo_once,
    time,
    timezone,
    update_state,
    uuid,
    vm_watchdog,
    websockets,
)



async def _async_save_vm_watchdog() -> None:
    """Persist VM watchdog state without blocking the event loop."""
    serialized = json.dumps(vm_watchdog, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, VM_WATCHDOG_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist VM watchdog state to %s: %s", VM_WATCHDOG_FILE, exc)




async def _async_save_commands() -> None:
    """Persist command queue without blocking the event loop."""
    serialized = json.dumps(commands, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, COMMAND_QUEUE_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist command queue to %s: %s", COMMAND_QUEUE_FILE, exc)




async def _async_save_reclone_state() -> None:
    """Persist reclone state without blocking the event loop."""
    serialized = json.dumps(reclone_state, default=str)
    try:
        await asyncio.to_thread(_write_atomic_str, RECLONE_STATE_FILE, serialized)
    except Exception as exc:
        logger.warning("Could not persist reclone state to %s: %s", RECLONE_STATE_FILE, exc)




def _save_commands() -> None:
    try:
        _atomic_write_json(COMMAND_QUEUE_FILE, commands)
    except Exception as exc:
        logger.warning("Could not persist command queue to %s: %s", COMMAND_QUEUE_FILE, exc)




def _save_reclone_state() -> None:
    try:
        _atomic_write_json(RECLONE_STATE_FILE, reclone_state)
    except Exception as exc:
        logger.warning("Could not persist reclone state to %s: %s", RECLONE_STATE_FILE, exc)




def _save_update_state() -> None:
    try:
        _atomic_write_json(UPDATE_STATE_FILE, update_state)
    except Exception as exc:
        logger.warning("Could not persist update state to %s: %s", UPDATE_STATE_FILE, exc)




def _save_vm_watchdog() -> None:
    try:
        _atomic_write_json(VM_WATCHDOG_FILE, vm_watchdog)
    except Exception as exc:
        logger.warning("Could not persist VM watchdog state to %s: %s", VM_WATCHDOG_FILE, exc)




def _load_commands() -> None:
    try:
        if not COMMAND_QUEUE_FILE.exists():
            return
        raw = json.loads(COMMAND_QUEUE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("command queue must be a list")
        now = time.time()
        changed = False
        restored: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                changed = True
                continue
            cmd = dict(entry)
            if cmd.get("status") in ("executing", "delivered"):
                cmd["status"] = "pending"
                cmd["updated_at"] = now
                changed = True
            restored.append(cmd)
        commands[:] = restored
        expired, purged = _cleanup_commands_locked(now)
        if expired or purged:
            changed = True
        if changed:
            _save_commands()
        logger.info("Restored %d command(s) from disk", len(commands))
    except Exception as exc:
        logger.warning("Could not load command queue: %s", exc)




def _load_reclone_state() -> None:
    try:
        if not RECLONE_STATE_FILE.exists():
            return
        raw = json.loads(RECLONE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("reclone state must be an object")
        for key in reclone_state:
            if key in raw:
                reclone_state[key] = raw[key]
        changed = False
        if reclone_state.get("status") == "running":
            reclone_state["status"] = "interrupted"
            reclone_state["current_vm"] = None
            reclone_state["started_at"] = None
            log_entry = {
                "vmid": None,
                "name": "System",
                "status": "interrupted",
                "timestamp": iso_utcnow(),
                "message": "Rolling reclone interrupted by restart",
            }
            reclone_state["log"] = list(reclone_state.get("log") or []) + [log_entry]
            reclone_state["log"] = reclone_state["log"][-200:]
            changed = True
        if reclone_state.get("status") == "completed":
            last_run = reclone_state.get("last_run") or {}
            ts = _parse_ts(last_run.get("timestamp"))
            if ts and (time.time() - ts) >= 8 * 3600:
                reclone_state.update({
                    "status": "idle", "type": None, "total": 0,
                    "completed": 0, "failed": 0, "current_vm": None,
                    "log": [], "started_at": None, "last_run": None,
                    "auto_recovery_log": [],
                })
                changed = True
        if changed:
            _save_reclone_state()
        logger.info("Restored reclone state from disk")
    except Exception as exc:
        logger.warning("Could not load reclone state: %s", exc)




def _load_update_state() -> None:
    try:
        if not UPDATE_STATE_FILE.exists():
            return
        raw = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("update state must be an object")
        for key in update_state:
            # Never restore current_version from disk — it must always reflect
            # the INSTALLER_VERSION file so that a self-update restart shows the
            # new version rather than the stale pre-update value.
            if key == "current_version":
                continue
            if key in raw:
                update_state[key] = raw[key]
        if update_state.get("update_in_progress"):
            update_state["update_in_progress"] = False
            update_state["update_error"] = "Update interrupted by restart"
            _save_update_state()
        logger.info("Restored update state from disk")
    except Exception as exc:
        logger.warning("Could not load update state: %s", exc)




def _load_vm_watchdog() -> None:
    try:
        if not VM_WATCHDOG_FILE.exists():
            return
        raw = json.loads(VM_WATCHDOG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("vm watchdog state must be an object")
        restored: dict[str, dict[str, Any]] = {}
        changed = False
        for raw_vmid, entry in raw.items():
            if not isinstance(entry, dict):
                changed = True
                continue
            try:
                vmid_key = str(int(raw_vmid))
            except (TypeError, ValueError):
                changed = True
                continue
            clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
            if clone_completed_at is None:
                changed = True
                continue
            try:
                reclone_count = max(0, int(entry.get("reclone_count", 0) or 0))
            except (TypeError, ValueError):
                reclone_count = 0
                changed = True
            normalized = {
                "clone_completed_at": clone_completed_at,
                "reclone_count": reclone_count,
                "hostname": str(entry.get("hostname") or "").strip(),
            }
            if entry != normalized or raw_vmid != vmid_key:
                changed = True
            restored[vmid_key] = normalized
        vm_watchdog.clear()
        vm_watchdog.update(restored)
        if changed:
            _save_vm_watchdog()
        logger.info("Restored VM watchdog state from disk")
    except Exception as exc:
        logger.warning("Could not load VM watchdog state: %s", exc)




def _sanitize_proxmox_tag(name: str) -> str:
    """Normalize a simulation name to a Proxmox-safe tag with sim- prefix."""
    name = re.sub(r'[^a-z0-9]+', '-', str(name).strip().lower()).strip('-')
    tag = f"{_SIM_TAG_PREFIX}{name}" if not name.startswith(_SIM_TAG_PREFIX) else name
    return tag[:64] if name else ""




def _get_proxmox_token_for_host(hostname: str | None) -> str:
    """Return per-host token if set, falling back to the legacy global token."""
    if hostname:
        per_host = str((settings.get("proxmox_tokens") or {}).get(hostname, "") or "").strip()
        if per_host:
            return per_host
    return str(settings.get("proxmox_api_token", "") or "").strip()




def _get_proxmox_host_config(hostname: Any) -> dict[str, Any]:
    proxmox_config = settings.get("proxmox_config") or {}
    if not isinstance(proxmox_config, dict):
        return {}
    normalized = _normalize_proxmox_hostname(hostname)
    if not normalized:
        return {}
    resolved = _resolve_proxmox_agent_hostname(normalized, proxmox_config) or normalized
    data = proxmox_config.get(resolved, {})
    return dict(data) if isinstance(data, dict) else {}




def _save_proxmox_host_config(hostname: str, updates: dict[str, Any]) -> dict[str, Any]:
    proxmox_config = settings.setdefault("proxmox_config", {})
    persisted_config = _persisted.setdefault("proxmox_config", {})
    current = proxmox_config.get(hostname, {})
    if not isinstance(current, dict):
        current = {}
    entry = dict(current)
    if "vm_set_override" in updates:
        vm_set_override = _sanitize_vm_set_override(updates.get("vm_set_override"))
        if vm_set_override:
            entry["vm_set_override"] = vm_set_override
        else:
            entry.pop("vm_set_override", None)
    if entry:
        proxmox_config[hostname] = entry
        persisted_config[hostname] = dict(entry)
    else:
        proxmox_config.pop(hostname, None)
        persisted_config.pop(hostname, None)
    _save_settings()
    return dict(entry)




def _has_any_proxmox_token() -> bool:
    if _get_proxmox_token_for_host(None):
        return True
    return any(str(tok or "").strip() for tok in (settings.get("proxmox_tokens") or {}).values())




def _save_proxmox_token_for_host(hostname: str, token: str) -> None:
    tokens = settings.setdefault("proxmox_tokens", {})
    persisted_tokens = _persisted.setdefault("proxmox_tokens", {})
    tokens[hostname] = token
    persisted_tokens[hostname] = token
    _save_settings()




def _update_service_health(name: str, *, ok: bool, error: str | None = None) -> None:
    now = iso_utcnow()
    entry = service_health.setdefault(name, {"run_count": 0, "consecutive_errors": 0})
    entry["last_run"] = now
    entry["run_count"] = entry.get("run_count", 0) + 1
    if ok:
        entry["last_success"] = now
        entry["last_error_msg"] = None
        entry["consecutive_errors"] = 0
        entry["status"] = "ok"
    else:
        entry["last_error_msg"] = error or "unknown error"
        entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
        entry["status"] = "error" if entry["consecutive_errors"] >= 3 else "warning"




def _normalize_command_action(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")




def _normalize_command_type(value: Any) -> str | None:
    normalized = str(value or "").strip().replace("-", "_")
    return normalized or None




def _command_args_signature(args: dict[str, Any] | None) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        safe_args = json.loads(json.dumps(args or {}, default=str))
        return json.dumps(safe_args, sort_keys=True, separators=(",", ":"))




def _trim_commands_locked() -> None:
    if len(commands) <= COMMAND_MAX:
        return
    terminal = {"completed", "failed", "expired"}
    idx = 0
    while len(commands) > COMMAND_MAX and idx < len(commands):
        if commands[idx].get("status") in terminal:
            del commands[idx]
            continue
        idx += 1
    if len(commands) > COMMAND_MAX:
        del commands[:len(commands) - COMMAND_MAX]




def _cleanup_commands_locked(now: float | None = None) -> tuple[int, int]:
    now = now or time.time()
    expired = 0
    for cmd in commands:
        if cmd.get("status") in {"pending", "delivered"} and (now - cmd.get("created_at", now)) > COMMAND_EXPIRE_SECS:
            cmd["status"] = "expired"
            cmd["updated_at"] = now
            cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
            expired += 1
            _trace(
                "command_expired",
                cmd_id=cmd.get("id"),
                action=cmd.get("action"),
                target=cmd.get("target"),
                age_secs=round(now - cmd.get("created_at", now)),
            )

    before = len(commands)
    commands[:] = [
        cmd for cmd in commands
        if cmd.get("status") not in {"completed", "failed", "expired"}
        or now < float(cmd.get("purge_after") or cmd.get("updated_at", cmd.get("created_at", now)) + COMMAND_RESULT_RETENTION_SECS)
    ]
    purged = before - len(commands)
    _trim_commands_locked()
    if expired or purged:
        _save_commands()
    return expired, purged




def _find_active_duplicate_command_locked(target: str, action: str, args: dict[str, Any]) -> dict[str, Any] | None:
    args_sig = _command_args_signature(args)
    for cmd in commands:
        if cmd.get("target") != target or cmd.get("action") != action:
            continue
        if cmd.get("status") not in {"pending", "delivered"}:
            continue
        if _command_args_signature(cmd.get("args", {})) == args_sig:
            return cmd
    return None




def _enqueue_command_locked(target: str, action: str, args: dict[str, Any] | None = None, command_type: str | None = None, relay: bool = False) -> tuple[dict[str, Any], bool, int, int]:
    normalized_action = _normalize_command_action(action)
    normalized_type = _normalize_command_type(command_type)
    normalized_args = dict(args or {})
    if target == "proxmox" and normalized_action == "delete_vm":
        # Hub relay commands use lenient validation — inventory may be stale or not yet loaded.
        # The proxmox agent performs the real validation before executing the delete.
        normalized_args = _prepare_delete_vm_args(normalized_args, strict=not relay)

    # Block all single-VM actions on protected VMIDs (start, stop, reboot, snapshot, reclone)
    _VM_ACTIONS = {"start_vm", "stop_vm", "reboot_vm", "snapshot_vm", "reclone_vm", "delete_vm"}
    if target == "proxmox" and normalized_action in _VM_ACTIONS:
        vmid = normalized_args.get("vmid")
        if _is_protected_vmid(vmid):
            raise HTTPException(
                status_code=403,
                detail=f"VM {vmid} is protected and cannot be managed from this UI",
            )

    now = time.time()
    expired, purged = _cleanup_commands_locked(now)
    existing = _find_active_duplicate_command_locked(target, normalized_action, normalized_args)
    if existing is not None:
        return existing, False, expired, purged

    cmd = _make_command(target, normalized_action, normalized_args, command_type=normalized_type)
    commands.append(cmd)
    _trim_commands_locked()
    _save_commands()
    return cmd, True, expired, purged




def _make_command(target: str, action: str, args: dict | None = None, command_type: str | None = None) -> dict[str, Any]:
    now = time.time()
    return {
        "id": str(uuid.uuid4()),
        "target": target,
        "action": _normalize_command_action(action),
        "args": dict(args or {}),
        "type": _normalize_command_type(command_type),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + COMMAND_EXPIRE_SECS,
        "purge_after": None,
        "result": None,
        "message": None,
    }




def _serialize_command_for_agent(command: dict[str, Any]) -> dict[str, Any]:
    return {"id": command["id"], "action": command["action"], "args": command["args"], "type": command.get("type")}




def _command_matches_agent(command: dict[str, Any], hostname: str, approved_hostname: str | None = None) -> bool:
    if command["target"] == hostname:
        return True
    if approved_hostname is None:
        return False
    return (
        _proxmox_hostnames_match(command["target"], approved_hostname)
        or command["target"] == "proxmox"
    )




def _reset_delivered_commands_locked(hostname: str, approved_hostname: str | None = None) -> int:
    """On WS reconnect, reset 'delivered' commands back to 'pending' so they are re-sent.

    Commands that were pushed via WS and marked 'delivered' but never acked (because the
    connection dropped before the agent could process them) would otherwise be silently
    abandoned — _peek_pending_agent_commands_locked only returns 'pending' commands.
    Resetting them to 'pending' ensures they are re-delivered on the next push.
    """
    now = time.time()
    reset = 0
    for cmd in commands:
        if cmd.get("status") == "delivered" and _command_matches_agent(cmd, hostname, approved_hostname):
            cmd["status"] = "pending"
            cmd["updated_at"] = now
            reset += 1
    if reset:
        _save_commands()
    return reset




def _peek_pending_agent_commands_locked(hostname: str, approved_hostname: str | None = None) -> tuple[list[dict[str, Any]], int, int]:
    expired, purged = _cleanup_commands_locked()
    pending = [
        command for command in commands
        if command["status"] == "pending" and _command_matches_agent(command, hostname, approved_hostname)
    ]
    return pending, expired, purged




def _mark_commands_delivered_locked(command_ids: list[str]) -> bool:
    if not command_ids:
        return False
    ids = set(command_ids)
    now = time.time()
    changed = False
    for command in commands:
        if command["id"] in ids and command["status"] == "pending":
            command["status"] = "delivered"
            command["updated_at"] = now
            changed = True
    if changed:
        _save_commands()
    return changed




async def _push_pending_agent_commands(hostname: str, websocket: WebSocket, approved_hostname: str | None = None) -> bool:
    async with state_lock:
        pending, expired, purged = _peek_pending_agent_commands_locked(hostname, approved_hostname)
        payload = [_serialize_command_for_agent(command) for command in pending]
        serialized = _serialize_commands()
    if not payload:
        if expired or purged:
            await broadcast({"type": "commands_update", "commands": serialized})
        return True
    _trace("agent_ws_push", hostname=hostname,
           commands=[{"action": c.get("action"), "args": {k: v for k, v in (c.get("args") or {}).items() if k in {"vmid", "vm_type"}}} for c in payload])
    try:
        await websocket.send_json({"type": "commands", "commands": payload})
    except Exception as exc:
        _trace("agent_ws_push_err", hostname=hostname, error=str(exc))
        return False
    async with state_lock:
        changed = _mark_commands_delivered_locked([command["id"] for command in pending])
        serialized = _serialize_commands()
    if changed or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    return True




async def _push_pending_commands_for_target(target: str) -> None:
    normalized = str(target or "").strip()
    if not normalized:
        return
    websocket = client_ws_connections.get(normalized)
    if websocket is not None:
        if not await _push_pending_agent_commands(normalized, websocket):
            client_ws_connections.pop(normalized, None)
    if state.proxmox_ws_connection is not None and state.proxmox_ws_hostname and (
        normalized == "proxmox" or _proxmox_hostnames_match(normalized, state.proxmox_ws_hostname)
    ):
        if not await _push_pending_agent_commands(state.proxmox_ws_hostname, state.proxmox_ws_connection, state.proxmox_ws_hostname):
            state.proxmox_ws_connection = None
            state.proxmox_ws_hostname = None




async def _push_pending_commands_for_targets(targets: list[str]) -> None:
    seen: set[str] = set()
    for target in targets:
        normalized = str(target or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            await _push_pending_commands_for_target(normalized)




def _serialize_commands() -> list[dict[str, Any]]:
    return [
        {**cmd, "age_secs": int(time.time() - cmd["created_at"])}
        for cmd in commands
    ]




def _vm_watchdog_key(vmid: Any) -> str | None:
    try:
        return str(int(vmid))
    except (TypeError, ValueError):
        return None




def _record_vm_watchdog_clone_completed(
    vmid: Any,
    hostname: Any,
    *,
    clone_completed_at: float | None = None,
    reclone_count: int | None = None,
) -> bool:
    vmid_key = _vm_watchdog_key(vmid)
    if vmid_key is None:
        return False
    current = vm_watchdog.get(vmid_key) or {}
    if reclone_count is None:
        try:
            reclone_count = max(0, int(current.get("reclone_count", 0) or 0))
        except (TypeError, ValueError):
            reclone_count = 0
    vm_watchdog[vmid_key] = {
        "clone_completed_at": float(clone_completed_at if clone_completed_at is not None else time.time()),
        "reclone_count": max(0, int(reclone_count)),
        "hostname": str(hostname or current.get("hostname") or "").strip(),
    }
    return True




def _proxmox_usb_config_payload(hostname: str | None = None) -> dict[str, Any]:
    # Read sim_phy from the repo's simulation.conf so the agent knows which
    # USB device type (wired/wireless/any) to provision and assign.
    sim_phy = "wireless"
    try:
        sim_conf = REPO_DIR / "configs" / "simulation.conf"
        if sim_conf.exists():
            parser = configparser.ConfigParser()
            parser.read_string(sim_conf.read_text(encoding="utf-8"))
            _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")
            sim_phy = parser.get("simulation", "sim_phy", fallback="wireless").strip().lower() or "wireless"
    except Exception:
        pass
    if sim_phy not in {"wireless", "ethernet", "any"}:
        sim_phy = "wireless"
    image1_template_spec = _resolved_template_spec(settings, 1)
    image2_template_spec = _resolved_template_spec(settings, 2)
    host_config = _get_proxmox_host_config(hostname) if hostname else {}
    vm_set_override = _sanitize_vm_set_override(host_config.get("vm_set_override", 0))
    return {
        "vidpids": _parse_json_list(settings.get("usb_vidpids", "[]")),
        "missing_timeout": _setting_int("usb_missing_timeout", 60, 1),
        # Template field accepts a vmid OR a name — emit the resolved value as
        # a STRING (a name can't be int-coerced). Numeric specs come out as a
        # digit string; the pxmx agent's _resolve_template_vmid handles both.
        "image1_template_id": _primary_template_id(image1_template_spec, _legacy_template_id(settings, 1)),
        "image1_template_spec": image1_template_spec,
        "image2_template_id": _primary_template_id(image2_template_spec, _legacy_template_id(settings, 2)),
        "image2_template_spec": image2_template_spec,
        "template_vmid_specs": [image1_template_spec, image2_template_spec],
        "image1_pct": max(0, min(100, int(str(settings.get("vm_image_1_pct", "50")).strip() or "50"))),
        "auto_provision": _normalize_toggle(settings.get("usb_auto_provision", "off")),
        "use_all_dongles": _setting_bool("use_all_dongles", False),
        "max_slots": max(1, min(256, int(str(settings.get("usb_max_slots", "24")).strip() or "24"))),
        "vmid_start": int(settings.get("vmid_start", 0) or 0),
        "vm_set_override": vm_set_override,
        "ignored_vidpids": _parse_json_list(settings.get("usb_ignored_vidpids", "[]")),
        "sim_phy": sim_phy,
        "reclone_concurrency": max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1")),
        "l1_vlan_start": max(1, min(4094, int(str(settings.get("l1_vlan_start", "100")).strip() or "100"))),
        "l1_vlan_end": max(1, min(4094, int(str(settings.get("l1_vlan_end", "199")).strip() or "199"))),
        "guest_agent_watchdog_enabled": _normalize_toggle(settings.get("guest_agent_watchdog_enabled", "on")),
        "guest_agent_grace_minutes": max(1, int(str(settings.get("guest_agent_grace_minutes", "20")).strip() or "20")),
        "guest_agent_check_interval_minutes": max(1, int(str(settings.get("guest_agent_check_interval_minutes", "10")).strip() or "10")),
        "guest_agent_reboot_after_minutes": max(1, int(str(settings.get("guest_agent_reboot_after_minutes", "10")).strip() or "10")),
        "guest_agent_reclone_after_minutes": max(1, int(str(settings.get("guest_agent_reclone_after_minutes", "30")).strip() or "30")),
        "watchdog_reboot_enabled": _normalize_toggle(settings.get("watchdog_reboot_enabled", "on")),
        "cpu_provision_threshold": max(0, min(100, int(str(settings.get("cpu_provision_threshold", "80")).strip() or "80"))),
        "mem_provision_threshold": max(0, min(100, int(str(settings.get("mem_provision_threshold", "80")).strip() or "80"))),
    }




def _normalize_proxmox_hostname(hostname: Any) -> str:
    return str(hostname or "").strip().rstrip(".").lower()




def _proxmox_hostname_aliases(hostname: Any) -> tuple[str, ...]:
    normalized = _normalize_proxmox_hostname(hostname)
    if not normalized:
        return ()
    aliases = [normalized]
    short = normalized.split(".", 1)[0]
    if short and short not in aliases:
        aliases.append(short)
    return tuple(aliases)




def _proxmox_hostnames_match(left: Any, right: Any) -> bool:
    left_aliases = set(_proxmox_hostname_aliases(left))
    return bool(left_aliases and left_aliases.intersection(_proxmox_hostname_aliases(right)))




def _resolve_proxmox_agent_hostname(hostname: Any, registry: dict[str, Any]) -> str | None:
    if not isinstance(registry, dict):
        return None
    for registered_hostname in registry:
        if _proxmox_hostnames_match(hostname, registered_hostname):
            return registered_hostname
    return None




def _upsert_pending_proxmox_agent(hostname: Any, client_ip: str, now: float) -> str | None:
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
    if not resolved_hostname:
        resolved_hostname = _normalize_proxmox_hostname(hostname)
    if not resolved_hostname:
        return None
    entry = pending_proxmox_agents.get(resolved_hostname)
    if entry is None:
        pending_proxmox_agents[resolved_hostname] = {"ip": client_ip, "first_seen": now, "last_seen": now}
    else:
        entry["ip"] = client_ip
        entry["last_seen"] = now
    return resolved_hostname




def _pending_proxmox_payload() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "hostname": hostname,
            "ip": info.get("ip", ""),
            "first_seen": info.get("first_seen", now),
            "last_seen": info.get("last_seen", now),
        }
        for hostname, info in pending_proxmox_agents.items()
    ]




def _approved_proxmox_payload() -> list[dict[str, Any]]:
    result = []
    for hostname in approved_proxmox_agents:
        state = proxmox_states.get(hostname, {})
        host_config = _get_proxmox_host_config(hostname)
        vm_set_override = _sanitize_vm_set_override(state.get("vm_set_override", host_config.get("vm_set_override", 0)))
        result.append({
            "hostname": hostname,
            "connected": bool(state.get("connected", False)),
            "last_seen": state.get("last_seen"),
            "agent_version": state.get("agent_version"),
            "pve_version": state.get("pve_version"),
            "vm_count": int(state.get("vm_count", 0)),
            "usb_count": int(state.get("usb_count", 0)),
            "node": state.get("node", {}),
            "provision_halt": _current_provision_halt(state),
            "cpu_1h_avg": state.get("cpu_1h_avg"),
            "mem_1h_avg": state.get("mem_1h_avg"),
            "vmid_range": state.get("vmid_range"),
            "vm_set_override": vm_set_override,
            "effective_vm_set": int(state.get("effective_vm_set", vm_set_override or _hostname_vm_set_number(hostname))),
        })
    return result




def _proxmox_status_payload() -> dict[str, Any]:
    node = proxmox_state.get("node") or {}
    client_seen = {hostname: client.get("last_seen") for hostname, client in clients.items()}
    usb_by_vmid = {
        str(entry.get("vmid")): entry
        for entry in proxmox_state.get("usb_state", [])
        if entry.get("vmid") is not None
    }
    vms = []
    current_vmids: set[int] = set()
    for vm in proxmox_state.get("vms") or []:
        enriched_vm = dict(vm)
        enriched_vm["pending_checkin"] = _vm_pending_checkin(enriched_vm, client_seen)
        enriched_vm["watchdog_tracked"] = bool(_vm_watchdog_key(vm.get("vmid")) and vm_watchdog.get(_vm_watchdog_key(vm.get("vmid"))))
        usb_entry = usb_by_vmid.get(str(vm.get("vmid")), {})
        enriched_vm["prov_status"] = usb_entry.get("prov_status") or "active"
        try:
            vmid_int = int(vm.get("vmid"))
            current_vmids.add(vmid_int)
            if vmid_int in _pending_delete_vmids:
                enriched_vm["status"] = "deleting"
        except (TypeError, ValueError):
            pass
        vms.append(enriched_vm)
    # Include any pending-delete VMIDs that have already been removed from agent telemetry
    # so the UI keeps showing them as "deleting…" until the next full render cycle.
    for pending_vmid in _pending_delete_vmids:
        if pending_vmid not in current_vmids:
            vms.append({
                "vmid": pending_vmid,
                "name": f"VM {pending_vmid}",
                "status": "deleting",
                "type": "qemu",
                "prov_status": "active",
                "pending_checkin": False,
                "watchdog_tracked": False,
            })
    return {
        **proxmox_state,
        "vms": vms,
        "prov_run": dict(proxmox_state.get("prov_run") or {}),
        "hostname": str(node.get("hostname") or "").strip(),
        "pending_proxmox": _pending_proxmox_payload(),
        "approved_proxmox": _approved_proxmox_payload(),
        "reclone_state": dict(reclone_state),
        "client_os_counts": _client_os_counts(),
        "auto_recovery_pending": _auto_recovery_pending_vmids(),
        "webui_vmid": WEBUI_VMID,
        "reseed_in_progress": bool(state._proxmox_reseed_in_progress),
        "cpu_1h_avg": _resource_1h_average(state._cpu_samples),
        "mem_1h_avg": _resource_1h_average(state._mem_samples),
        "provision_halt": _current_provision_halt(),
        "cpu_est_avg": _resource_estimated_average(state._cpu_samples),
        "mem_est_avg": _resource_estimated_average(state._mem_samples),
        "resource_samples_started": state._resource_samples_started or None,
        "resource_sample_count": len(state._cpu_samples),
        "pending_command_count": len([c for c in commands if c.get("status") in ("queued", "delivered")]),
        "spoke_version": APP_VERSION,
    }




def _find_proxmox_vm(vmid: int) -> dict[str, Any] | None:
    for vm in proxmox_state.get("vms") or []:
        try:
            if int(vm.get("vmid")) == vmid:
                return dict(vm)
        except (TypeError, ValueError):
            continue
    return None




async def _broadcast_proxmox_state() -> None:
    _save_state_cache()
    payload = _proxmox_status_payload()
    h = hashlib.md5(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    if h == state._last_proxmox_hash:
        return
    state._last_proxmox_hash = h
    await broadcast({"type": "proxmox_update", **payload})




async def _broadcast_reclone_state() -> None:
    await _async_save_reclone_state()
    await broadcast({"type": "reclone_update", **dict(reclone_state)})




def _update_reclone_log(vmid: int, name: str, status: str, message: str | None = None) -> None:
    timestamp = iso_utcnow()
    for entry in reversed(reclone_state["log"]):
        if entry.get("vmid") == vmid and entry.get("status") in {"queued", "in_progress"}:
            entry.update({"name": name, "status": status, "timestamp": timestamp})
            if message:
                entry["message"] = message
            elif entry.get("message") and status in {"queued", "in_progress"}:
                entry.pop("message", None)
            break
    else:
        entry = {"vmid": vmid, "name": name, "status": status, "timestamp": timestamp}
        if message:
            entry["message"] = message
        reclone_state["log"].append(entry)
    reclone_state["log"] = reclone_state["log"][-200:]
    _save_reclone_state()




def _parse_reclone_schedule(value: Any) -> tuple[str, int, int] | None:
    raw = str(value or "").strip().lower()
    parts = raw.split()
    if len(parts) != 2:
        return None
    day, clock = parts
    if day not in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
        return None
    try:
        hour, minute = (int(piece) for piece in clock.split(":", 1))
    except ValueError:
        return None
    if hour not in range(24) or minute not in range(60):
        return None
    return day, hour, minute




def _has_pending_reclone(vmid: int) -> bool:
    for cmd in commands:
        if cmd.get("action") != "reclone_vm":
            continue
        if int(cmd.get("args", {}).get("vmid", -1)) != vmid:
            continue
        if cmd.get("status") in {"pending", "delivered"}:
            return True
    return False




def _proxmox_unassigned_present_usb() -> list[dict[str, Any]]:
    assigned_buses = {
        str(entry.get("bus_path", "")).strip()
        for entry in proxmox_state.get("usb_state", [])
        if str(entry.get("bus_path", "")).strip() and entry.get("vmid") is not None
    }
    return [
        dict(entry)
        for entry in proxmox_state.get("present_usb", [])
        if str(entry.get("bus_path", "")).strip()
        and str(entry.get("bus_path", "")).strip() not in assigned_buses
    ]





def _normalize_proxmox_usb_state(
    usb_state: Any,
    present_usb: Any,
) -> list[dict[str, Any]]:
    present_by_bus = {
        str(entry.get("bus_path", "")).strip(): dict(entry)
        for entry in (present_usb if isinstance(present_usb, list) else [])
        if isinstance(entry, dict) and str(entry.get("bus_path", "")).strip()
    }
    normalized: list[dict[str, Any]] = []
    for raw_entry in usb_state if isinstance(usb_state, list) else []:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        bus_path = str(entry.get("bus_path", "")).strip()
        present_entry = present_by_bus.get(bus_path)
        if present_entry:
            entry["missing_since"] = None
            if entry.get("prov_status") in {None, "", "missing", "tearing_down"}:
                entry["prov_status"] = "active"
            if not entry.get("vidpid") and present_entry.get("vidpid"):
                entry["vidpid"] = present_entry.get("vidpid")
            if not entry.get("name") and present_entry.get("name"):
                entry["name"] = present_entry.get("name")
        normalized.append(entry)
    return normalized




def _update_provision_run_state(vms: list[dict[str, Any]], usb_state: list[dict[str, Any]], now: int) -> None:
    run = _default_provision_run_state()
    current = proxmox_state.get("prov_run")
    if isinstance(current, dict):
        for key in ("running", "started_at", "updated_at", "completed_at", "total", "completed", "failed"):
            run[key] = current.get(key)
        run["items"] = [
            dict(item)
            for item in current.get("items", [])
            if isinstance(item, dict) and item.get("vmid") is not None
        ]

    vm_by_vmid = {
        str(vm.get("vmid")): dict(vm)
        for vm in (vms if isinstance(vms, list) else [])
        if isinstance(vm, dict) and vm.get("vmid") is not None
    }
    usb_by_vmid = {
        str(entry.get("vmid")): dict(entry)
        for entry in (usb_state if isinstance(usb_state, list) else [])
        if isinstance(entry, dict) and entry.get("vmid") is not None
    }
    provisioning_vmids = [
        vmid
        for vmid, entry in usb_by_vmid.items()
        if str(entry.get("prov_status") or "").strip().lower() == "provisioning"
    ]
    provisioning_vmids.sort(key=lambda value: int(value) if str(value).isdigit() else value)

    if not run.get("running") and provisioning_vmids:
        run = _default_provision_run_state()
        run["running"] = True
        run["started_at"] = now
        run["updated_at"] = now

    items = run["items"]
    item_by_vmid = {str(item.get("vmid")): item for item in items if item.get("vmid") is not None}

    if run.get("running"):
        for vmid in provisioning_vmids:
            entry = usb_by_vmid[vmid]
            item = item_by_vmid.get(vmid)
            if item is None:
                vm = vm_by_vmid.get(vmid) or {}
                item = {
                    "vmid": entry.get("vmid"),
                    "vm_name": str(vm.get("name") or "").strip() or None,
                    "usb_name": str(entry.get("name") or "").strip() or None,
                    "bus_path": str(entry.get("bus_path") or "").strip() or None,
                    "vidpid": str(entry.get("vidpid") or "").strip() or None,
                    "status": _derive_provision_run_item_status(entry, vm_by_vmid),
                    "started_at": now,
                    "updated_at": now,
                    "completed_at": None,
                }
                items.append(item)
                item_by_vmid[vmid] = item
            elif item.get("status") in {"done", "failed"}:
                item.update({
                    "status": _derive_provision_run_item_status(entry, vm_by_vmid),
                    "started_at": now,
                    "updated_at": now,
                    "completed_at": None,
                })

    for item in items:
        vmid_key = str(item.get("vmid"))
        entry = usb_by_vmid.get(vmid_key)
        vm = vm_by_vmid.get(vmid_key) or {}
        if vm.get("name"):
            item["vm_name"] = str(vm.get("name"))
        if entry:
            if entry.get("name"):
                item["usb_name"] = str(entry.get("name"))
            if entry.get("bus_path"):
                item["bus_path"] = str(entry.get("bus_path"))
            if entry.get("vidpid"):
                item["vidpid"] = str(entry.get("vidpid"))

        previous_status = str(item.get("status") or "pending")
        next_status = previous_status
        if entry and str(entry.get("prov_status") or "").strip().lower() == "provisioning":
            enriched = next((v for v in vms if str(v.get("vmid")) == vmid_key), None)
            # If the watchdog confirms the client has already checked in, the USB state
            # is just lagging — treat as done rather than staying stuck at "configuring".
            if (enriched
                    and enriched.get("watchdog_tracked")
                    and not enriched.get("pending_checkin")
                    and str(enriched.get("status", "")).lower() == "running"):
                next_status = "done"
                item["completed_at"] = item.get("completed_at") or now
            else:
                next_status = _derive_provision_run_item_status(entry, vm_by_vmid)
                item["completed_at"] = None
        elif entry and str(entry.get("prov_status") or "").strip().lower() == "active":
            if previous_status != "failed":
                # Clone finished — keep as "pending_checkin" until the VM's client
                # actually contacts the API (pending_checkin flag on the enriched VM).
                # This keeps run.running=True and the live panel visible through the
                # boot-up gap between clone-complete and first API check-in.
                enriched = next(
                    (v for v in vms if str(v.get("vmid")) == vmid_key),
                    None,
                )
                if enriched and enriched.get("pending_checkin"):
                    next_status = "pending_checkin"
                    item["completed_at"] = None
                else:
                    next_status = "done"
                    item["completed_at"] = item.get("completed_at") or now
        elif run.get("running") and previous_status not in {"done", "failed"}:
            if state._prev_usb_by_vmid.get(vmid_key) == "provisioning":
                next_status = "failed"
                item["completed_at"] = item.get("completed_at") or now

        if next_status != previous_status or (entry and str(entry.get("prov_status") or "").strip().lower() == "provisioning"):
            item["updated_at"] = now
        item["status"] = next_status

    run["total"] = len(items)
    run["completed"] = sum(1 for item in items if item.get("status") == "done")
    run["failed"] = sum(1 for item in items if item.get("status") == "failed")

    # "pending_checkin" is an active (non-terminal) status — keep run alive
    active_items = [item for item in items if item.get("status") not in {"done", "failed"}]
    if run.get("running") and items and not active_items:
        run["running"] = False
        run["completed_at"] = now
        run["updated_at"] = now
    elif run.get("running"):
        run["updated_at"] = now

    proxmox_state["prov_run"] = run




def _guest_supports_reclone(vm: dict[str, Any]) -> bool:
    if vm.get("is_template"):
        return False
    if _is_protected_vmid(vm.get("vmid")):
        return False
    return bool(vm.get("reclone_supported"))




def _reclone_targets_for_run() -> list[dict[str, Any]]:
    return sorted(
        [
            dict(vm)
            for vm in proxmox_state.get("vms") or []
            if (
                vm.get("vmid") is not None
                and _guest_supports_reclone(vm)
                and int(vm.get("vmid", 0)) > 9000  # only auto-provisioned sim clients
            )
        ],
        key=lambda vm: int(vm.get("vmid", 0)),
    )





def _reclone_command_args(vm: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "vmid": int(vm.get("vmid")),
        "type": str(vm.get("type") or "qemu"),
    }
    if vm.get("reclone_source_vmid") is not None:
        args["source_vmid"] = int(vm["reclone_source_vmid"])
    if vm.get("reclone_bus_path"):
        args["bus_path"] = str(vm["reclone_bus_path"])
    return args




async def _queue_command(target: str, action: str, args: dict[str, Any] | None = None, command_type: str | None = None) -> dict[str, Any]:
    async with state_lock:
        cmd, created, expired, purged = _enqueue_command_locked(target, action, args, command_type=command_type)
        serialized = _serialize_commands()
    if created or expired or purged:
        await broadcast({"type": "commands_update", "commands": serialized})
    if created:
        await _push_pending_commands_for_target(target)
    return cmd




async def _queue_proxmox_command(action: str, args: dict[str, Any] | None = None, command_type: str | None = None, target: str = "proxmox") -> dict[str, Any]:
    # In multi-agent setups, resolve the generic "proxmox" target to the currently
    # WS-connected (primary) agent so the command is delivered to exactly one agent.
    # Commands remain generic "proxmox" if no agent is currently connected via WS
    # (they'll be picked up by whichever agent polls next).
    resolved = target
    if target == "proxmox" and state.proxmox_ws_hostname:
        resolved = state.proxmox_ws_hostname
    return await _queue_command(resolved, action, args, command_type=command_type)




def _resolve_proxmox_vm_target(vmid: int | None) -> str:
    """Return the specific agent hostname that owns this vmid, or 'proxmox' if unknown."""
    if vmid is not None:
        owner = _proxmox_agent_vm_map.get(int(vmid))
        if owner and owner in approved_proxmox_agents:
            return owner
    return "proxmox"




async def _queue_unlock_template_command(command_type: str = "unlock_template") -> dict[str, Any]:
    return await _queue_proxmox_command("unlock_template", {}, command_type=command_type)




def _proxmox_update_branch() -> str:
    branch = str(settings.get("repo_branch", REPO_BRANCH) or REPO_BRANCH).strip()
    return branch or REPO_BRANCH




def _proxmox_update_args() -> dict[str, str]:
    branch = _proxmox_update_branch()
    return {
        "branch": branch,
        "repo_raw": f"{CLIENT_SIM_REPO_RAW.rstrip('/')}/{branch}",
    }




def _resolve_proxmox_update_target() -> str:
    hostname = str((proxmox_state.get("node") or {}).get("hostname") or "").strip()
    resolved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if resolved_hostname:
        return resolved_hostname
    if len(approved_proxmox_agents) == 1:
        return next(iter(approved_proxmox_agents))
    if not approved_proxmox_agents:
        raise HTTPException(status_code=409, detail="No approved Proxmox agent is available")
    raise HTTPException(status_code=409, detail="Unable to determine which Proxmox host should be updated")




async def _queue_proxmox_agent_update(target: str | None = None) -> dict[str, Any]:
    resolved_target = target or _resolve_proxmox_update_target()
    if resolved_target not in approved_proxmox_agents:
        raise HTTPException(status_code=404, detail="Proxmox agent not approved")
    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        cmd, created, _expired, _purged = _enqueue_command_locked(resolved_target, "update_agent", _proxmox_update_args())
        serialized = _serialize_commands()
    if not created:
        raise HTTPException(status_code=409, detail=f"An agent update is already queued for {resolved_target}")
    if expired or purged or created:
        await broadcast({"type": "commands_update", "commands": serialized})
    await _push_pending_commands_for_target(resolved_target)
    return cmd




async def _run_rolling_reclone(trigger_type: str) -> None:
    async with reclone_run_lock:
        if reclone_state["status"] == "running":
            return

        vms = _reclone_targets_for_run()
        reclone_state.update({
            "status": "running",
            "type": trigger_type,
            "total": len(vms),
            "completed": 0,
            "failed": 0,
            "current_vm": None,
            "log": [],
            "started_at": iso_utcnow(),
        })
        logger.info("Rolling reclone (%s): %d eligible VMs: %s", trigger_type, len(vms), [v.get("vmid") for v in vms])
        await _broadcast_reclone_state()
        await _broadcast_proxmox_state()

        concurrency = max(1, int(str(settings.get("reclone_concurrency", "1")).strip() or "1"))

        async def _reclone_one(vm: dict) -> None:
            vmid = int(vm.get("vmid"))
            name = vm.get("name") or f"VM {vmid}"
            _update_reclone_log(vmid, name, "queued")
            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()

            cmd = await _queue_proxmox_command("reclone_vm", _reclone_command_args(vm), command_type=trigger_type)
            deadline = time.time() + 1800
            last_status = "pending"
            poll_interval = 2.0
            while time.time() < deadline:
                # Commands remain in a small module-level list, so a linear scan keeps the
                # lookup simple here without a broader commands storage refactor.
                current = next((item for item in commands if item["id"] == cmd["id"]), None)
                if current is None:
                    break
                status = current.get("status", "pending")
                if status != last_status:
                    if status == "delivered":
                        _update_reclone_log(vmid, name, "in_progress")
                        await _broadcast_reclone_state()
                        await _broadcast_proxmox_state()
                        poll_interval = 5.0
                    last_status = status
                if status in {"completed", "failed", "expired"}:
                    final_status = "completed" if status == "completed" else "failed"
                    _update_reclone_log(vmid, name, final_status, str(current.get("message") or "").strip() or None)
                    if final_status == "completed":
                        _record_vm_watchdog_clone_completed(vmid, name)
                        await _async_save_vm_watchdog()
                        reclone_state["completed"] += 1
                    else:
                        reclone_state["failed"] += 1
                    await _broadcast_reclone_state()
                    await _broadcast_proxmox_state()
                    return
                await asyncio.sleep(poll_interval)
                if status == "pending":
                    poll_interval = min(poll_interval * 2, 10.0)
            logger.warning("Rolling reclone: VM %s (%s) timed out", vmid, name)
            _trace("reclone_timeout", vmid=vmid, name=name, cmd_id=cmd.get("id"), trigger=trigger_type)
            _update_reclone_log(vmid, name, "failed", "Timed out waiting for Proxmox agent ACK")
            reclone_state["failed"] += 1
            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()

        try:
            for i in range(0, len(vms), concurrency):
                batch = vms[i:i + concurrency]
                reclone_state["current_vm"] = int(batch[0].get("vmid")) if batch else None
                await _broadcast_reclone_state()
                await asyncio.gather(*(_reclone_one(vm) for vm in batch))

            # After recloning existing VMs, trigger provisioning for any
            # unassigned dongles (present USB device with no VM allocated).
            unassigned = _proxmox_unassigned_present_usb()
            if unassigned:
                logger.info(
                    "Rolling reclone: found %d unassigned dongle(s) — queuing provision_unassigned",
                    len(unassigned),
                )
                await _queue_proxmox_command("provision_unassigned", {}, command_type=trigger_type)

            reclone_state["status"] = "failed" if reclone_state["failed"] else "completed"
        except Exception as exc:
            logger.exception("Rolling reclone failed: %s", exc)
            reclone_state["status"] = "failed"
            reclone_state["failed"] += 1
        finally:
            reclone_state["current_vm"] = None
            # Capture a last_run summary before resetting so the UI can show
            # "Last run: X completed, Y failed" even after the tile goes idle.
            reclone_state["last_run"] = {
                "timestamp": iso_utcnow(),
                "completed": reclone_state["completed"],
                "failed": reclone_state["failed"],
                "type": trigger_type,
            }
            if reclone_state["status"] != "running":
                reclone_state["started_at"] = None

            # Once the run has reached a terminal state (completed / failed),
            # reset all counters and the log back to idle so the Fleet Reclone
            # tile disappears and shows 0 instead of lingering at the last
            # progress value.  The last_run summary we just captured above is
            # preserved so the "Last run" line in the UI still reflects what
            # happened.
            terminal_statuses = {"completed", "failed", "interrupted"}
            if reclone_state["status"] in terminal_statuses:
                saved_last_run = reclone_state["last_run"]
                saved_auto_log = reclone_state.get("auto_recovery_log") or []
                reclone_state.update({
                    "status": "idle",
                    "type": None,
                    "total": 0,
                    "completed": 0,
                    "failed": 0,
                    "current_vm": None,
                    "log": [],
                    "started_at": None,
                    "last_run": saved_last_run,
                    "auto_recovery_log": saved_auto_log,
                })
                logger.info(
                    "Rolling reclone: terminal state reached — reset to idle "
                    "(completed=%d, failed=%d)",
                    saved_last_run.get("completed", 0),
                    saved_last_run.get("failed", 0),
                )

            await _broadcast_reclone_state()
            await _broadcast_proxmox_state()






async def vm_watchdog_loop() -> None:
    await asyncio.sleep(VM_WATCHDOG_INTERVAL_SECS)
    while True:
        try:
            now = time.time()
            client_seen = {hostname: client.get("last_seen") for hostname, client in clients.items()}
            vm_names = {
                str(int(vm.get("vmid"))): str(vm.get("name") or "").strip()
                for vm in proxmox_state.get("vms") or []
                if vm.get("vmid") is not None
            }
            changed = False
            broadcast_needed = False
            for vmid_key, entry in list(vm_watchdog.items()):
                clone_completed_at = _parse_ts(entry.get("clone_completed_at"))
                if clone_completed_at is None:
                    vm_watchdog.pop(vmid_key, None)
                    changed = True
                    broadcast_needed = True
                    continue
                hostname = str(entry.get("hostname") or vm_names.get(vmid_key) or "").strip()
                if hostname and hostname != entry.get("hostname"):
                    entry["hostname"] = hostname
                    changed = True
                if _vm_has_checked_in(hostname, clone_completed_at, client_seen):
                    vm_watchdog.pop(vmid_key, None)
                    changed = True
                    broadcast_needed = True
                    continue
                if (now - clone_completed_at) <= VM_WATCHDOG_TIMEOUT_SECS:
                    continue
                vmid_int = int(vmid_key)
                if _has_pending_reclone(vmid_int):
                    continue
                vm = _find_proxmox_vm(vmid_int) or {"vmid": vmid_int}
                await _queue_proxmox_command("reclone_vm", _reclone_command_args(vm), command_type="watchdog")
                reclone_count = max(0, int(entry.get("reclone_count", 0) or 0)) + 1
                _record_vm_watchdog_clone_completed(
                    vmid_int,
                    hostname or vm.get("name"),
                    clone_completed_at=now,
                    reclone_count=reclone_count,
                )
                changed = True
                broadcast_needed = True
                logger.warning("VM watchdog queued reclone for VM %s (%s) after 24h without check-in", vmid_int, hostname or vm.get("name") or f"VM {vmid_int}")
                _trace("watchdog_reclone_queued", vmid=vmid_int, name=hostname or vm.get("name") or f"VM {vmid_int}", reclone_count=reclone_count)
            if changed:
                await _async_save_vm_watchdog()
            if broadcast_needed:
                await _broadcast_proxmox_state()
            _update_service_health("vm_watchdog", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("vm_watchdog", ok=False, error=str(exc))
            logger.exception("VM watchdog error: %s", exc)
        await asyncio.sleep(VM_WATCHDOG_INTERVAL_SECS)






async def expire_commands() -> None:
    """Expire stale active commands and purge terminal results after a short grace period."""
    await asyncio.sleep(15)
    while True:
        try:
            async with state_lock:
                expired, purged = _cleanup_commands_locked()
                serialized = _serialize_commands()
            if expired:
                logger.warning("Expired %d stale command(s) from the in-memory queue", expired)
                await broadcast({"type": "commands_update", "commands": serialized})
                await broadcast({"type": "notification", "level": "warning", "message": "One or more commands expired without being ACKed by the agent."})
            elif purged:
                await broadcast({"type": "commands_update", "commands": serialized})
            _update_service_health("command_expiry", ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("command_expiry", ok=False, error=str(exc))
            logger.exception("Command expiry error: %s", exc)
        await asyncio.sleep(15)




def _hub_isolated() -> bool:  # Compute whether hub-driven config pushes must pause so every safeguard check shares one helper.
    return bool(  # Evaluate the isolation rule in one expression so every caller uses the same last-sync timeout test.
        settings.get("hub_managed")  # Only hub-managed spokes should self-protect because self-managed spokes do not accept hub config pushes.
        and settings.get("relay_enabled") == "on"  # Only an enabled relay can be isolated because disabled hub connectivity should not trigger this safeguard.
        and relay_state.get("last_sync")  # A real last check-in is required so the timeout compares against known hub contact instead of guessing.
        and (time.time() - float(relay_state["last_sync"])) > int(settings.get("hub_isolation_timeout", 3600))  # Enter isolation after the configured no-contact window so stale hubs stop changing live config.
    )  # Share one boolean source of truth so every relay path evaluates isolation consistently.




def _revert_hub_managed_if_auth_failure(status_code: int | None, reason: str) -> bool:
    """Immediately revert hub_managed to False when the hub returns a definitive auth/not-found error.

    A 401 (wrong key), 403 (wrong PSK or forbidden), or 404 (tenant deleted) means the hub cannot
    recognise this spoke anymore — waiting for the isolation timeout would leave the spoke stuck in
    a read-only hub-managed state indefinitely.  Reverting immediately restores local control.

    Returns True if hub_managed was cleared so callers can log/broadcast the change.
    """
    if not settings.get("hub_managed"):
        return False
    if status_code not in (401, 403, 404):
        return False
    settings["hub_managed"] = False
    _save_settings()
    logger.warning(
        "hub_managed reverted to local control — hub auth failure (HTTP %s): %s",
        status_code,
        reason,
    )
    _relay_diag_append("hub_managed_reverted", status_code=status_code, reason=reason)
    return True




def _hub_config_isolation_result(task_type: str) -> dict[str, Any]:  # Build a consistent skip result so every blocked hub config push is acknowledged the same way.
    return {  # Return a structured ack payload so the hub can tell a deliberate isolation skip from a transport failure.
        "success": False,  # Mark the ack as non-success so the hub can distinguish a safeguard skip from an applied config change.
        "skipped": True,  # Flag the result as skipped so operators can see the spoke deliberately ignored the push.
        "reason": "hub_isolated",  # Identify the exact safeguard reason so downstream tooling can explain the skip clearly.
        "task_type": task_type,  # Echo the original command type so the hub knows which config action was paused.
        "detail": "Hub isolated — config pushes paused until contact resumes",  # Explain the safeguard outcome so the hub UI does not look like a silent failure.
        "timestamp": datetime.now(timezone.utc).isoformat(),  # Stamp the skip result so operators can correlate it with outage timing.
    }  # Return one reusable skip payload so every blocked hub config ack stays consistent.




async def hub_isolation_monitor() -> None:  # Poll isolation state so the UI updates even when no relay message arrives to trigger a broadcast.
    last_isolated = _hub_isolated()  # Capture the initial state so the monitor only broadcasts when isolation actually changes.
    while True:  # Keep watching in the background so timeout expiry and recovery both reach the UI automatically.
        await asyncio.sleep(60)  # Re-check once per minute so the safeguard flips even when no hub message arrives to trigger a relay broadcast.
        current_isolated = _hub_isolated()  # Recompute isolation from last_sync so timeout expiry and recovery both use the live source of truth.
        if current_isolated != last_isolated:  # Only broadcast on state changes so the monitor updates the UI without creating noisy relay traffic.
            last_isolated = current_isolated  # Remember the new state so the next loop only announces another real transition.
            await _broadcast_relay_state()  # Push the changed isolation state to browsers immediately so banners and status text stay accurate.




async def _broadcast_update_state() -> None:
    _save_update_state()
    await broadcast({"type": "version_status", **dict(update_state)})




async def _hub_self_register(server_url: str) -> None:
    """POST to hub /api/spokes/register with full config payload.
    Stores the returned spoke_id. If already approved, also stores api_key and tenant_id."""
    hostname = socket.gethostname()
    spoke_name = settings.get("relay_spoke_name", "").strip() or hostname
    spoke_id = _ensure_relay_spoke_id()
    payload = {
        "spoke_id": spoke_id,
        "hostname": hostname,
        "label": hostname,
        "spoke_name": spoke_name,
        "tenant_id_hint": (settings.get("relay_tenant_id") or settings.get("relay_tenant_hint") or "").strip(),
        "onboarding_psk": settings.get("relay_onboarding_psk", "").strip(),
        "api_key": settings.get("relay_api_key", "").strip(),
        "config": _build_registration_config(),
    }
    _relay_diag_append("register_attempt", url=f"{server_url}/api/spokes/register",
                       hostname=hostname, spoke_name=spoke_name, spoke_id=spoke_id)
    hub_base = _relay_hub_base_url(server_url, settings.get("relay_tenant_id", ""))
    try:
        async with httpx.AsyncClient(timeout=15, verify=_hub_tls_verify()) as hc:
            resp = await hc.post(f"{hub_base}/api/spokes/register", json=payload)
            if resp.status_code == 409:
                data = resp.json()
                conflict = data.get("conflict", "name_in_use")
                msg = data.get("message", f"Spoke name '{spoke_name}' is already in use on the hub. Choose a different name.")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                relay_state.update({
                    "connected": False,
                    "registration_status": "name_conflict",
                    "error": f"{ts} — {msg}",
                })
                state.relay_registration_refresh_needed = False
                _save_relay_state()
                _relay_diag_append("register_409", conflict=conflict, message=msg)
                logger.warning("Hub registration name conflict: %s", msg)
                return
            resp.raise_for_status()
            data = resp.json()
        spoke_id = str(data.get("spoke_id", "")).strip()
        status = data.get("status", "pending")
        if spoke_id and not _relay_spoke_id_needs_rotation(spoke_id):
            settings["relay_spoke_id"] = spoke_id
        else:
            spoke_id = _ensure_relay_spoke_id()
        if status == "approved":
            tenant_id = data.get("tenant_id", "")
            settings["relay_api_key"] = data.get("api_key", "")
            settings["relay_tenant_id"] = tenant_id
            settings["relay_tenant_hint"] = tenant_id
            relay_state["registration_status"] = "approved"
            relay_state["error"] = ""
            _relay_diag_append("register_ok", status="approved", spoke_id=spoke_id,
                               tenant_id=data.get("tenant_id"))
            logger.info("Hub registration: approved immediately spoke_id=%s tenant_id=%s", spoke_id, data.get("tenant_id"))
        else:
            tenant_hint = str(data.get("tenant_hint", "")).strip()
            settings["relay_api_key"] = ""
            settings["relay_tenant_id"] = ""
            if tenant_hint:
                settings["relay_tenant_hint"] = tenant_hint
            relay_state["registration_status"] = "pending"
            relay_state["error"] = ""
            _relay_diag_append("register_ok", status="pending", spoke_id=spoke_id)
            logger.info("Hub registration submitted: spoke_id=%s status=pending", spoke_id)
        state.relay_registration_refresh_needed = False
        _save_relay_state()
        _save_settings()
    except Exception as exc:
        state.relay_registration_refresh_needed = True
        _relay_diag_append("register_error", error=str(exc))
        logger.warning("Hub self-register failed: %s", exc)
        relay_state.update({"connected": False, "error": f"Registration failed: {exc}"})
        _save_relay_state()




async def _hub_check_approval(server_url: str, spoke_id: str) -> None:
    """Re-POST registration to check if spoke has been approved.
    Hub returns 'approved' with api_key and tenant_id once superadmin has approved."""
    hostname = socket.gethostname()
    spoke_name = settings.get("relay_spoke_name", "").strip() or hostname
    tenant_hint = (settings.get("relay_tenant_id") or settings.get("relay_tenant_hint") or "").strip()
    existing_api_key = settings.get("relay_api_key", "").strip()
    existing_tenant_id = settings.get("relay_tenant_id", "").strip()
    had_approval = bool(existing_api_key and existing_tenant_id)
    _relay_diag_append("check_approval", spoke_id=spoke_id)
    hub_base = _relay_hub_base_url(server_url, settings.get("relay_tenant_id", ""))
    try:
        async with httpx.AsyncClient(timeout=10, verify=_hub_tls_verify()) as hc:
            resp = await hc.post(f"{hub_base}/api/spokes/register", json={
                "spoke_id": spoke_id,
                "hostname": hostname,
                "label": hostname,
                "spoke_name": spoke_name,
                "tenant_id_hint": tenant_hint,
                "onboarding_psk": settings.get("relay_onboarding_psk", "").strip(),
                "api_key": existing_api_key,
                "config": _build_registration_config(),
            })
            resp.raise_for_status()
            data = resp.json()
        status = data.get("status", "pending")
        updated = False
        returned_spoke_id = str(data.get("spoke_id", "")).strip()
        if returned_spoke_id and not _relay_spoke_id_needs_rotation(returned_spoke_id) and returned_spoke_id != settings.get("relay_spoke_id", ""):
            settings["relay_spoke_id"] = returned_spoke_id
            spoke_id = returned_spoke_id
            updated = True
        if status == "approved":
            tenant_id = data.get("tenant_id", "")
            settings["relay_api_key"] = data.get("api_key", "")
            settings["relay_tenant_id"] = tenant_id
            settings["relay_tenant_hint"] = tenant_id
            relay_state["registration_status"] = "approved"
            relay_state["error"] = ""
            updated = True
            _relay_diag_append("approval_received", spoke_id=spoke_id,
                               tenant_id=data.get("tenant_id"))
            logger.info("Hub approval received: spoke_id=%s tenant_id=%s", spoke_id, data.get("tenant_id"))
        else:
            tenant_hint = str(data.get("tenant_hint", "")).strip()
            if tenant_hint and tenant_hint != settings.get("relay_tenant_hint", ""):
                settings["relay_tenant_hint"] = tenant_hint
                updated = True
            if had_approval:
                relay_state["registration_status"] = "approved"
                relay_state["error"] = "Hub registration check returned pending; keeping existing approval until credentials are explicitly rejected."
                _relay_diag_append("pending_ignored", spoke_id=spoke_id, tenant_hint=tenant_hint)
                logger.warning("Hub registration check returned pending for approved spoke %s; keeping stored approval", spoke_id)
            else:
                relay_state["registration_status"] = "pending"
                relay_state["error"] = ""
                _relay_diag_append("still_pending", spoke_id=spoke_id)
                logger.info("Hub registration still pending: spoke_id=%s", spoke_id)
        state.relay_registration_refresh_needed = False
        _save_relay_state()
        if updated:
            _save_settings()
    except Exception as exc:
        state.relay_registration_refresh_needed = True
        _relay_diag_append("check_approval_error", error=str(exc))
        logger.warning("Hub approval check failed: %s", exc)




def _relay_hub_base_url(server_url: str, tenant_id: str) -> str:
    url = server_url.rstrip("/")
    if tenant_id:
        url = re.sub(rf"/api/{re.escape(tenant_id)}$", "", url)
    return url.rstrip("/")




def _hub_tls_verify() -> bool:
    return _normalize_relay_enabled(settings.get("hub_tls_verify", "off")) == "on"




def _hub_reseed_block_result() -> dict[str, str]:
    return {
        "error": "reseed_in_progress",
        "message": "Reseed in progress — provisioning paused. Try again shortly.",
    }




async def _forward_hub_passthrough_to_proxmox(cmd_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if state.proxmox_ws_connection is None:
        raise RuntimeError("Proxmox agent is not connected")
    if cmd_type == "backup":
        logger.info(f"Forwarding backup command to proxmox agent: vm_ids={payload.get('vm_ids')}")
    elif cmd_type == "reseed":
        logger.info(f"Forwarding reseed command to proxmox agent: vm_ids={payload.get('vm_ids')}")
    else:
        logger.info(f"Forwarding {cmd_type} command to proxmox agent: action={payload.get('action')}")
    if cmd_type == "command":
        await state.proxmox_ws_connection.send_json({"type": cmd_type, **payload})
    else:
        await state.proxmox_ws_connection.send_json({"type": cmd_type, "payload": payload})
    return {
        "success": True,
        "task_type": cmd_type,
        "detail": f"Forwarded {cmd_type} command to proxmox agent",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }




def _hub_targets_proxmox_agent(target: str) -> bool:
    normalized = _normalize_proxmox_hostname(target)
    if not normalized:
        return False
    if normalized == "proxmox":
        return True
    if state.proxmox_ws_hostname and _proxmox_hostnames_match(normalized, state.proxmox_ws_hostname):
        return True
    return _resolve_proxmox_agent_hostname(normalized, approved_proxmox_agents) is not None




def _hub_command_blocked_by_reseed(cmd_type: str, target: str, action: str) -> bool:
    if not state._proxmox_reseed_in_progress:
        return False
    if cmd_type == "proxmox_reclone_all":
        return True
    return _hub_targets_proxmox_agent(target) and action in {"reclone_vm", "provision_unassigned"}




async def _relay_proxmox_progress_to_hub(message: dict[str, Any]) -> None:
    if state._relay_ws_send_json is None:
        return
    outbound = dict(message)
    payload = outbound.get("payload") if isinstance(outbound.get("payload"), dict) else None
    if payload is not None:
        payload = dict(payload)
        if "spoke_id" not in payload and state._relay_ws_spoke_id:
            payload["spoke_id"] = state._relay_ws_spoke_id
        outbound["payload"] = payload
    elif "spoke_id" not in outbound and state._relay_ws_spoke_id:
        outbound["spoke_id"] = state._relay_ws_spoke_id
    await state._relay_ws_send_json(outbound)




async def _relay_vnc_to_hub(message: dict[str, Any]) -> None:
    """Forward a VNC frame/control message back to the hub."""
    if state._relay_ws_send_json is None:
        return
    outbound = dict(message)
    if "spoke_id" not in outbound and state._relay_ws_spoke_id:
        outbound["spoke_id"] = state._relay_ws_spoke_id
    await state._relay_ws_send_json(outbound)




async def _handle_vnc_proxy_request(message: dict[str, Any]) -> None:
    """Open a WebSocket to Proxmox vncwebsocket and relay frames to/from hub."""
    request_id = str(message.get("request_id") or "").strip()
    vmid = int(message.get("vmid") or 0)
    vmtype = str(message.get("vmtype") or "qemu").strip().lower()

    if not request_id or not vmid:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Missing request_id or vmid"})
        return

    proxmox_host = str(_proxmox_agent_vm_map.get(vmid) or state.proxmox_ws_hostname or "").strip()
    api_token = _get_proxmox_token_for_host(proxmox_host)

    if not proxmox_host:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Proxmox host unknown — no agent connected"})
        return
    if not api_token:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "Proxmox API token not configured on spoke"})
        return

    # Ask Proxmox to create a VNC ticket via REST
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    node = proxmox_host.split(".")[0]
    vncproxy_url = f"https://{proxmox_host}:8006/api2/json/nodes/{node}/{vmtype}/{vmid}/vncproxy"
    headers = {"Authorization": f"PVEAPIToken={api_token}"}

    try:
        if httpx is None:
            raise RuntimeError("httpx not installed")
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(vncproxy_url, headers=headers, json={"websocket": 1}, timeout=10)
        if resp.status_code != 200:
            await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": f"Proxmox vncproxy returned {resp.status_code}: {resp.text[:200]}"})
            return
        body = resp.json()
        ticket = body["data"]["ticket"]
        port = body["data"]["port"]
    except Exception as exc:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": f"Proxmox vncproxy call failed: {exc}"})
        return

    # Register an inbound queue so browser→proxmox frames can be forwarded
    inbound_queue: asyncio.Queue = asyncio.Queue()
    _vnc_sessions[request_id] = inbound_queue

    import urllib.parse as _urlparse
    params = _urlparse.urlencode({"port": port, "vncticket": ticket})
    ws_path = f"/api2/json/nodes/{node}/{vmtype}/{vmid}/vncwebsocket?{params}"
    ws_url = f"wss://{proxmox_host}:8006{ws_path}"

    if websockets is None:
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": "websockets library not installed"})
        _vnc_sessions.pop(request_id, None)
        return

    try:
        connect_kwargs: dict[str, Any] = {
            "ssl": ssl_ctx,
            "open_timeout": 20,
            "max_size": None,
        }
        # Use correct keyword for this version of websockets
        import inspect as _inspect
        hdr_key = "additional_headers" if "additional_headers" in _inspect.signature(websockets.connect).parameters else "extra_headers"
        connect_kwargs[hdr_key] = headers

        await _relay_vnc_to_hub({"type": "vnc_proxy_response", "request_id": request_id})

        async with websockets.connect(ws_url, **connect_kwargs) as px_ws:

            async def proxmox_to_hub() -> None:
                async for raw in px_ws:
                    data = raw if isinstance(raw, bytes) else raw.encode()
                    await _relay_vnc_to_hub({
                        "type": "vnc_frame_to_browser",
                        "request_id": request_id,
                        "data": __import__("base64").b64encode(data).decode(),
                    })

            async def hub_to_proxmox() -> None:
                while True:
                    msg = await inbound_queue.get()
                    if msg is None:
                        break
                    raw = __import__("base64").b64decode(msg.get("data", ""))
                    await px_ws.send(raw)

            t1 = asyncio.create_task(proxmox_to_hub())
            t2 = asyncio.create_task(hub_to_proxmox())
            try:
                done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)
            finally:
                pass
    except Exception as exc:
        logger.warning("VNC relay error for request %s: %s", request_id, exc)
        await _relay_vnc_to_hub({"type": "vnc_proxy_error", "request_id": request_id, "error": str(exc)})
    finally:
        _vnc_sessions.pop(request_id, None)
        await _relay_vnc_to_hub({"type": "vnc_disconnect", "request_id": request_id})




async def _handle_provision_proxmox_token(message: dict[str, Any]) -> None:
    """Auto-create a Proxmox API token via pvesh and report it back to the hub."""
    request_id = str(message.get("request_id") or "").strip()

    async def _send_error(error: str) -> None:
        await _relay_vnc_to_hub({"type": "proxmox_token_provision_error", "request_id": request_id, "error": error})

    # pvesh may not be in the systemd service PATH — check all common Proxmox locations.
    # Use os.path.isfile only (not os.access X_OK) since the service may run as a user
    # that lacks execute permission on the stat but can still exec via the kernel.
    pvesh_candidates = [
        shutil.which("pvesh"),
        "/usr/bin/pvesh",
        "/usr/sbin/pvesh",
        "/usr/local/bin/pvesh",
        "/usr/share/pve-manager/bin/pvesh",
        "/opt/proxmox/bin/pvesh",
    ]
    pvesh_path = next((c for c in pvesh_candidates if c and os.path.isfile(c)), None)
    if not pvesh_path:
        # Last resort: try running pvesh directly and let the OS sort out the path
        try:
            probe = await asyncio.create_subprocess_exec(
                "pvesh", "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(probe.wait(), timeout=5.0)
            if probe.returncode == 0:
                pvesh_path = "pvesh"
        except Exception:
            pass
    if not pvesh_path:
        # pvesh not available locally — try relaying to the Proxmox agent if connected.
        if state.proxmox_ws_connection is not None:
            logger.info("provision_proxmox_token: pvesh not found locally, relaying to proxmox agent")
            q: asyncio.Queue = asyncio.Queue(maxsize=1)
            _proxmox_token_provision_queues[request_id] = q
            try:
                await state.proxmox_ws_connection.send_json({
                    "type": "create_proxmox_token",
                    "request_id": request_id,
                })
                result = await asyncio.wait_for(q.get(), timeout=30.0)
                if result.get("ok"):
                    token = str(result.get("token") or "").strip()
                    settings["proxmox_api_token"] = token
                    _persisted["proxmox_api_token"] = token
                    _save_settings()
                    logger.info("Proxmox API token provisioned via agent: relaying to hub")
                    await _relay_vnc_to_hub({
                        "type": "proxmox_token_provisioned",
                        "request_id": request_id,
                        "token": token,
                    })
                else:
                    await _send_error(str(result.get("error") or "Agent failed to provision token"))
            except asyncio.TimeoutError:
                await _send_error("Proxmox agent did not respond to token creation request within 30 seconds")
            except Exception as exc:
                await _send_error(f"Failed to relay token request to agent: {exc}")
            finally:
                _proxmox_token_provision_queues.pop(request_id, None)
        else:
            await _send_error(
                "pvesh not found locally and no Proxmox agent is connected. "
                "Ensure the proxmox-agent.sh service is running on the Proxmox host."
            )
        return

    TOKEN_ID = "cs-hub"
    USER = "root@pam"
    logger.info("provision_proxmox_token: using pvesh at %s", pvesh_path)

    try:
        # Remove any existing token with this ID so we always get a fresh secret
        del_proc = await asyncio.create_subprocess_exec(
            pvesh_path, "delete", f"/access/users/{USER}/token/{TOKEN_ID}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(del_proc.wait(), timeout=10.0)
    except Exception:
        pass  # Token may not exist yet — ignore

    try:
        proc = await asyncio.create_subprocess_exec(
            pvesh_path, "create", f"/access/users/{USER}/token/{TOKEN_ID}",
            "--privsep", "0",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode != 0:
            await _send_error(f"pvesh create failed: {stderr.decode().strip()[:300]}")
            return

        data = json.loads(stdout.decode().strip())
        secret = str(data.get("value") or "").strip()
        if not secret:
            await _send_error("pvesh returned no token value in response")
            return

        full_token = f"{USER}!{TOKEN_ID}={secret}"
        settings["proxmox_api_token"] = full_token
        _persisted["proxmox_api_token"] = full_token
        _save_settings()
        logger.info("Proxmox API token auto-provisioned: %s!%s", USER, TOKEN_ID)

        await _relay_vnc_to_hub({"type": "proxmox_token_provisioned", "request_id": request_id, "token": full_token})

    except asyncio.TimeoutError:
        await _send_error("pvesh timed out after 15 seconds")
    except json.JSONDecodeError as exc:
        await _send_error(f"Could not parse pvesh output: {exc}")
    except Exception as exc:
        await _send_error(f"Unexpected error: {exc}")




async def _handle_command_trace_request(message: dict[str, Any]) -> None:
    """Send the command relay trace buffer back to the hub."""
    request_id = str(message.get("request_id") or "").strip()
    if not request_id or state._relay_ws_send_json is None:
        return
    async with state_lock:
        cmds_snapshot = list(_serialize_commands())
    out: dict[str, Any] = {
        "type": "command_trace_response",
        "request_id": request_id,
        "agent_connected": state.proxmox_ws_connection is not None,
        "agent_hostname": state.proxmox_ws_hostname,
        "command_queue": cmds_snapshot,
        "trace": list(reversed(_command_trace)),
    }
    if state._relay_ws_spoke_id:
        out["spoke_id"] = state._relay_ws_spoke_id
    await state._relay_ws_send_json(out)




def _push_to_github(files_changed: list[str], commit_message: str) -> bool:
    token = settings.get("github_token", "").strip()
    if not token:
        raise ValueError("GitHub token not configured")

    if not (REPO_DIR / ".git").exists():
        raise RuntimeError(f"{REPO_DIR} exists but is not a git repository")

    # Ensure git identity is set (required for commit)
    try:
        _git("config", "user.name")
    except RuntimeError:
        _git("config", "user.name", "Client Simulator")
    try:
        _git("config", "user.email")
    except RuntimeError:
        _git("config", "user.email", "client-sim@localhost")

    askpass_script = BASE_DIR / f".git-askpass-{uuid.uuid4().hex}.sh"
    askpass_script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *Password*) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    askpass_script.chmod(0o700)
    push_env = {
        "GIT_ASKPASS": str(askpass_script),
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_TOKEN": token,
    }

    _git("remote", "set-url", "origin", REPO_URL)
    try:
        _git("add", *files_changed)
        # Check if there is anything staged
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_DIR,
        )
        if status.returncode == 0:
            return False  # nothing staged
        _git("commit", "-m", commit_message)
        _git("push", env=push_env)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            askpass_script.unlink()
        _git("remote", "set-url", "origin", REPO_URL)




def _update_ini_section(filepath: Path, section: str, updates: dict[str, str]) -> None:
    text = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    normalized_updates = {str(key).strip(): str(value) for key, value in updates.items() if str(key).strip()}

    updated_lines: list[str] = []
    found_keys: set[str] = set()
    section_found = False
    in_target_section = False

    def append_missing_keys() -> None:
        for key, value in normalized_updates.items():
            if key not in found_keys:
                updated_lines.append(f"{key}={value}")

    for line in lines:
        match = re.match(r"^\s*\[(?P<section>[^\]]+)\]\s*$", line)
        if match:
            if in_target_section:
                append_missing_keys()
            current_section = match.group("section")
            in_target_section = current_section == section
            section_found = section_found or in_target_section
            updated_lines.append(line)
            continue

        if in_target_section:
            key_match = re.match(r"^(?P<indent>\s*)(?P<key>[^=\s#;][^=]*?)\s*=.*$", line)
            if key_match:
                key = key_match.group("key").strip()
                if key in normalized_updates:
                    updated_lines.append(f"{key_match.group('indent')}{key}={normalized_updates[key]}")
                    found_keys.add(key)
                    continue

        updated_lines.append(line)

    if in_target_section:
        append_missing_keys()

    if not section_found:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(f"[{section}]")
        append_missing_keys()

    output = newline.join(updated_lines)
    if updated_lines and (text.endswith("\n") or not text):
        output += newline
    filepath.write_text(output, encoding="utf-8")




async def _run_hub_repo_sync() -> dict[str, Any]:
    repo_version = await _sync_repo_now()
    output: dict[str, Any] = {"repo_version": repo_version}
    detail = f"Client-Sim repo synced{f' ({repo_version})' if repo_version else ''}"

    if approved_proxmox_agents:
        try:
            cmd = await _queue_proxmox_agent_update()
            output.update({
                "agent_command_id": cmd["id"],
                "agent_target": cmd["target"],
                "agent_branch": cmd.get("args", {}).get("branch"),
            })
            detail += f"; queued Proxmox agent update for {cmd['target']}"
        except HTTPException as exc:
            detail_msg = str(exc.detail or exc)
            if exc.status_code == 409 and "already queued" in detail_msg.lower():
                detail += f"; {detail_msg}"
            else:
                raise RuntimeError(detail_msg) from exc
    else:
        detail += "; no approved Proxmox agent available"

    return {
        "success": True,
        "task_type": "repo_sync",
        "detail": detail,
        "output": output,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }




async def check_for_update() -> None:
    """Background task: check for a new installer version every 24 hours with
    a random 2-hour jitter. Auto-applies the update when a new version is
    detected and the spoke is idle (no active reclone or reseed)."""
    import random
    # Spread initial check across first 2 hours to avoid update stampedes
    initial_jitter = random.uniform(0, 7200)
    logger.info("Update checker: first check in %.0f seconds", initial_jitter)
    await asyncio.sleep(initial_jitter)
    while True:
        try:
            available = await asyncio.to_thread(_get_repo_version)
            import datetime
            update_state["available_version"] = available
            update_state["last_checked"] = datetime.datetime.now().isoformat(timespec="seconds")
            update_state["update_available"] = (
                available is not None
                and available != update_state["current_version"]
            )
            logger.info(
                "Version check: installed=%s repo=%s update_available=%s",
                update_state["current_version"],
                available,
                update_state["update_available"],
            )
            _update_service_health("update_checker", ok=True)
            await _broadcast_update_state()

            # Auto-apply if update available and spoke is idle
            if (
                update_state["update_available"]
                and not update_state["update_in_progress"]
                and reclone_state.get("status") != "running"
                and not state._proxmox_reseed_in_progress
            ):
                logger.info(
                    "Auto-update: new version %s available and spoke is idle — applying",
                    available,
                )
                asyncio.create_task(_run_self_update())
            elif update_state["update_available"]:
                logger.info(
                    "Auto-update: new version %s available but spoke is busy — will retry next cycle",
                    available,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_service_health("update_checker", ok=False, error=str(exc))
            logger.exception("Update checker error: %s", exc)
        # 24 hours + up to 2-hour jitter to prevent all spokes checking simultaneously
        await asyncio.sleep(UPDATE_CHECK_INTERVAL + random.uniform(0, 7200))






async def _run_update_all() -> None:
    """Queue the shared Proxmox update command, wait for its ACK, then self-update the WebUI."""

    approved = list(approved_proxmox_agents.keys())
    agent_cmd_ids: list[str] = []

    # ── Phase 1: Agent update ────────────────────────────────────────────────
    try:
        async with state_lock:
            update_args = _proxmox_update_args()
            for hostname in approved:
                cmd, _created, _expired, _purged = _enqueue_command_locked(hostname, "update_agent", dict(update_args))
                agent_cmd_ids.append(cmd["id"])

        state.update_all_state.update({
            "running": True,
            "phase": "agents" if agent_cmd_ids else "webui",
            "total_agents": len(agent_cmd_ids),
            "completed_agents": 0,
            "failed_agents": 0,
            "agent_cmds": agent_cmd_ids,
            "started_at": time.time(),
            "error": None,
        })
        await broadcast({"type": "update_all_progress", **state.update_all_state})
        await broadcast({"type": "commands_update", "commands": _serialize_commands()})
        await _push_pending_commands_for_targets(approved)

        if agent_cmd_ids:
            deadline = time.time() + 300
            while time.time() < deadline:
                await asyncio.sleep(5)
                async with state_lock:
                    command_statuses = {
                        c["id"]: c["status"]
                        for c in commands
                        if c["id"] in agent_cmd_ids
                    }
                done = sum(1 for status in command_statuses.values() if status in ("completed", "failed"))
                failed = sum(1 for status in command_statuses.values() if status == "failed")
                state.update_all_state["completed_agents"] = done
                state.update_all_state["failed_agents"] = failed
                await broadcast({"type": "update_all_progress", **state.update_all_state})
                if done >= len(agent_cmd_ids):
                    break
            else:
                logger.warning(
                    "Update All: agent ACK timed out after 300s — proceeding to WebUI update anyway"
                )

        if len(approved) == 0:
            logger.info("Update All: no approved agents, proceeding directly to WebUI update")
        else:
            logger.info(
                "Update All: agents done (%d/%d failed), proceeding to WebUI update",
                state.update_all_state["completed_agents"],
                state.update_all_state["failed_agents"],
            )
    except Exception as exc:
        logger.error("Update All: agent phase error (continuing to WebUI update): %s", exc)
        state.update_all_state["error"] = str(exc)

    # ── Phase 2: WebUI self-update ───────────────────────────────────────────
    try:
        state.update_all_state["phase"] = "webui"
        state.update_all_state["error"] = None
        await broadcast({"type": "update_all_progress", **state.update_all_state})

        # Sync the local repo cache before running the installer so it gets
        # the freshest content from GitHub (same as the /api/self-update path).
        try:
            async with _git_lock:
                await asyncio.to_thread(sync_repo_once)
            logger.info("Update All: repo synced before installer")
        except Exception as sync_exc:
            logger.warning("Update All: repo sync failed (%s) — installer will retry git fetch", sync_exc)

        await _run_self_update()
        if update_state.get("update_error"):
            state.update_all_state["phase"] = "failed"
            state.update_all_state["error"] = str(update_state["update_error"])
            logger.error("Update All: WebUI self-update failed: %s", update_state["update_error"])
        else:
            state.update_all_state["phase"] = "done"
    except Exception as exc:
        state.update_all_state["phase"] = "failed"
        state.update_all_state["error"] = str(exc)
        logger.error("Update All: WebUI phase error: %s", exc)
    finally:
        state.update_all_state["running"] = False
        await broadcast({"type": "update_all_progress", **state.update_all_state})




async def _run_self_update() -> None:
    """Re-run the installer from the synced repo. Systemd will restart the service."""
    if update_state["update_in_progress"]:
        return
    # Wait for any pending/delivered Proxmox agent commands (e.g. update_agent) to be
    # acked before restarting. Without this delay, the spoke restarts and loses the
    # in-memory command state before the agent has a chance to ack the command.
    _agent_update_wait_secs = 180
    _agent_update_deadline = time.time() + _agent_update_wait_secs
    while time.time() < _agent_update_deadline:
        async with state_lock:
            active = [c for c in commands if c.get("status") in ("pending", "delivered")]
        if not active:
            break
        logger.info(
            "Self-update: waiting for %d Proxmox agent command(s) to complete before restarting (%.0fs remaining)...",
            len(active),
            _agent_update_deadline - time.time(),
        )
        await asyncio.sleep(5)
    if not _INSTALLER_PATH.exists():
        msg = f"Self-update: installer not found at {_INSTALLER_PATH}"
        logger.error(msg)
        update_state["update_error"] = msg
        await _broadcast_update_state()
        return
    update_state["update_in_progress"] = True
    update_state["update_log"] = []
    update_state["update_error"] = None
    await _broadcast_update_state()
    try:
        import shlex as _shlex, os as _os
        # Use create_subprocess_shell so /bin/sh resolves bash via its own PATH.
        # This is more robust than exec when systemd strips the PATH env.
        full_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        installer = _shlex.quote(str(_INSTALLER_PATH))
        # Pass --branch and --port so the bootstrap step can curl the right branch.
        _branch = _shlex.quote(os.environ.get("REPO_BRANCH", "main"))
        _port   = _shlex.quote(os.environ.get("PORT", "8000"))
        _base     = f'/bin/bash {installer} --branch {_branch} --port {_port}'
        shell_cmd = _base if _os.geteuid() == 0 else f'sudo -n /bin/bash {installer} --branch {_branch} --port {_port}'
        logger.info("Self-update: shell_cmd=%s", shell_cmd)
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PATH": full_path},
            start_new_session=True,  # detach from server's process group so SIGTERM on restart doesn't kill installer
        )
        assert proc.stdout is not None
        _ansi_re = re.compile(r'\x1b(?:\[[0-9;]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[^[\]])')
        async for raw in proc.stdout:
            line = _ansi_re.sub('', raw.decode(errors="replace")).rstrip()
            update_state["update_log"].append(line)
            logger.info("self-update: %s", line)
            await _broadcast_update_state()
        await proc.wait()
        # -15 (SIGTERM) is expected when the installer schedules a deferred
        # `systemctl restart` and asyncio cleans up the subprocess transport
        # when the server is stopped.  If the restart step already ran, treat
        # it as success rather than surfacing a misleading error.
        restart_triggered = any(
            "Restarting client-sim-dashboard" in l for l in update_state["update_log"]
        )
        if proc.returncode != 0 and not (proc.returncode == -15 and restart_triggered):
            logger.error("Self-update installer exited with code %s", proc.returncode)
            update_state["update_in_progress"] = False
            update_state["update_error"] = f"Installer exited with code {proc.returncode} — check logs"
            await _broadcast_update_state()
        else:
            logger.info("Self-update installer completed successfully (rc=%s)", proc.returncode)
            update_state["update_in_progress"] = False
            update_state["update_error"] = None
            await _broadcast_update_state()
    except Exception as exc:
        logger.exception("Self-update failed")
        update_state["update_in_progress"] = False
        update_state["update_error"] = str(exc)
        update_state["update_log"].append(f"ERROR: {exc}")
        await _broadcast_update_state()




async def _authorize_proxmox_agent(hostname: str, api_key: str, client_ip: str, now: float) -> tuple[str | None, JSONResponse | None]:
    approved_hostname = _resolve_proxmox_agent_hostname(hostname, approved_proxmox_agents)
    if approved_hostname is None:
        _upsert_pending_proxmox_agent(hostname, client_ip, now)
        await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
        if api_key:
            return None, JSONResponse({"error": "agent not approved"}, status_code=401)
        return None, JSONResponse({"pending": True}, status_code=202)

    if api_key != approved_proxmox_agents[approved_hostname]:
        return None, JSONResponse({"error": "invalid key"}, status_code=401)

    pending_hostname = _resolve_proxmox_agent_hostname(hostname, pending_proxmox_agents)
    if pending_hostname is not None:
        pending_proxmox_agents.pop(pending_hostname, None)
        await broadcast({"type": "proxmox_pending_update", "pending": _pending_proxmox_payload()})
    return approved_hostname, None




async def _apply_proxmox_telemetry_state(body: dict[str, Any], hostname: str, now: float) -> dict[str, bool]:
    async with state_lock:
        client_seen = {client_hostname: client.get("last_seen") for client_hostname, client in clients.items()}

    enriched_vms: list[dict[str, Any]] = []
    configured_template_ids: set[str] = {
        str(settings.get("vm_image_1_template_id", "100")).strip(),
        str(settings.get("vm_image_2_template_id", "200")).strip(),
    } - {""}
    for vm in body.get("vms", []):
        enriched = dict(vm)
        client_last_seen = client_seen.get(str(enriched.get("name", "")))
        if isinstance(client_last_seen, datetime):
            enriched["last_seen"] = client_last_seen.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        # Mark as template if agent flagged it OR if vmid matches a configured
        # template ID — OR if the VM's NAME matches a configured template name
        # (the clone-source field accepts a vmid OR a name; a name-configured
        # template must still be marked here so telemetry/UI stay consistent).
        if (enriched.get("is_template")
                or str(enriched.get("vmid", "")).strip() in configured_template_ids
                or str(enriched.get("name", "")).strip() in configured_template_ids):
            enriched["is_template"] = True
        enriched_vms.append(enriched)

    # Filter unknown_usb against currently certified and ignored vidpids so the device
    # disappears from the UI immediately after a certify/ignore action, even before the
    # Proxmox agent picks up the updated config on its next poll.
    certified_vidpids: set[str] = {
        str(item.get("vidpid", "")).strip().lower()
        for item in _parse_json_list(settings.get("usb_vidpids", "[]"))
        if isinstance(item, dict) and item.get("vidpid")
    }
    ignored_vidpids: set[str] = {
        str(v).strip().lower()
        for v in _parse_json_list(settings.get("usb_ignored_vidpids", "[]"))
        if str(v).strip()
    }
    exclude_vidpids = certified_vidpids | ignored_vidpids
    raw_unknown = body.get("unknown_usb", [])
    proxmox_state["unknown_usb"] = [
        d for d in raw_unknown
        if str(d.get("vidpid", "")).strip()  # skip devices with no VID:PID
        and str(d.get("vidpid", "")).strip().lower() not in exclude_vidpids
    ]

    normalized_present_usb = body.get("present_usb", [])
    normalized_usb_state = _normalize_proxmox_usb_state(body.get("usb_state", []), normalized_present_usb)

    was_connected = bool(proxmox_state.get("connected"))
    proxmox_state["connected"] = True
    proxmox_state["last_seen"] = now
    if not was_connected:
        gap = now - (proxmox_state.get("last_seen") or now)
        _debug_event("proxmox_reconnected", f"agent={hostname} gap={gap:.0f}s")
    state._proxmox_reseed_in_progress = bool(body.get("reseed_in_progress", False))
    proxmox_state["node"] = body.get("node", {}) or {}

    # Tag each VM with the reporting agent hostname for client-side per-agent filtering.
    tagged_vms = [{**vm, "_agent_hostname": hostname} for vm in enriched_vms]

    # Tag each USB entry with the reporting agent hostname for client-side per-agent filtering.
    tagged_usb_state   = [{**e, "_agent_hostname": hostname} for e in normalized_usb_state]
    tagged_present_usb = [{**e, "_agent_hostname": hostname} for e in normalized_present_usb]
    tagged_unknown_usb = [{**e, "_agent_hostname": hostname} for e in proxmox_state.get("unknown_usb", [])]

    # Maintain per-agent rolling resource samples (1-hour window) so the detail
    # card can show per-server CPU/mem averages in a multi-Proxmox setup.
    _prev_agent = proxmox_states.get(hostname, {})
    _agent_cpu_samples: list[tuple[float, float]] = _prev_agent.get("_cpu_samples", [])
    _agent_mem_samples: list[tuple[float, float]] = _prev_agent.get("_mem_samples", [])
    _sample_cutoff = now - _RESOURCE_SAMPLE_WINDOW
    _anode = body.get("node", {}) or {}
    _cpu_pct = _anode.get("cpu_percent")
    if _cpu_pct is not None:
        _agent_cpu_samples = [(ts, v) for ts, v in _agent_cpu_samples if ts >= _sample_cutoff]
        _agent_cpu_samples.append((now, float(_cpu_pct)))
    _mem_used  = _anode.get("mem_used_kb")
    _mem_total = _anode.get("mem_total_kb")
    if _mem_used is not None and _mem_total:
        try:
            _mem_pct = float(_mem_used) / float(_mem_total) * 100.0
            _agent_mem_samples = [(ts, v) for ts, v in _agent_mem_samples if ts >= _sample_cutoff]
            _agent_mem_samples.append((now, _mem_pct))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    _agent_cpu_avg = (sum(v for _, v in _agent_cpu_samples) / len(_agent_cpu_samples)) if _agent_cpu_samples else None
    _agent_mem_avg = (sum(v for _, v in _agent_mem_samples) / len(_agent_mem_samples)) if _agent_mem_samples else None
    reported_provision_halt = body.get("provision_halt") if _autoprov_enabled() else None

    # Update per-agent state for multi-server list UI.
    # Preserve vmid_range from this telemetry cycle (or from prior state if not yet sent).
    _vmid_range_raw = body.get("vmid_range") or {}
    _vmid_range: dict[str, int] | None = None
    try:
        _vr_start = int(_vmid_range_raw.get("start", 0) or 0)
        _vr_end   = int(_vmid_range_raw.get("end",   0) or 0)
        if _vr_start > 0 and _vr_end >= _vr_start:
            _vmid_range = {"start": _vr_start, "end": _vr_end}
    except (TypeError, ValueError):
        pass
    if _vmid_range is None:
        # Fall back to previously stored range if the agent hasn't sent it yet
        _vmid_range = (_prev_agent or {}).get("vmid_range")

    proxmox_states[hostname] = {
        "connected": True,
        "last_seen": now,
        "agent_version": str(body.get("agent_version", "")).strip() or None,
        "pve_version": str(body.get("pve_version", "")).strip() or None,
        "vm_count": sum(1 for vm in enriched_vms if not vm.get("is_template")),
        "usb_count": len(normalized_usb_state),
        "node": body.get("node", {}) or {},
        "provision_halt": reported_provision_halt,
        "_cpu_samples": _agent_cpu_samples,
        "_mem_samples": _agent_mem_samples,
        "cpu_1h_avg": _agent_cpu_avg,
        "mem_1h_avg": _agent_mem_avg,
        "vmid_range": _vmid_range,
        "vm_set_override": _sanitize_vm_set_override(body.get("vm_set_override", 0)),
        "effective_vm_set": max(1, int(body.get("effective_vm_set", _hostname_vm_set_number(hostname)) or _hostname_vm_set_number(hostname))),
        "vms": tagged_vms,
        "usb_state": tagged_usb_state,
        "present_usb": tagged_present_usb,
        "unknown_usb": tagged_unknown_usb,
    }

    # Rebuild merged VM list from all approved agents so the VMs tab shows
    # all agents' VMs (not just the most recently reporting one).
    all_vms: list[dict[str, Any]] = []
    all_usb_state: list[dict[str, Any]] = []
    all_present_usb: list[dict[str, Any]] = []
    all_unknown_usb: list[dict[str, Any]] = []
    for st in proxmox_states.values():
        all_vms.extend(st.get("vms", []))
        all_usb_state.extend(st.get("usb_state", []))
        all_present_usb.extend(st.get("present_usb", []))
        all_unknown_usb.extend(st.get("unknown_usb", []))
    proxmox_state["vms"] = all_vms

    # Update the vmid→hostname routing map so delete/reclone commands target the right node.
    reported_vmids = {int(vm["vmid"]) for vm in enriched_vms if vm.get("vmid") is not None}
    # Remove stale entries owned by this agent (VMs it no longer reports).
    stale = [vmid for vmid, owner in _proxmox_agent_vm_map.items() if owner == hostname and vmid not in reported_vmids]
    for vmid in stale:
        del _proxmox_agent_vm_map[vmid]
    # Register/update all VMs reported by this agent.
    for vmid in reported_vmids:
        _proxmox_agent_vm_map[vmid] = hostname

    proxmox_state["reseed_in_progress"] = state._proxmox_reseed_in_progress
    proxmox_state["usb_state"] = all_usb_state
    proxmox_state["present_usb"] = all_present_usb
    proxmox_state["unknown_usb"] = all_unknown_usb
    proxmox_state["missing_timeout_mins"] = int(body.get("missing_timeout_mins", 60) or 60)
    proxmox_state["vm_set_override"] = _sanitize_vm_set_override(body.get("vm_set_override", 0))
    proxmox_state["effective_vm_set"] = max(1, int(body.get("effective_vm_set", _hostname_vm_set_number(hostname)) or _hostname_vm_set_number(hostname)))
    proxmox_state["agent_version"] = str(body.get("agent_version", "")).strip() or None
    proxmox_state["pve_version"] = str(body.get("pve_version", "")).strip() or None
    proxmox_state["template_lock"] = str(body.get("template_lock", "") or "").strip()
    proxmox_state["vh_devices"] = body.get("vh_devices", {})

    # Record rolling resource samples for 1-hour average threshold checks.
    # Called after agent_version/pve_version are set so _save_resource_cache persists them.
    _record_resource_samples(proxmox_state["node"], now)

    # T3 PCI devices — store the raw list from the agent and compute a filtered list
    # of devices matching the T3 target VID:PIDs (currently just 168c:0034).
    # T3_VIDPIDS defines which PCI vendor:device IDs qualify a node as a T3 host.
    T3_VIDPIDS: set[str] = {"168c:0034"}
    raw_pci: list[dict[str, Any]] = body.get("t3_pci_devices") or []
    # Normalize: keep only dicts with a vidpid field, lower-case for consistent matching.
    proxmox_state["t3_pci_devices"] = [
        d for d in raw_pci
        if isinstance(d, dict) and str(d.get("vidpid", "")).strip().lower() in T3_VIDPIDS
    ]

    # Hardware watchdog fault log + last reset reason (set by hw_watchdog_loop in agent)
    if "hw_faults" in body:
        proxmox_state["hw_faults"] = body["hw_faults"]
    if "hw_last_reset" in body and body["hw_last_reset"]:
        existing = proxmox_state.get("hw_last_reset") or {}
        incoming = body["hw_last_reset"]
        # Only overwrite if this is a newer reset record
        if not existing or incoming.get("ts", 0) > existing.get("ts", 0):
            proxmox_state["hw_last_reset"] = incoming
            # Broadcast a dedicated alert so the hub hears about it in real-time
            await broadcast({
                "type": "proxmox_hw_reset",
                "hostname": str((body.get("node") or {}).get("hostname", "") or ""),
                "reason": incoming.get("reason", ""),
                "ts": incoming.get("ts"),
                "agent_version": incoming.get("agent_version", ""),
            })

    # Persist provision_halt from the agent's telemetry so the hub can display it.
    # When auto-provisioning is disabled, force the state clear even if the agent
    # has not yet refreshed its local cache.
    if "provision_halt" in body or not _autoprov_enabled():
        proxmox_state["provision_halt"] = reported_provision_halt

    # Clear pending-delete VMIDs that the agent has confirmed are gone.
    # intersection_update keeps only IDs still in the telemetry report;
    # any VMID that has disappeared from the agent has been successfully deleted.
    if _pending_delete_vmids:
        telemetry_vmids = {int(v.get("vmid")) for v in enriched_vms if v.get("vmid") is not None}
        confirmed_deleted = _pending_delete_vmids - telemetry_vmids
        _pending_delete_vmids.intersection_update(telemetry_vmids)
        # Cancel any pending auto-recovery reclone commands for confirmed-deleted VMIDs
        if confirmed_deleted:
            # Start the post-delete cooldown now that the VM is actually gone so the
            # fleet has time to stabilise before the gate may fire again.
            state._delete_gate_cooldown_until = time.time() + DELETE_GATE_COOLDOWN_S
            logger.info(
                "Auto-delete gate: %d VM(s) confirmed deleted — cooldown active for %ds",
                len(confirmed_deleted), DELETE_GATE_COOLDOWN_S,
            )
            for cmd in commands:
                if (cmd.get("action") == "reclone_vm"
                        and cmd.get("type") == "auto-recovery"
                        and cmd.get("status") in {"pending", "delivered"}
                        and int(cmd.get("args", {}).get("vmid", -1)) in confirmed_deleted):
                    cmd["status"] = "cancelled"
                    cmd["error"] = "VM was deleted — auto-recovery cancelled"
    new_usb: list[dict] = proxmox_state["usb_state"]
    new_by_vmid = {str(e["vmid"]): e.get("prov_status", "active") for e in new_usb if e.get("vmid") is not None}
    if state._prev_usb_by_vmid:
        newly_provisioned = [
            vmid for vmid, st in new_by_vmid.items()
            if st == "active" and state._prev_usb_by_vmid.get(vmid) == "provisioning"
        ]
        torn_down = [
            vmid for vmid, st in state._prev_usb_by_vmid.items()
            if vmid not in new_by_vmid and st in ("tearing_down", "missing")
        ]
        if newly_provisioned:
            proxmox_state["prov_summary"] = {"action": "provisioned", "count": len(newly_provisioned), "at": now}
            for vmid in newly_provisioned:
                vm = next((item for item in enriched_vms if str(item.get("vmid")) == vmid), {})
                _record_vm_watchdog_clone_completed(vmid, vm.get("name"))
            await _async_save_vm_watchdog()
        elif torn_down:
            proxmox_state["prov_summary"] = {"action": "deleted", "count": len(torn_down), "at": now}
    _update_provision_run_state(proxmox_state["vms"], new_usb, now)
    state._prev_usb_by_vmid = new_by_vmid

    # Auto-reset a stale reclone run to idle when:
    #   • The run is in a non-running terminal/interrupted state
    #     (interrupted, failed — "completed" is already reset in _run_rolling_reclone)
    #   • The Proxmox agent now reports zero reclone-eligible VMs, which means
    #     any VMs that were part of the interrupted run have since been deleted.
    # This prevents the Fleet Reclone tile from staying stuck at "3/9" indefinitely
    # after an operator cleans up VMs outside the normal reclone flow.
    _stale_reclone_statuses = {"interrupted", "failed"}
    if reclone_state.get("status") in _stale_reclone_statuses:
        eligible_after_update = _reclone_targets_for_run()
        # Also clear if every VM that previously failed is now running — the
        # operator may have fixed them outside the reclone flow (e.g. by starting
        # them manually) and the stale "Failed" badge is no longer meaningful.
        failed_vmids_in_log = {
            str(e.get("vmid"))
            for e in (reclone_state.get("log") or [])
            if e.get("status") == "failed" and e.get("vmid") is not None
        }
        running_vmids = {
            str(v.get("vmid"))
            for v in enriched_vms
            if str(v.get("status", "")).lower() == "running" and v.get("vmid") is not None
        }
        all_failed_now_running = bool(failed_vmids_in_log) and failed_vmids_in_log.issubset(running_vmids)
        if not eligible_after_update or all_failed_now_running:
            reason = "0 eligible VMs" if not eligible_after_update else "all previously failed VMs are now running"
            logger.info(
                "Fleet Reclone: detected stale '%s' run (%s) — auto-resetting to idle",
                reclone_state["status"], reason,
            )
            saved_last_run = reclone_state.get("last_run")
            saved_auto_log = reclone_state.get("auto_recovery_log") or []
            reclone_state.update({
                "status": "idle",
                "type": None,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "current_vm": None,
                "log": [],
                "started_at": None,
                "last_run": saved_last_run,
                "auto_recovery_log": saved_auto_log,
            })
            # Persist the reset and push a reclone_update WS message so any
            # connected browser sees the tile clear immediately without waiting
            # for the next proxmox_update broadcast.
            await _async_save_reclone_state()
            await _broadcast_reclone_state()

    # Auto-trigger provision_unassigned when usb_auto_provision is enabled and
    # certified unassigned dongles are physically present.  Resource (CPU/memory)
    # thresholds gate provisioning and can also trigger deletion of the newest sim VM.
    _ap_enabled = settings.get("usb_auto_provision") == "on"
    _reclone_running = reclone_state.get("status") == "running"
    if not _ap_enabled:
        _autoprov_gate_log("disabled", "usb_auto_provision=off — skipping all provision/delete checks")
    elif _reclone_running:
        _autoprov_gate_log("reclone_running", "reclone job is running (status=%s) — skipping provision checks", reclone_state.get("status"))
    if _ap_enabled and not _reclone_running:
        def _pct_setting(key: str, default: str) -> int:
            try:
                return max(0, min(100, int(str(settings.get(key, default)).strip() or default)))
            except (TypeError, ValueError):
                return int(default)

        cpu_prov_thr  = _pct_setting("cpu_provision_threshold", "80")
        cpu_del_thr   = _pct_setting("cpu_delete_threshold",   "90")
        cpu_prov_ceil = _pct_setting("cpu_provision_ceiling",  "90")
        mem_prov_thr  = _pct_setting("mem_provision_threshold", "80")
        mem_del_thr   = _pct_setting("mem_delete_threshold",   "90")
        cpu_avg = _resource_1h_average(_agent_cpu_samples)
        mem_avg = _resource_1h_average(_agent_mem_samples)
        # Most-recent instantaneous CPU reading (updated every ~30 s by telemetry).
        # Used as a hard ceiling to block provisioning during ramp-up before the
        # 1-hour average catches up.
        cpu_instant = _agent_cpu_samples[-1][1] if _agent_cpu_samples else None

        # Delete gate: if either metric exceeds its delete threshold and no delete is
        # already in flight, remove the newest sim VM (highest VMID) to shed load.
        #
        # The check and enqueue are performed atomically under state_lock to prevent
        # a TOCTOU race where multiple concurrent telemetry calls each see
        # delete_queued=False and each independently queue a delete for the same VM.
        delete_queued = False  # initialise; set True inside the atomic lock section below
        _threshold_exceeded = (
            (cpu_avg is not None and cpu_avg >= cpu_del_thr) or
            (mem_avg is not None and mem_avg >= mem_del_thr)
        )
        if _threshold_exceeded:
            usb_vmids_int: set[int] = set()
            _usb_prov_status: dict[int, str] = {}
            for _e in normalized_usb_state:
                try:
                    _evmid = int(_e["vmid"])
                    usb_vmids_int.add(_evmid)
                    _usb_prov_status[_evmid] = str(_e.get("prov_status") or "active").strip().lower()
                except (KeyError, TypeError, ValueError):
                    pass
            # Correct stale "provisioning" status: the bash agent's usb_state lags by
            # one telemetry cycle after the spoke's prov_run finishes configuring a VM.
            # Without this correction newly-configured or failed VMs remain stuck in
            # "provisioning" and are excluded from delete candidates.
            # NOTE: do NOT guard on `not running` — if a parallel clone was killed mid-run
            # (stuck >120s), the overall run stays running=True indefinitely but individual
            # items already have status="done" or "failed". We must correct those too.
            _prov_run_snap = proxmox_state.get("prov_run") or {}
            for _pr_item in (_prov_run_snap.get("items") or []):
                if isinstance(_pr_item, dict) and str(_pr_item.get("status") or "").strip().lower() in {"done", "failed"}:
                    try:
                        _pr_vid = int(_pr_item.get("vmid") or 0)
                        if _pr_vid and _usb_prov_status.get(_pr_vid) == "provisioning":
                            _usb_prov_status[_pr_vid] = "active"
                    except (TypeError, ValueError):
                        pass
            # Exclude VMs that are mid-clone (provisioning) or already being torn down
            # by the USB-missing timeout handler (tearing_down) — both are transient
            # states where a second delete command causes wasted work or race conditions.
            _skip_statuses = {"provisioning", "tearing_down"}
            candidates: list[int] = []
            for _vm in enriched_vms:
                try:
                    _vid = int(_vm.get("vmid", 0) or 0)
                    if (
                        _vm.get("type") == "qemu"
                        and not _vm.get("is_template")
                        and _vid in usb_vmids_int
                        and _vid not in _pending_delete_vmids
                        and _usb_prov_status.get(_vid, "active") not in _skip_statuses
                    ):
                        candidates.append(_vid)
                except (TypeError, ValueError):
                    pass
            if candidates:
                target_vmid = max(candidates)  # newest = highest VMID
                _del_args = _prepare_delete_vm_args({"vmid": target_vmid})
                # Re-check and enqueue atomically under state_lock to close the TOCTOU
                # window between the threshold check above and the actual queue operation.
                async with state_lock:
                    # Respect the post-delete cooldown so consecutive auto-deletes are
                    # separated by at least DELETE_GATE_COOLDOWN_S (set after the prior
                    # delete is confirmed, not at enqueue time — see confirmed_deleted block).
                    if time.time() < state._delete_gate_cooldown_until:
                        _remaining_cd = int(state._delete_gate_cooldown_until - time.time())
                        logger.info(
                            "Auto-delete gate: cooldown active (%ds remaining) — skipping delete of VMID %d",
                            _remaining_cd, target_vmid,
                        )
                    else:
                        delete_queued = any(
                            c.get("action") == "delete_vm"
                            and c.get("status") not in {"completed", "failed", "expired"}
                            for c in commands
                        )
                        if not delete_queued:
                            _enqueue_command_locked(
                                _resolve_proxmox_vm_target(target_vmid),
                                "delete_vm",
                                _del_args,
                                command_type="auto-provision",
                            )
                            _pending_delete_vmids.add(target_vmid)
                            # Also start the cooldown at enqueue time so the gate cannot
                            # fire a second time during the window between "delete command
                            # executed by agent" and "telemetry confirms VM gone".
                            # The confirmed_deleted block will refresh the cooldown once
                            # the deletion is confirmed, giving the full window from that
                            # later point.
                            state._delete_gate_cooldown_until = time.time() + DELETE_GATE_COOLDOWN_S
                            logger.info(
                                "Auto-provision resource gate: delete threshold exceeded "
                                "(cpu_avg=%.1f%% mem_avg=%.1f%%) — queued delete_vm for VMID %d; "
                                "cooldown active for %ds",
                                cpu_avg or 0.0, mem_avg or 0.0, target_vmid, DELETE_GATE_COOLDOWN_S,
                            )
            else:
                logger.info(
                    "Auto-provision resource gate: delete threshold exceeded "
                    "(cpu_avg=%.1f%% mem_avg=%.1f%%) — no eligible candidates "
                    "(all USB VMs are provisioning, tearing_down, or pending delete)",
                    cpu_avg or 0.0, mem_avg or 0.0,
                )

        # Provision gate: skip new provisioning when either resource exceeds its threshold.
        # Also skip for this cycle if we just queued a delete, to avoid churn.
        # Also skip for the full delete-gate cooldown window — prevents the dongle that
        # was just freed by a resource-triggered delete from being immediately re-provisioned
        # (which would otherwise create a delete→reprovision→delete loop).
        # cpu_prov_ceil is a hard ceiling on the *instantaneous* CPU reading so that
        # provisioning is suppressed during ramp-up before the 1-hour average catches up.
        _in_delete_cooldown = time.time() < state._delete_gate_cooldown_until
        if _in_delete_cooldown:
            _remaining_prov_cd = int(state._delete_gate_cooldown_until - time.time())
            logger.info(
                "Auto-provision gate: delete cooldown active (%ds remaining) — suppressing provision_unassigned",
                _remaining_prov_cd,
            )
        _ceil_hit = cpu_instant is not None and cpu_instant >= cpu_prov_ceil
        if _ceil_hit:
            logger.info(
                "Auto-provision ceiling: instantaneous CPU %.1f%% >= ceiling %d%% — suppressing provision_unassigned",
                cpu_instant, cpu_prov_ceil,
            )
        resource_ok = (
            not delete_queued
            and not _in_delete_cooldown
            and not _ceil_hit
            and cpu_avg is not None and cpu_avg < cpu_prov_thr
            and mem_avg is not None and mem_avg < mem_prov_thr
        )
        # Log resource state periodically so the journal shows what the gate sees
        _autoprov_gate_log(
            "resource_state",
            "cpu_avg=%.1f%% (thr=%d%%) mem_avg=%.1f%% (thr=%d%%) cpu_instant=%.1f%% (ceil=%d%%) "
            "delete_queued=%s in_delete_cooldown=%s ceil_hit=%s resource_ok=%s",
            cpu_avg or 0.0, cpu_prov_thr,
            mem_avg or 0.0, mem_prov_thr,
            cpu_instant or 0.0, cpu_prov_ceil,
            delete_queued, _in_delete_cooldown, _ceil_hit, resource_ok,
        )
        if not resource_ok and not _ceil_hit:
            if delete_queued:
                _autoprov_gate_log("delete_queued", "delete_vm already in queue — suppressing provision_unassigned")
            elif _in_delete_cooldown:
                pass  # already logged above
            elif cpu_avg is None or mem_avg is None:
                _autoprov_gate_log("no_telemetry", "waiting for CPU/mem telemetry (cpu_avg=%s mem_avg=%s) — suppressing provision_unassigned", cpu_avg, mem_avg)
            elif cpu_avg >= cpu_prov_thr:
                _autoprov_gate_log("cpu_threshold", "cpu_avg=%.1f%% >= threshold=%d%% — suppressing provision_unassigned", cpu_avg, cpu_prov_thr)
            elif mem_avg >= mem_prov_thr:
                _autoprov_gate_log("mem_threshold", "mem_avg=%.1f%% >= threshold=%d%% — suppressing provision_unassigned", mem_avg, mem_prov_thr)
        prov_run = proxmox_state.get("prov_run") or {}
        if resource_ok and prov_run.get("running"):
            _autoprov_gate_log(
                "prov_run_active",
                "prov_run.running=True — provision loop already active, skipping trigger "
                "(vmids=%s status=%s)",
                [i.get("vmid") for i in (prov_run.get("items") or [])],
                [i.get("status") for i in (prov_run.get("items") or [])],
            )
        if resource_ok and not prov_run.get("running"):
            unassigned = _proxmox_unassigned_present_usb()
            if not unassigned:
                _autoprov_gate_log("no_unassigned", "no unassigned USB dongles present — nothing to provision")
            if unassigned:
                certified_set = {
                    (str(v.get("vidpid", "")).strip().lower() if isinstance(v, dict) else str(v).strip().lower())
                    for v in _parse_json_list(settings.get("usb_vidpids", "[]"))
                    if (str(v.get("vidpid", "") if isinstance(v, dict) else v)).strip()
                }
                certified_unassigned = [
                    u for u in unassigned
                    if str(u.get("vidpid", "")).strip().lower() in certified_set
                ]
                if not certified_unassigned:
                    _autoprov_gate_log(
                        "not_certified",
                        "unassigned dongles present but none match certified VIDPIDs — "
                        "unassigned=%s certified_vidpids=%s",
                        [u.get("vidpid") for u in unassigned],
                        sorted(certified_set),
                    )
                if certified_unassigned:
                    has_pending = any(
                        c.get("action") == "provision_unassigned"
                        and c.get("status") not in {"completed", "failed", "expired"}
                        for c in commands
                    )
                    if has_pending:
                        pending_cmd = next(
                            (c for c in commands if c.get("action") == "provision_unassigned"
                             and c.get("status") not in {"completed", "failed", "expired"}), None
                        )
                        _autoprov_gate_log(
                            "already_pending",
                            "provision_unassigned already pending (id=%s status=%s) — not queuing again",
                            pending_cmd.get("id") if pending_cmd else "?",
                            pending_cmd.get("status") if pending_cmd else "?",
                        )
                    if not has_pending:
                        await _queue_proxmox_command("provision_unassigned", {}, command_type="auto-provision")
                        logger.info(
                            "Auto-provisioning: detected %d unassigned certified dongle(s) — queued provision_unassigned",
                            len(certified_unassigned),
                        )

    # ── VMID gap audit ────────────────────────────────────────────────────────
    # If the auto-provision loop previously deleted/re-provisioned VMs out of
    # order, VMIDs can develop gaps (e.g. …90030, 90032 with 90031 missing).
    # This audit detects such gaps and queues a delete for the highest VMID
    # above the lowest gap so the provision loop can fill the hole on the next
    # cycle.  It bypasses the normal delete-gate cooldown because it is a
    # corrective bookkeeping action, not a resource-pressure shedding action.
    # It does respect its own per-host interval to avoid hammering the queue.
    if _ap_enabled and not _reclone_running and _vmid_range:
        _audit_due = (now - _vmid_gap_audit_last_run.get(hostname, 0.0)) >= VMID_AUDIT_INTERVAL_S
        if _audit_due:
            _vmid_gap_audit_last_run[hostname] = now
            _gap_start: int = _vmid_range["start"]
            _gap_end:   int = _vmid_range["end"]

            # Build map of VMID → prov_status for VMs in this host's range.
            _gap_prov_status: dict[int, str] = {}
            for _ge in normalized_usb_state:
                try:
                    _gvid = int(_ge["vmid"])
                    if _gap_start <= _gvid <= _gap_end:
                        _gap_prov_status[_gvid] = str(_ge.get("prov_status") or "active").strip().lower()
                except (KeyError, TypeError, ValueError):
                    pass
            # Apply the same stale-provisioning correction used by the delete gate.
            _gap_prov_snap = proxmox_state.get("prov_run") or {}
            for _gpr in (_gap_prov_snap.get("items") or []):
                if isinstance(_gpr, dict) and str(_gpr.get("status") or "").strip().lower() in {"done", "failed"}:
                    try:
                        _gpvid = int(_gpr.get("vmid") or 0)
                        if _gap_prov_status.get(_gpvid) == "provisioning":
                            _gap_prov_status[_gpvid] = "active"
                    except (TypeError, ValueError):
                        pass

            # Active (stable) VMIDs only — skip anything in-flight.
            _gap_skip = {"provisioning", "tearing_down"}
            _gap_active = sorted(
                vid for vid, st in _gap_prov_status.items()
                if st not in _gap_skip and vid not in _pending_delete_vmids
            )

            if len(_gap_active) >= 2:
                _gap_max = _gap_active[-1]
                _gap_active_set = set(_gap_active)
                _lowest_gap: int | None = None
                for _chk in range(_gap_start, _gap_max):
                    if _chk not in _gap_active_set:
                        _lowest_gap = _chk
                        break

                if _lowest_gap is not None:
                    # Find highest active VMID above the gap.
                    _above_gap = [v for v in _gap_active if v > _lowest_gap]
                    if _above_gap:
                        _gap_target = max(_above_gap)
                        _gap_del_args = _prepare_delete_vm_args({"vmid": _gap_target})
                        async with state_lock:
                            _gap_already_pending = any(
                                c.get("action") == "delete_vm"
                                and c.get("status") not in {"completed", "failed", "expired"}
                                for c in commands
                            )
                            if not _gap_already_pending:
                                _enqueue_command_locked(
                                    _resolve_proxmox_vm_target(_gap_target),
                                    "delete_vm",
                                    _gap_del_args,
                                    command_type="auto-provision",
                                )
                                _pending_delete_vmids.add(_gap_target)
                                _gap_msg = (
                                    f"VMID gap audit [{hostname}]: gap detected at {_lowest_gap} "
                                    f"(range {_gap_start}-{_gap_end}, active={_gap_active}) — "
                                    f"queued delete_vm for VMID {_gap_target} to restore sequential order"
                                )
                                logger.info(_gap_msg)
                                proxmox_log_buffer.append(_gap_msg)
                                if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
                                    del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
                                await broadcast({"type": "proxmox_log_update", "lines": [_gap_msg]})
                            else:
                                logger.info(
                                    "VMID gap audit [%s]: gap at %d would target VMID %d "
                                    "but a delete_vm is already pending — skipping",
                                    hostname, _lowest_gap, _gap_target,
                                )
                else:
                    logger.debug(
                        "VMID gap audit [%s]: no gaps in active VMIDs %s (range %d-%d)",
                        hostname, _gap_active, _gap_start, _gap_end,
                    )

    # Append new log lines to ring buffer and broadcast if any arrived
    new_lines = [str(ln) for ln in (body.get("log_lines") or []) if ln]
    if new_lines:
        # Prefix log lines with the agent hostname so multi-agent logs are distinguishable
        if len(approved_proxmox_agents) > 1:
            new_lines = [f"[{hostname}] {ln}" if not ln.startswith(f"[{hostname}]") else ln for ln in new_lines]
        proxmox_log_buffer.extend(new_lines)
        if len(proxmox_log_buffer) > PROXMOX_LOG_MAX:
            del proxmox_log_buffer[:len(proxmox_log_buffer) - PROXMOX_LOG_MAX]
        await broadcast({"type": "proxmox_log_update", "lines": new_lines})

    await _broadcast_proxmox_state()
    return {"ok": True}




async def _ack_command_internal(body: dict[str, Any]) -> dict[str, bool]:
    cmd_id = str(body.get("id", "")).strip()
    status = str(body.get("status", "completed")).strip().lower()
    message = body.get("message", "")

    if status not in ("completed", "failed"):
        raise HTTPException(status_code=422, detail="status must be 'completed' or 'failed'")

    async with state_lock:
        expired, purged = _cleanup_commands_locked()
        cmd = next((c for c in commands if c["id"] == cmd_id), None)
        if not cmd:
            raise HTTPException(status_code=404, detail="Command not found")

        cmd["status"] = status
        cmd["message"] = str(message) if message is not None else ""
        cmd["updated_at"] = time.time()
        cmd["purge_after"] = cmd["updated_at"] + COMMAND_RESULT_RETENTION_SECS
        _trace("agent_ack", cmd_id=cmd_id, action=cmd.get("action"), target=cmd.get("target"),
               args={k: v for k, v in (cmd.get("args") or {}).items() if k in {"vmid", "vm_type"}},
               status=status, message=str(message)[:200] if message else "")
        await _async_save_commands()
        serialized = _serialize_commands()

    await broadcast({"type": "commands_update", "commands": serialized})
    return {"ok": True}




async def _proxmox_disconnect_grace(expected_hostname: str | None) -> None:
    await asyncio.sleep(PROXMOX_WS_GRACE_SECS)
    if state.proxmox_ws_connection is not None:
        return
    if expected_hostname and state.proxmox_ws_hostname and _proxmox_hostnames_match(expected_hostname, state.proxmox_ws_hostname):
        return
    if proxmox_state.get("last_seen") and (time.time() - float(proxmox_state.get("last_seen") or 0)) <= PROXMOX_WS_GRACE_SECS:
        return
    proxmox_state["connected"] = False
    proxmox_state["vms"] = []
    await _broadcast_proxmox_state()
