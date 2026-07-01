"""client_api — the cs spoke's client-facing FastAPI surface (port 8080).

The spoke that owns the DHCP scope (``169.253.1.1/24``) is also the client API
gateway. This module builds the FastAPI app that simulation clients call:

  * ``/api/health``, ``/api/kill-switch``        — bootstrap / state
  * ``POST /api/status``                          — client → spoke status beacon
  * ``/api/client/key``                           — shared PSK agents fetch first
  * ``/api/config``(+``/overrides``/``/parsed``)  — simulation.conf delivery
  * ``/api/scripts/{platform}/...``               — client script distribution
  * ``/api/clients`` + ``/api/clients/{h}/control`` — registry / per-host overrides
  * ``/api/commands`` / ``/api/inbox``(/ack)      — command queue (HTTP fallback)
  * ``ws://…/ws/client``                           — primary agent channel
  * ``/status`` / ``/simulate/trigger`` / ``/config`` / ``/version`` — mgmt

Every route is a thin wrapper over the backing modules already wired onto
``CSSpoke`` (``engine`` / ``queue`` / ``settings`` / ``registry`` / ``sim_config``)
— the same modules ``CSSpoke.handle_command`` drives, so the spoke is drivable
identically from an LM hub command or an HTTP/WS client. This mirrors the
webui-spoke ``server.py`` client surface but is headless (no browser UI, no
proxmox-agent socket, no ACME/LDAP/RADIUS — those live in the LM hub).

Auth: a shared ``client_api_key`` (``CSSettings``, default empty = open). When
non-empty, the WS socket and the mutating/inbox HTTP routes require it (header
``X-Client-Key`` or ``?api_key=``); ``secrets.compare_digest`` is used. The t3
agent sends no key, so empty=open lets it connect out of the box; the linux
agent fetches ``/api/client/key`` first when a key is set.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
# Response classes are canonically starlette's; fastapi re-exports them in newer
# versions but not all, so import from the source for cross-version safety.
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

import sim_config

logger = logging.getLogger("CSClientAPI")

# lm-spoke/src/client_api.py → src → lm-spoke → <repo root>
REPO = Path(__file__).resolve().parent.parent.parent
CONFIGS_DIR = REPO / "configs"
CLIENTS_DIR = REPO / "clients"
_VALID_PLATFORMS = ("linux", "windows", "t3")

# hostname → WebSocket, for live command push to connected agents.
client_ws_connections: Dict[str, WebSocket] = {}


# ── auth ─────────────────────────────────────────────────────────────────────
def _client_key(spoke) -> str:
    return str(spoke.settings.get("client_api_key", "") or "")


def _key_ok(spoke, provided: Optional[str]) -> bool:
    """Empty key = open. Otherwise constant-time compare."""
    key = _client_key(spoke)
    if not key:
        return True
    return secrets.compare_digest(str(provided or ""), key)


def _require_key_dep(spoke):
    """FastAPI dependency: reject 401 when a key is set and the request lacks it."""
    def _dep(request: Request) -> None:
        if not _client_key(spoke):
            return
        provided = request.headers.get("x-client-key") or request.query_params.get("api_key")
        if not _key_ok(spoke, provided):
            raise HTTPException(status_code=401, detail="invalid client api key")
    return _dep


# ── scripts path resolution (module-level so the traversal guard is testable) ─
def resolve_script_path(platform: str, filename: str) -> Path:
    """Resolve ``clients/{platform}/{filename}`` with a path-traversal guard.

    Returns the absolute file Path. Raises ``HTTPException(404)`` when the
    platform is unknown, the resolved file escapes the platform directory, or
    the file does not exist.
    """
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
    sd = (CLIENTS_DIR / platform).resolve()
    candidate = (sd / filename).resolve()
    try:
        candidate.relative_to(sd)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return candidate


# ── live command push to a connected agent ───────────────────────────────────
async def push_pending(spoke, hostname: str) -> bool:
    """Poll the inbox for *hostname* and, if it has a live WS connection, push
    any pending commands to it immediately. Called from ``CS_QUEUE_COMMAND`` so a
    command queued by the hub reaches a connected agent without waiting for the
    agent's next ``sync``. Returns True if a frame was sent."""
    hn = str(hostname or "").strip()
    if not hn:
        return False
    ws = client_ws_connections.get(hn)
    if ws is None:
        return False
    try:
        res = await spoke.queue.poll_agent_inbox(hn)
        cmds = res.get("commands") or []
        if cmds:
            await ws.send_json({"type": "commands", "commands": cmds})
            return True
    except Exception as exc:  # noqa: BLE001 — don't kill the enqueue path
        logger.debug("push_pending(%s) failed: %s", hn, exc)
    return False


# ── app builder ──────────────────────────────────────────────────────────────
def build_client_api_app(spoke) -> FastAPI:
    app = FastAPI(title="cs spoke client API", docs_url=None, redoc_url=None)
    require_key = _require_key_dep(spoke)

    # ── health / kill switch ───────────────────────────────────────────────
    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": spoke.get_version(),
            "clients": spoke.registry.count() if spoke.registry is not None else 0,
            "repo_synced": True,
            "repo_error": None,
        }

    @app.get("/api/kill-switch")
    async def api_kill_switch() -> PlainTextResponse:
        return PlainTextResponse("on" if spoke.engine.kill_switch_active() else "off")

    # ── status beacon (client → spoke) ─────────────────────────────────────
    @app.post("/api/status")
    async def api_status(body: Dict[str, Any]) -> Dict[str, Any]:
        hostname = str(body.get("hostname") or "").strip()
        entry = await spoke.registry.apply_status(hostname or "__unknown__", body)
        return {"status": "ok", "client": entry.get("hostname"),
                "throttle_interval": None}

    # ── client key (auth bootstrap) ────────────────────────────────────────
    @app.get("/api/client/key")
    async def api_client_key() -> Dict[str, str]:
        return {"client_api_key": _client_key(spoke)}

    # ── config delivery ────────────────────────────────────────────────────
    @app.get("/api/config")
    async def api_config(hostname: str = Query("")) -> PlainTextResponse:
        sim_conf, _user = sim_config.load_configs(CONFIGS_DIR)
        overrides: Dict[str, str] = {}
        if hostname and spoke.registry is not None:
            entry = spoke.registry.get(hostname)
            if entry and isinstance(entry.get("overrides"), dict):
                overrides = {str(k): str(v) for k, v in entry["overrides"].items()}
        text = sim_config.render_ini_for_client(sim_conf, hostname, overrides or None)
        return PlainTextResponse(text)

    @app.get("/api/config/overrides")
    async def api_config_overrides() -> PlainTextResponse:
        path = CONFIGS_DIR / "user-overrides.conf"
        return PlainTextResponse(path.read_text(encoding="utf-8") if path.exists() else "")

    @app.get("/api/config/parsed")
    async def api_config_parsed() -> JSONResponse:
        parser = sim_config.load_ini(CONFIGS_DIR / "simulation.conf")
        out: Dict[str, Dict[str, str]] = {}
        for section in parser.sections():
            out[section] = {k: v for k, v in parser.items(section)}
        return JSONResponse(out)

    # ── scripts ────────────────────────────────────────────────────────────
    def _scripts_dir(platform: str) -> Path:
        if platform not in _VALID_PLATFORMS:
            raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
        return (CLIENTS_DIR / platform).resolve()

    @app.get("/api/scripts/list")
    async def api_scripts_list(platform: str = Query(...)) -> Dict[str, Any]:
        sd = _scripts_dir(platform)
        if not sd.is_dir():
            return {"platform": platform, "scripts": []}
        scripts = sorted(p.name for p in sd.iterdir() if p.is_file())
        return {"platform": platform, "scripts": scripts}

    @app.get("/api/scripts/{platform}/{filename:path}")
    async def api_scripts_get(platform: str, filename: str) -> FileResponse:
        return FileResponse(str(resolve_script_path(platform, filename)))

    # ── registry / control ─────────────────────────────────────────────────
    @app.get("/api/clients")
    async def api_clients() -> Dict[str, Any]:
        return spoke.registry.get_all() if spoke.registry is not None else {}

    @app.post("/api/clients/{hostname}/control", dependencies=[Depends(require_key)])
    async def api_clients_control(hostname: str, body: Dict[str, Any]) -> Dict[str, Any]:
        overrides = body.get("overrides", {})
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=400, detail="'overrides' must be an object")
        entry = await spoke.registry.set_overrides(hostname, overrides)
        return {"status": "ok", "client": entry}

    @app.delete("/api/clients/{hostname}/control", dependencies=[Depends(require_key)])
    async def api_clients_control_delete(hostname: str) -> Dict[str, Any]:
        await spoke.registry.clear_overrides(hostname)
        return {"status": "ok"}

    # ── command queue (HTTP fallback / admin) ──────────────────────────────
    @app.post("/api/commands", dependencies=[Depends(require_key)])
    async def api_commands_post(body: Dict[str, Any]) -> Dict[str, Any]:
        target = str(body.get("target") or "proxmox")
        action = str(body.get("action") or "").strip()
        if not action:
            raise HTTPException(status_code=400, detail="missing 'action'")
        try:
            res = await spoke.queue.enqueue(target, action,
                                             body.get("args") or {},
                                             command_type=body.get("type"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Live-deliver to a connected agent if this command targets one.
        await push_pending(spoke, target)
        return res

    @app.get("/api/commands", dependencies=[Depends(require_key)])
    async def api_commands_list() -> Dict[str, Any]:
        return {"commands": await spoke.queue.list_commands()}

    @app.get("/api/inbox", dependencies=[Depends(require_key)])
    async def api_inbox(hostname: str = Query(...)) -> Dict[str, Any]:
        return await spoke.queue.poll_agent_inbox(hostname)

    @app.post("/api/inbox/ack", dependencies=[Depends(require_key)])
    async def api_inbox_ack(body: Dict[str, Any]) -> Dict[str, Any]:
        res = await spoke.queue.ack_command(body.get("id"), body.get("status"),
                                             body.get("message"), body.get("result"))
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail=res.get("message", "ack failed"))
        return res

    # ── WebSocket — primary agent channel ──────────────────────────────────
    @app.websocket("/ws/client")
    async def ws_client(ws: WebSocket,
                        hostname: str = Query(""),
                        platform: str = Query(""),
                        api_key: Optional[str] = Query(None)) -> None:
        # Auth: empty key = open; else compare_digest, close 4403 on mismatch.
        if not _key_ok(spoke, api_key):
            await ws.close(code=4403)
            return

        hn = str(hostname or "").strip() or "__unknown__"
        await ws.accept()
        client_ws_connections[hn] = ws
        logger.info("client WS connected: %s (%s)", hn, platform or "?")
        try:
            await ws.send_json({"type": "hello", "hostname": hn,
                                "version": spoke.get_version()})
            # Push any pending commands immediately (marks them delivered).
            await push_pending(spoke, hn)

            while True:
                msg = await ws.receive_json()
                mtype = str(msg.get("type") or "").lower()
                if mtype == "status":
                    await spoke.registry.apply_status(hn, msg.get("payload") or {})
                    await ws.send_json({"type": "status_ack"})
                elif mtype == "ack":
                    p = msg.get("payload") or {}
                    res = await spoke.queue.ack_command(
                        p.get("id"), p.get("status"),
                        p.get("message"), p.get("result"))
                    await ws.send_json({"type": "ack_ok", "id": p.get("id"),
                                        "ok": bool(res.get("ok"))})
                elif mtype == "sync":
                    await push_pending(spoke, hn)
                elif mtype == "ping":
                    await ws.send_json({"type": "pong"})
                # unknown types ignored (agents only act on "commands")
        except WebSocketDisconnect:
            logger.info("client WS disconnected: %s", hn)
        except Exception as exc:  # noqa: BLE001
            logger.debug("client WS error (%s): %s", hn, exc)
        finally:
            if client_ws_connections.get(hn) is ws:
                client_ws_connections.pop(hn, None)

    # ── mgmt (kept from standalone, folded in so standalone == hub) ────────
    @app.get("/status")
    async def mgmt_status() -> Dict[str, Any]:
        return spoke.engine.get_current_state()

    @app.post("/simulate/trigger")
    async def mgmt_trigger() -> Dict[str, Any]:
        return await spoke.engine.run_iteration()

    @app.post("/config")
    async def mgmt_config_post(body: Dict[str, Any]) -> Dict[str, Any]:
        spoke.engine.update_config(body)
        return spoke.engine.get_current_state()

    @app.get("/config")
    async def mgmt_config_get() -> Dict[str, Any]:
        return spoke.engine.get_current_state()

    @app.get("/version")
    async def mgmt_version() -> Dict[str, str]:
        return {"version": spoke.get_version()}

    return app