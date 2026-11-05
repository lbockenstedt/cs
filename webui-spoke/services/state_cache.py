"""State Cache (helpers moved verbatim from server.py; shared deps imported from server)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# Shared state, file paths and limits come from server.py; this module saves and loads the cache.
from server import (
    OFFLINE_TIMEOUT,
    STATE_CACHE_FILE,
    STATE_CACHE_MIN_INTERVAL,
    _atomic_write_json,
    json,
    logger,
    proxmox_state,
    proxmox_states,
    repo_state,
    state,
    time,
)



def _save_state_cache(force: bool = False) -> None:
    """Save the current dashboard state to disk, at most once per minimum interval unless forced."""
    # Skip the save if one happened too recently (unless forced), to limit disk writes.
    now = time.time()
    if not force and (now - state._state_cache_last_save) < STATE_CACHE_MIN_INTERVAL:
        return
    # Build the snapshot; per-agent VM lists are left out because agents resend them.
    try:
        cache = {
            "proxmox_state": dict(proxmox_state),
            "proxmox_states": {k: {ek: ev for ek, ev in v.items() if ek != "vms"} for k, v in proxmox_states.items()},
            "central_status": state.central_status,
            "central_wireless_clients": dict(state.central_wireless_clients),
            "repo_state": dict(repo_state),
            "ts": now,
        }
        # Write atomically so a crash never leaves a half-written file, then note the save time.
        _atomic_write_json(STATE_CACHE_FILE, cache)
        state._state_cache_last_save = now
    except Exception as exc:
        # A failed save is logged but never raised.
        logger.warning("Could not write state cache: %s", exc)




def _load_state_cache() -> None:
    """Restore last-known state from disk so the UI renders immediately on restart
    instead of showing empty state for up to one full agent poll interval (60 s)."""
    try:
        # No cache file means nothing to restore.
        if not STATE_CACHE_FILE.exists():
            return
        # Read the file and work out how old it is.
        cache = json.loads(STATE_CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - cache.get("ts", 0)
        # Restore the main Proxmox state.
        cached_px = cache.get("proxmox_state", {})
        if cached_px:
            proxmox_state.update(cached_px)
            # Restore connected status only if last_seen is within OFFLINE_TIMEOUT;
            # otherwise agent has gone quiet and we should show disconnected.
            last_seen_ts = cached_px.get("last_seen")
            if last_seen_ts and (time.time() - last_seen_ts) <= OFFLINE_TIMEOUT:
                proxmox_state["connected"] = cached_px.get("connected", False)
            else:
                proxmox_state["connected"] = False
        # Restore Central check status and client counts.
        state.central_status.update(cache.get("central_status", {}))
        state.central_wireless_clients.update(cache.get("central_wireless_clients", {}))
        # Restore per-agent states (without VMs — those re-populate on first telemetry push).
        # Mark connected=False if the agent hasn't been seen within OFFLINE_TIMEOUT.
        cached_px_states = cache.get("proxmox_states", {})
        for hn, st in cached_px_states.items():
            if not isinstance(st, dict):
                continue
            last_seen_ts = st.get("last_seen")
            connected = bool(st.get("connected", False)) and bool(
                last_seen_ts and (time.time() - last_seen_ts) <= OFFLINE_TIMEOUT
            )
            proxmox_states[hn] = {**st, "connected": connected, "vms": []}
        # Restore repo sync info.
        cached_repo = cache.get("repo_state", {})
        if cached_repo:
            # Restore last_sync timestamp and last error for display, but mark
            # synced=False — we haven't actually synced since this restart yet.
            repo_state["last_sync"] = cached_repo.get("last_sync")
            repo_state["error"] = cached_repo.get("error")
            repo_state["synced"] = False
        # Log how old the restored state was.
        logger.info("Restored state cache from disk (age=%.0fs)", age)
    except Exception as exc:
        # A broken cache is logged and ignored; the app starts with empty state.
        logger.warning("Could not load state cache: %s", exc)
