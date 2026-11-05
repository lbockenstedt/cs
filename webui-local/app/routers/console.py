"""Browser access to VM consoles (noVNC) and spoke shells, relayed through the spoke WebSocket.

The hub never connects to Proxmox directly here: browser frames are forwarded to the
spoke over its WebSocket, and the spoke's replies come back through per-session queues
in app.ws.
"""
# Postpone annotation evaluation so modern hint syntax works.
from __future__ import annotations

# asyncio runs the two relay directions concurrently; base64 wraps binary VNC frames in JSON.
import asyncio
import base64
# logging reports relay failures; time tracks session expiry; uuid makes session/request ids.
import logging
import time
import uuid
# Any is used for loosely structured message dicts.
from typing import Any
# FastAPI router, dependencies, errors, query params and WebSocket types.
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
# HTMLResponse serves the console page.
from fastapi.responses import HTMLResponse
# WebSocketState tells whether a socket is already closed.
from starlette.websockets import WebSocketState

# auth checks tokens/membership; store loads spokes; relay_ws talks to spokes.
from .. import auth, store, ws as relay_ws
# User model for type hints.
from ..data_models import User

# Router mounted by app.main without a prefix (paths below are absolute).
router = APIRouter()
# Module logger.
logger = logging.getLogger(__name__)

# Pending console sessions: session id -> tenant/spoke/request ids and expiry time.
_sessions: dict[str, dict[str, Any]] = {}
# Seconds a console session id stays valid before the browser must connect.
SESSION_TTL = 60
# Seconds to wait for the spoke to confirm a shell started.
SHELL_START_TIMEOUT = 10


def _cleanup_sessions() -> None:
    """Remove console sessions whose expiry time has passed."""
    # Collect expired keys first, then delete, so the dict is not changed while iterating.
    now = time.time()
    expired = [key for key, value in _sessions.items() if value.get("expires", 0) < now]
    for key in expired:
        _sessions.pop(key, None)


def _require_tenant_admin(tenant_id: str, user: User) -> None:
    """Require explicit tenant membership with the admin role (403 otherwise)."""
    # Remote control needs explicit membership; then the role must be admin.
    auth.require_tenant_member(tenant_id, user)
    if user.get_role(tenant_id) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


async def _close_shell_websocket(websocket: WebSocket, code: int, reason: str) -> None:
    """Close the shell WebSocket with a code and reason unless it is already closed."""
    if websocket.application_state != WebSocketState.DISCONNECTED:
        await websocket.close(code=code, reason=reason)


async def _browser_to_spoke_shell(websocket: WebSocket, tenant_id: str, spoke_id: str, session_id: str) -> None:
    """Forward keystrokes and terminal resizes from the browser to the spoke shell until an error.
    """
    # Read browser messages forever; exceptions (disconnect, relay failure) end the loop.
    while True:
        message = await websocket.receive_json()
        # Only JSON objects are valid shell messages.
        if not isinstance(message, dict):
            raise ValueError("Shell websocket payload must be a JSON object")
        # Normalised message type decides what to forward.
        msg_type = str(message.get("type") or "").strip().lower()
        # Keystrokes: forward the input text to the spoke for this session.
        if msg_type == "shell_input":
            if not await relay_ws.send_to_spoke(tenant_id, spoke_id, {
                "type": "shell_input",
                "session_id": session_id,
                "data": str(message.get("data") or ""),
            }):
                raise RuntimeError("Spoke relay disconnected")
        # Terminal resize: forward the new size (default 80x24) so the remote PTY matches.
        elif msg_type == "shell_resize":
            cols = int(message.get("cols") or 80)
            rows = int(message.get("rows") or 24)
            if not await relay_ws.send_to_spoke(tenant_id, spoke_id, {
                "type": "shell_resize",
                "session_id": session_id,
                "cols": cols,
                "rows": rows,
            }):
                raise RuntimeError("Spoke relay disconnected")


async def _spoke_to_browser_shell(websocket: WebSocket, queue: asyncio.Queue, session_id: str) -> None:
    """Forward shell output from the spoke's queue to the browser until the shell exits."""
    # Wait for the next message the spoke sent for this session.
    while True:
        message = await queue.get()
        # Ignore anything malformed or meant for another session.
        if not isinstance(message, dict) or message.get("session_id") != session_id:
            continue
        # Pass the message straight to the browser terminal.
        await websocket.send_json(message)
        # Stop once the remote shell has exited.
        if str(message.get("type") or "") == "shell_exit":
            return


async def _request_vnc_proxy(
    *,
    proxmox_host: str,
    proxmox_token: str,
    node: str,
    vmid: int,
    vmtype: str,
) -> dict[str, Any]:
    """Ask a Proxmox API directly for a VNC proxy ticket and port for a VM.

    Note: this direct path is not used by the relay endpoints below, which go via the spoke.
    """
    # Proxmox vncproxy endpoint for the VM; the node name is URL-encoded.
    path = f"/api2/json/nodes/{urllib.parse.quote(node, safe='')}/{vmtype}/{vmid}/vncproxy"
    # Proxmox API listens on port 8006.
    url = f"https://{proxmox_host}:8006{path}"
    # Proxmox normally uses a self-signed cert, so verification is off; request websocket mode.
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        response = await client.post(
            url,
            headers=_proxmox_headers(proxmox_token),
            data={"websocket": 1},
        )
    # API errors become 502 Bad Gateway with Proxmox's message.
    if not response.is_success:
        raise HTTPException(status_code=502, detail=_proxmox_error_message(response))
    # Parse the JSON reply; unparsable replies are a 502.
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from Proxmox vncproxy") from exc
    # The useful values live under "data".
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Missing Proxmox vncproxy response data")
    # A ticket and port are both needed to open the VNC connection.
    ticket = str(data.get("ticket") or "").strip()
    port = data.get("port")
    if not ticket or port is None:
        raise HTTPException(status_code=502, detail="Proxmox vncproxy did not return ticket and port")
    return {"ticket": ticket, "port": int(port)}


async def _browser_to_proxmox(websocket: WebSocket, upstream) -> None:
    """Copy browser WebSocket messages (binary or text) to an upstream Proxmox socket."""
    # Read raw ASGI messages so both binary and text frames are handled.
    while True:
        message = await websocket.receive()
        message_type = message.get("type")
        # Browser closed: raise so the caller tears down the relay.
        if message_type == "websocket.disconnect":
            raise WebSocketDisconnect(code=message.get("code", 1000))
        # Skip anything that is not a data message.
        if message_type != "websocket.receive":
            continue
        # Forward binary frames as bytes, otherwise as text.
        if message.get("bytes") is not None:
            await upstream.send(message["bytes"])
        elif message.get("text") is not None:
            await upstream.send(message["text"])


async def _proxmox_to_browser(upstream, websocket: WebSocket) -> None:
    """Copy upstream Proxmox messages to the browser, keeping binary vs text framing."""
    # Wait for the next upstream message and send it with the same type.
    while True:
        message = await upstream.recv()
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


@router.get("/console", response_class=HTMLResponse, include_in_schema=False)
async def vnc_console_page(session_id: str = Query(...), token: str = Query(...)):
    """Serve the noVNC console page for a given console session."""
    # Insert the session id and auth token into the page template.
    html = _VNC_PAGE_HTML.replace("__SESSION_ID__", session_id).replace("__AUTH_TOKEN__", token)
    # Return it as an HTML response.
    return HTMLResponse(content=html)


# Self-contained noVNC page; the placeholders are replaced per request.
_VNC_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VM Console</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { width: 100%; height: 100%; background: #1a1a2e; overflow: hidden; }
    #toolbar {
      display: flex; align-items: center; gap: 10px;
      padding: 6px 12px; background: #16213e; border-bottom: 1px solid #333;
      color: #ccc; font-family: sans-serif; font-size: 13px;
    }
    #toolbar button {
      padding: 4px 12px; border: 1px solid #555; border-radius: 4px;
      background: #0f3460; color: #eee; cursor: pointer; font-size: 12px;
    }
    #toolbar button:hover { background: #1a5276; }
    #status { margin-left: auto; font-size: 12px; }
    #status.connected { color: #2ecc71; }
    #status.disconnected { color: #e74c3c; }
    #status.connecting { color: #f39c12; }
    #screen { width: 100%; height: calc(100vh - 38px); }
    #screen canvas { width: 100% !important; height: 100% !important; }
  </style>
</head>
<body>
  <div id="toolbar">
    <strong>VM Console</strong>
    <button onclick="sendCtrlAltDel()">Ctrl+Alt+Del</button>
    <button onclick="toggleClipboard()">Clipboard</button>
    <button onclick="rfb && rfb.sendKey(0xFFE9, 'AltLeft', true); rfb && rfb.sendKey(0xFFE9, 'AltLeft', false);" style="display:none" id="btn-extra"></button>
    <span id="status" class="connecting">Connecting…</span>
  </div>
  <div id="screen"></div>
  <script type="module">
    import RFB from 'https://cdn.jsdelivr.net/npm/@novnc/novnc@1.4.0/core/rfb.js';

    const sessionId = '__SESSION_ID__';
    const authToken = '__AUTH_TOKEN__';
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = proto + '//' + location.host + '/ws/console/' + sessionId + '?token=' + encodeURIComponent(authToken);

    const statusEl = document.getElementById('status');
    let rfb;

    function setStatus(msg, cls) {
      statusEl.textContent = msg;
      statusEl.className = cls;
    }

    try {
      rfb = new RFB(document.getElementById('screen'), wsUrl, {
        credentials: { password: '' },
      });

      rfb.scaleViewport = true;
      rfb.resizeSession = false;

      rfb.addEventListener('connect', () => setStatus('Connected', 'connected'));
      rfb.addEventListener('disconnect', (e) => {
        const reason = e.detail?.reason || 'Connection closed';
        setStatus('Disconnected: ' + reason, 'disconnected');
      });
      rfb.addEventListener('credentialsrequired', () => {
        const pass = prompt('VNC Password:') || '';
        rfb.sendCredentials({ password: pass });
      });
      rfb.addEventListener('securityfailure', (e) => {
        setStatus('Security failure: ' + (e.detail?.reason || 'unknown'), 'disconnected');
      });
    } catch (err) {
      setStatus('Error: ' + err.message, 'disconnected');
    }

    window.rfb = rfb;
    window.sendCtrlAltDel = () => rfb && rfb.sendCtrlAltDel();
    window.toggleClipboard = () => {
      if (!rfb) return;
      if (document.fullscreenElement) document.exitFullscreen();
      else document.getElementById('screen').requestFullscreen().catch(() => {});
    };
  </script>
</body>
</html>"""


@router.post("/api/{tenant_id}/spokes/{spoke_id}/console/{vmid}")
async def create_console_session(
    tenant_id: str,
    spoke_id: str,
    vmid: int,
    vmtype: str = Query("qemu"),
    current_user: User = Depends(auth.get_current_user),
):
    """Start a VNC console session for a VM via the spoke and return a short-lived session id.

    The browser then opens /console?session_id=... which connects to /ws/console/{session_id}.
    """
    # Remote control requires explicit tenant membership.
    auth.require_tenant_member(tenant_id, current_user)
    # Drop expired sessions while we are here.
    _cleanup_sessions()
    # The spoke must exist and be approved.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        raise HTTPException(status_code=404, detail="Spoke not found")
    if spoke.status != "approved":
        raise HTTPException(status_code=409, detail="Spoke is not approved")

    # Only QEMU VMs and LXC containers have consoles.
    normalized_vmtype = str(vmtype or "qemu").strip().lower()
    if normalized_vmtype not in {"qemu", "lxc"}:
        raise HTTPException(status_code=400, detail="vmtype must be qemu or lxc")

    # Unique id to match the spoke's reply, with a queue registered to receive it.
    request_id = str(uuid.uuid4())
    queue = relay_ws.register_vnc_session(request_id)
    # Always unregister the reply queue afterwards (see finally).
    try:
        # Ask the spoke to set up a VNC proxy to this VM.
        sent = await relay_ws.send_to_spoke(tenant_id, spoke_id, {
            "type": "vnc_proxy_request",
            "request_id": request_id,
            "vmid": vmid,
            "vmtype": normalized_vmtype,
        })
        # Spoke not connected.
        if not sent:
            raise HTTPException(status_code=502, detail="Spoke relay is offline")
        # Wait up to 15 seconds for the spoke's reply.
        try:
            response = await asyncio.wait_for(queue.get(), timeout=15.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Spoke did not respond to VNC request in time")
        # The spoke reported a failure setting up the proxy.
        if response.get("type") == "vnc_proxy_error":
            raise HTTPException(status_code=502, detail=response.get("error", "VNC proxy failed on spoke"))
    finally:
        relay_ws.unregister_vnc_session(request_id)

    # Store a session the browser can connect to within SESSION_TTL seconds.
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "tenant_id": tenant_id,
        "spoke_id": spoke_id,
        "request_id": request_id,
        "expires": time.time() + SESSION_TTL,
    }
    # Return the session id and how long it is valid.
    return {"session_id": session_id, "expires_in": SESSION_TTL}


@router.websocket("/ws/console/{session_id}")
async def console_websocket(websocket: WebSocket, session_id: str, token: str = Query(...)):
    """Relay a browser noVNC WebSocket to the spoke's VNC proxy for an existing console session."""
    # Look up the session; reject unknown or expired ones with close code 4404.
    _cleanup_sessions()
    session = _sessions.get(session_id)
    if not session or session.get("expires", 0) < time.time():
        await websocket.close(code=4404, reason="Invalid or expired console session")
        return

    # Validate the token from the query string and tenant membership.
    try:
        user = auth.decode_access_token(str(token or "").strip())
        auth.require_tenant_member(session["tenant_id"], user)
    # Map 401 to close code 4401 and anything else (403) to 4403.
    except HTTPException as exc:
        code = 4401 if exc.status_code == 401 else 4403
        await websocket.close(code=code, reason=str(exc.detail))
        return

    # Sessions are single-use: remove it and keep its ids locally.
    _sessions.pop(session_id, None)
    request_id = session["request_id"]
    tenant_id = session["tenant_id"]
    spoke_id = session["spoke_id"]

    # Accept the browser connection.
    await websocket.accept()

    # Queue that receives VNC frames from the spoke for this request.
    frame_queue = relay_ws.register_vnc_session(request_id)
    # Relay tasks, kept so the finally block can cancel them.
    relay_tasks: list[asyncio.Task] = []
    try:
        async def browser_to_spoke() -> None:
            """Send browser VNC data to the spoke, base64-encoded inside JSON messages."""
            while True:
                # Read raw ASGI messages so both binary and text frames are handled.
                msg = await websocket.receive()
                msg_type = msg.get("type")
                # Browser closed: end the relay.
                if msg_type == "websocket.disconnect":
                    raise WebSocketDisconnect(code=msg.get("code", 1000))
                # Skip non-data messages.
                if msg_type != "websocket.receive":
                    continue
                # Use the binary payload, or encode text to bytes.
                raw = msg.get("bytes") or (msg.get("text") or "").encode()
                # Forward to the spoke tagged with the request id.
                await relay_ws.send_to_spoke(tenant_id, spoke_id, {
                    "type": "vnc_frame_to_proxmox",
                    "request_id": request_id,
                    "data": base64.b64encode(raw).decode(),
                })

        async def spoke_to_browser() -> None:
            """Send VNC frames from the spoke to the browser until the spoke signals disconnect."""
            while True:
                # Wait for the next frame from the spoke.
                frame_msg = await frame_queue.get()
                # Spoke closed the proxy or hit an error: end the relay.
                if frame_msg.get("type") in ("vnc_disconnect", "vnc_proxy_error"):
                    raise WebSocketDisconnect(code=1000)
                # Decode and send as a binary WebSocket frame.
                raw = base64.b64decode(frame_msg.get("data", ""))
                await websocket.send_bytes(raw)

        # Run both directions at the same time.
        relay_tasks = [
            asyncio.create_task(browser_to_spoke()),
            asyncio.create_task(spoke_to_browser()),
        ]
        # Stop as soon as either direction finishes, cancel the other, and wait for both to settle.
        done, pending = await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*relay_tasks, return_exceptions=True)
        # Re-raise unexpected errors; normal disconnects/cancellations are fine.
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    # HTTP errors propagate unchanged.
    except HTTPException:
        raise
    # Anything else is logged; the finally block cleans up.
    except Exception as exc:
        logger.warning("Console relay failed for session %s: %s", session_id, exc)
    finally:
        # Clean-up: drop the queue and tell the spoke to close its VNC proxy.
        relay_ws.unregister_vnc_session(request_id)
        await relay_ws.send_to_spoke(tenant_id, spoke_id, {
            "type": "vnc_disconnect",
            "request_id": request_id,
        })
        # Cancel any relay task still running and wait for them.
        for task in relay_tasks:
            if not task.done():
                task.cancel()
        if relay_tasks:
            await asyncio.gather(*relay_tasks, return_exceptions=True)
        # Close the browser socket if it is still open.
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close()


@router.websocket("/api/{tenant_id}/spokes/{spoke_id}/shell")
async def spoke_shell_ws(websocket: WebSocket, tenant_id: str, spoke_id: str, token: str = Query(...)):
    """Open an interactive shell on the spoke host and relay it to the browser terminal.

    Tenant admins only. Close codes: 4401 bad token, 4403 forbidden, 4404 unknown spoke,
    4409 unapproved spoke, 1011 relay/start-up failure.
    """
    # Token comes from the query string because browsers cannot set headers on WebSockets.
    try:
        current_user = auth.decode_access_token(str(token or "").strip())
        _require_tenant_admin(tenant_id, current_user)
    # Reject before accepting: 4401 for bad token, 4403 for insufficient rights.
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 4403, reason=str(exc.detail))
        return

    # The spoke must exist and be approved.
    spoke = store.get_spoke(tenant_id, spoke_id)
    if not spoke:
        await websocket.close(code=4404, reason="Spoke not found")
        return
    if spoke.status != "approved":
        await websocket.close(code=4409, reason="Spoke is not approved")
        return

    # New session id with a queue for the spoke's shell messages.
    session_id = str(uuid.uuid4())
    queue = relay_ws.register_shell_session(session_id)
    # Relay tasks and whether the browser side closed first (to tell the spoke).
    relay_tasks: list[asyncio.Task[Any]] = []
    browser_disconnected = False

    # Accept the browser connection.
    await websocket.accept()
    try:
        # Ask the spoke to start a shell for this session.
        sent = await relay_ws.send_to_spoke(tenant_id, spoke_id, {"type": "shell_start", "session_id": session_id})
        # Spoke offline: tell the terminal and close.
        if not sent:
            await websocket.send_json({"type": "shell_exit", "session_id": session_id, "exit_code": -1, "error": "Spoke relay is offline"})
            await _close_shell_websocket(websocket, 1011, "Spoke relay is offline")
            return

        # Wait for the spoke's start-up reply.
        try:
            started_message = await asyncio.wait_for(queue.get(), timeout=SHELL_START_TIMEOUT)
        # Timed out: tell the browser, ask the spoke to stop (best effort) and close.
        except asyncio.TimeoutError:
            await websocket.send_json({"type": "shell_exit", "session_id": session_id, "exit_code": -1, "error": "Timed out waiting for shell startup"})
            with contextlib.suppress(Exception):
                await relay_ws.send_to_spoke(tenant_id, spoke_id, {"type": "shell_exit", "session_id": session_id})
            await _close_shell_websocket(websocket, 1011, "Shell startup timeout")
            return

        # The reply must be a dict.
        if not isinstance(started_message, dict):
            raise RuntimeError("Invalid shell startup response")

        # Forward the start-up reply to the browser, then stop if it was not a success.
        started_type = str(started_message.get("type") or "")
        await websocket.send_json(started_message)
        if started_type != "shell_started":
            await _close_shell_websocket(websocket, 1011, str(started_message.get("error") or "Shell startup failed"))
            return

        # Relay both directions concurrently.
        relay_tasks = [
            asyncio.create_task(_browser_to_spoke_shell(websocket, tenant_id, spoke_id, session_id)),
            asyncio.create_task(_spoke_to_browser_shell(websocket, queue, session_id)),
        ]
        # Stop when either direction finishes; cancel the other and wait for both.
        done, pending = await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*relay_tasks, return_exceptions=True)
        # A browser disconnect is normal; other errors are re-raised.
        for task in done:
            exc = task.exception()
            if isinstance(exc, WebSocketDisconnect):
                browser_disconnected = True
                continue
            if exc and not isinstance(exc, asyncio.CancelledError):
                raise exc
    # Browser closed the socket.
    except WebSocketDisconnect:
        browser_disconnected = True
    # Unexpected failure: log it, tell the terminal (best effort) and close with 1011.
    except Exception as exc:
        logger.warning("Spoke shell relay failed for %s/%s session %s: %s", tenant_id, spoke_id, session_id, exc)
        if websocket.application_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "shell_exit", "session_id": session_id, "exit_code": -1, "error": str(exc)})
            await _close_shell_websocket(websocket, 1011, "Shell relay failed")
    finally:
        # Clean-up: drop the queue, cancel and await any running relay tasks.
        relay_ws.unregister_shell_session(session_id)
        for task in relay_tasks:
            if not task.done():
                task.cancel()
        if relay_tasks:
            await asyncio.gather(*relay_tasks, return_exceptions=True)
        # If the browser left first, tell the spoke to end the shell (best effort).
        if browser_disconnected:
            with contextlib.suppress(Exception):
                await relay_ws.send_to_spoke(tenant_id, spoke_id, {"type": "shell_exit", "session_id": session_id})
        # Close the browser socket if it is still open.
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close()
