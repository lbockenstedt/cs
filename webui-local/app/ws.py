"""WebSocket connection registry and message routing for browsers and spokes.

Browsers connect to /ws for live updates (filtered by tenant). Spokes keep a WebSocket
open so the hub can push commands. Interactive features (shell, VNC, log fetch, token
provisioning, command trace) route spoke replies to per-request asyncio queues.
"""
# Postpone annotation evaluation so modern hint syntax works.
from __future__ import annotations

# asyncio provides locks, queues and cross-thread scheduling; json encodes outgoing messages.
import asyncio
import json
# Optional marks values that may be None.
from typing import Optional

# FastAPI WebSocket type, its disconnect error, and HTTPException raised by token decoding.
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

# Token decoder and the User model for authenticated browser connections.
from .auth import decode_access_token
from .data_models import User

# Maps each browser WebSocket to the set of tenant IDs it may see.
# None means the connection belongs to a superadmin who sees all tenants.
browser_connections: dict[WebSocket, Optional[frozenset[str]]] = {}
# Live spoke connections keyed by (tenant_id, spoke_id).
spoke_connections: dict[tuple[str, str], WebSocket] = {}
# One lock per spoke so concurrent sends never interleave frames on the same socket.
_spoke_send_locks: dict[tuple[str, str], asyncio.Lock] = {}
# Reply queues for interactive sessions, keyed by session/request id (shell, VNC, log fetch).
_shell_queues: dict[str, asyncio.Queue] = {}
_vnc_queues: dict[str, asyncio.Queue] = {}
_log_fetch_queues: dict[str, asyncio.Queue] = {}
# The server's main event loop, so synchronous code in threads can schedule async sends.
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the main event loop (called once at start-up)."""
    # Assign the module-level variable rather than a local one.
    global _main_loop
    _main_loop = loop


def _extract_ws_token(message: str) -> str:
    """Pull an auth token out of the first WebSocket message.

    Accepts a bare token string, {"token": ...}, or {"payload": {"token": ...}}; returns "" if none.
    """
    # Treat None as empty and ignore surrounding whitespace.
    candidate = str(message or "").strip()
    if not candidate:
        return ""
    # Not JSON: assume the whole message is the token.
    if not candidate.startswith("{"):
        return candidate
    # Parse JSON; invalid JSON means no token.
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return ""
    # Only JSON objects can contain a token.
    if not isinstance(payload, dict):
        return ""
    # Top-level "token" field.
    token = payload.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    # Nested {"payload": {"token": ...}} form.
    inner = payload.get("payload")
    if isinstance(inner, dict):
        nested = inner.get("token")
        if isinstance(nested, str):
            return nested.strip()
    return ""


async def _close_unauthorized(websocket: WebSocket, reason: str) -> None:
    """Close the socket with application code 4401 ("unauthorized") and a reason text."""
    await websocket.close(code=4401, reason=reason)


async def _authenticate_browser_websocket(websocket: WebSocket) -> Optional[User]:
    """Authenticate a browser WebSocket. Returns the User on success, None on failure."""
    # First option: token passed in the URL query string (?token=...).
    token = str(websocket.query_params.get("token") or "").strip()
    if token:
        # Validate it before accepting; reject the socket on failure.
        try:
            user = decode_access_token(token)
        except HTTPException:
            await _close_unauthorized(websocket, "Invalid credentials")
            return None
        # Valid: accept the connection and return the user.
        await websocket.accept()
        return user

    # Second option: accept, then expect the token as the first message within 10 seconds.
    await websocket.accept()
    try:
        token = _extract_ws_token(await asyncio.wait_for(websocket.receive_text(), timeout=10))
    # Client never sent a token in time.
    except asyncio.TimeoutError:
        await _close_unauthorized(websocket, "Authentication timeout")
        return None
    # Client went away before authenticating.
    except WebSocketDisconnect:
        return None
    # Any other receive error is treated as missing authentication.
    except Exception:
        await _close_unauthorized(websocket, "Authentication required")
        return None

    # First message contained no token.
    if not token:
        await _close_unauthorized(websocket, "Authentication required")
        return None

    # Validate the token from the message.
    try:
        user = decode_access_token(token)
    except HTTPException:
        await _close_unauthorized(websocket, "Invalid credentials")
        return None
    return user


async def ws_connect(websocket: WebSocket) -> None:
    """Handle one browser WebSocket for its whole lifetime: authenticate, register, wait, clean up.
    """
    # Authentication closes the socket itself on failure, so just stop here.
    user = await _authenticate_browser_websocket(websocket)
    if user is None:
        return
    # Superadmins get None (see all tenants); others get a frozenset of their tenant IDs.
    tenant_scope: Optional[frozenset[str]] = None if user.is_superadmin else frozenset(user.tenant_ids())
    # Register the socket so broadcasts reach it.
    browser_connections[websocket] = tenant_scope
    # Keep reading (and ignoring) client messages until the browser disconnects.
    try:
        while True:
            await websocket.receive_text()
    # Disconnects/errors simply end the loop.
    except Exception:
        pass
    # Always remove the socket from the registry when it goes away.
    finally:
        browser_connections.pop(websocket, None)


async def register_spoke(websocket: WebSocket, tenant_id: str, spoke_id: str) -> None:
    """Accept a spoke's WebSocket and remember it as that spoke's live connection."""
    # Complete the WebSocket handshake.
    await websocket.accept()
    # Replace any previous connection for this spoke and make sure it has a send lock.
    key = (tenant_id, spoke_id)
    spoke_connections[key] = websocket
    _spoke_send_locks.setdefault(key, asyncio.Lock())


async def unregister_spoke(tenant_id: str, spoke_id: str, websocket: WebSocket | None = None) -> None:
    """Forget a spoke's connection; if ``websocket`` is given, only if it is still the current one.
    """
    # Look up the currently registered socket for this spoke.
    key = (tenant_id, spoke_id)
    current = spoke_connections.get(key)
    # Avoid removing a newer connection that replaced the one being closed.
    if websocket is None or current is websocket:
        spoke_connections.pop(key, None)
        _spoke_send_locks.pop(key, None)


async def ws_broadcast(data: dict) -> None:
    """Send an event to every browser allowed to see its tenant; drop dead sockets."""
    # Extract the tenant_id this event belongs to (may be top-level or nested in "job").
    tenant_id: Optional[str] = data.get("tenant_id") or (data.get("job") or {}).get("tenant_id")
    # Make sure json-encoding happens only once for all recipients.
    dead = set()
    message = json.dumps(data)
    # Iterate over a snapshot because the dict may change while we await sends.
    for ws, tenant_scope in tuple(browser_connections.items()):
        # tenant_scope is None → superadmin, sees everything.
        # Otherwise only send if tenant_id matches the connection's allowed set.
        if tenant_id and tenant_scope is not None and tenant_id not in tenant_scope:
            continue
        # Send; any failure means the socket is dead.
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    # Remove dead sockets after the loop so the dict is not modified while iterating.
    for ws in dead:
        browser_connections.pop(ws, None)


def register_shell_session(session_id: str) -> asyncio.Queue:
    """Create and register a reply queue for an interactive shell session."""
    queue: asyncio.Queue = asyncio.Queue()
    _shell_queues[session_id] = queue
    return queue


def unregister_shell_session(session_id: str) -> None:
    """Remove a shell session's reply queue."""
    _shell_queues.pop(session_id, None)


def route_shell_message(message: dict) -> None:
    """Deliver a spoke shell message to the queue of the session it belongs to."""
    # Messages without a session id cannot be routed.
    session_id = str(message.get("session_id") or "").strip()
    if not session_id:
        return
    # Drop messages for sessions that have already closed.
    queue = _shell_queues.get(session_id)
    if queue is not None:
        queue.put_nowait(message)


def register_vnc_session(request_id: str) -> asyncio.Queue:
    """Create and register a reply queue for a VNC console request."""
    queue: asyncio.Queue = asyncio.Queue()
    _vnc_queues[request_id] = queue
    return queue


def unregister_vnc_session(request_id: str) -> None:
    """Remove a VNC request's reply queue."""
    _vnc_queues.pop(request_id, None)


def route_vnc_message(message: dict) -> None:
    """Deliver a spoke VNC message to the queue of the request it belongs to."""
    # Messages without a request id cannot be routed.
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    # Drop messages for requests that have already finished.
    queue = _vnc_queues.get(request_id)
    if queue is not None:
        queue.put_nowait(message)


# ── Proxmox token provisioning session queues ─────────────────────
# Reply queues for Proxmox API-token provisioning requests, keyed by request id.
_provision_queues: dict[str, asyncio.Queue] = {}


def register_provision_session(request_id: str) -> asyncio.Queue:
    """Create and register a reply queue for a token-provisioning request."""
    queue: asyncio.Queue = asyncio.Queue()
    _provision_queues[request_id] = queue
    return queue


def unregister_provision_session(request_id: str) -> None:
    """Remove a token-provisioning request's reply queue."""
    _provision_queues.pop(request_id, None)


def route_provision_message(message: dict) -> None:
    """Deliver a spoke provisioning reply to the waiting request's queue."""
    # Messages without a request id cannot be routed.
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    # Drop replies for requests that already gave up.
    queue = _provision_queues.get(request_id)
    if queue is not None:
        queue.put_nowait(message)


async def send_to_spoke(tenant_id: str, spoke_id: str, message: dict) -> bool:
    """Send a JSON message to a connected spoke. Returns False if not connected or the send fails.
    """
    # Find the spoke's live socket; none means it is offline.
    key = (tenant_id, spoke_id)
    websocket = spoke_connections.get(key)
    if websocket is None:
        return False
    # Serialise sends to this spoke.
    lock = _spoke_send_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            await websocket.send_json(message)
    # A failed send means the socket is broken: unregister it so we stop trying.
    except Exception:
        await unregister_spoke(tenant_id, spoke_id, websocket)
        return False
    return True


async def send_spoke_command(tenant_id: str, spoke_id: str, command: dict) -> bool:
    """Push a single command to a spoke immediately; returns False if it could not be sent."""
    return await send_to_spoke(tenant_id, spoke_id, {"type": "commands", "commands": [command]})


async def push_spoke_commands(tenant_id: str, spoke_id: str) -> bool:
    """Send all queued commands to a spoke over its WebSocket and mark them delivered.

    Returns False if the spoke is offline or the send fails (commands stay queued for polling).
    """
    # Spoke offline: leave commands in the queue.
    websocket = spoke_connections.get((tenant_id, spoke_id))
    if websocket is None:
        return False

    # Imported here to avoid a circular import with the store module.
    from . import store

    # Look at queued commands without removing them yet.
    commands = store.peek_queued_commands(tenant_id, spoke_id)
    # Nothing to send counts as success.
    if not commands:
        return True

    # Wire format the spoke expects: a list of commands with id, target, type and payload.
    payload = {
        "type": "commands",
        "commands": [
            {"id": c.id, "target": c.target, "type": c.type, "payload": c.payload}
            for c in commands
        ],
    }
    # Only mark delivered after a successful send, so failures are retried later.
    sent = await send_to_spoke(tenant_id, spoke_id, payload)
    if not sent:
        return False

    store.mark_commands_delivered(tenant_id, spoke_id, [command.id for command in commands])
    return True


def notify_spoke_command(tenant_id: str, spoke_id: str) -> None:
    """Thread-safe trigger: schedule push_spoke_commands on the main loop (no-op before start-up).
    """
    # Before start-up there is no loop to schedule on.
    if _main_loop is None:
        return
    # Schedule the coroutine from whatever thread we are in.
    asyncio.run_coroutine_threadsafe(push_spoke_commands(tenant_id, spoke_id), _main_loop)


def register_log_fetch(request_id: str) -> asyncio.Queue:
    """Create and register a reply queue for a remote log-fetch request."""
    queue: asyncio.Queue = asyncio.Queue()
    _log_fetch_queues[request_id] = queue
    return queue


def unregister_log_fetch(request_id: str) -> None:
    """Remove a log-fetch request's reply queue."""
    _log_fetch_queues.pop(request_id, None)


def route_log_fetch_message(message: dict) -> None:
    """Deliver a spoke log-fetch reply to the waiting request's queue."""
    # Messages without a request id cannot be routed.
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    # Drop replies for requests that already gave up.
    queue = _log_fetch_queues.get(request_id)
    if queue is not None:
        queue.put_nowait(message)


# ── Command trace queues (hub → spoke → hub round-trip for debug) ─────────────
# Reply queues for debug command traces, keyed by request id.
_command_trace_queues: dict[str, asyncio.Queue] = {}


def register_command_trace(request_id: str) -> asyncio.Queue:
    """Create and register a reply queue for a command-trace request."""
    queue: asyncio.Queue = asyncio.Queue()
    _command_trace_queues[request_id] = queue
    return queue


def unregister_command_trace(request_id: str) -> None:
    """Remove a command-trace request's reply queue."""
    _command_trace_queues.pop(request_id, None)


def route_command_trace_message(message: dict) -> None:
    """Deliver a spoke command-trace reply to the waiting request's queue."""
    # Messages without a request id cannot be routed.
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    # Drop replies for requests that already gave up.
    queue = _command_trace_queues.get(request_id)
    if queue is not None:
        queue.put_nowait(message)
