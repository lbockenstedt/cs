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

import logging
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles
# Response classes are canonically starlette's; fastapi re-exports them in newer
# versions but not all, so import from the source for cross-version safety.
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

import sim_config
import resource_pressure

# Client's normal status-report interval (s); throttle scales UP from here.
_CLIENT_BASE_INTERVAL_S = 15.0


def _client_throttle_interval(spoke) -> Optional[int]:
    """Back-pressure: when the Proxmox host the sim VMs run on is under load,
    tell clients to report LESS often so the loaded host + spoke can recover.

    Derives pressure from the freshest per-host node CPU/mem the pxmx agent
    already relays (no extra agent plumbing) via the shared resource_pressure
    scorer, and scales the client report interval. Returns an int > base when
    throttling, else None (calm → clients keep their normal cadence). Best-effort:
    None on any error."""
    try:
        deploy = getattr(spoke, "deploy", None)
        states = getattr(deploy, "proxmox_states", None) or {}
        cpu = mem = None
        for st in states.values():
            node = (st or {}).get("node") or {}
            c = node.get("cpu_percent")
            if c is not None:
                cpu = max(cpu or 0.0, float(c))
            mu, mt = node.get("mem_used_kb"), node.get("mem_total_kb")
            if mu is not None and mt:
                try:
                    mem = max(mem or 0.0, float(mu) / float(mt) * 100.0)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        pressure = resource_pressure.pressure_score(cpu_pct=cpu, mem_pct=mem)
        interval = resource_pressure.throttle_interval(_CLIENT_BASE_INTERVAL_S, pressure)
        return int(round(interval)) if interval > _CLIENT_BASE_INTERVAL_S else None
    except Exception:  # noqa: BLE001
        return None
from local_ui_routes import build_local_ui_router

logger = logging.getLogger("CSClientAPI")

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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


def _exclusive_sim_ids() -> set:
    """The sim_ids that are EXCLUSIVE (``multi_capable=False``) — a failure sim
    that monopolizes its client (dhcp_fail, dns_fail, assoc_fail, auth_fail,
    ssidpw_fail, port_flap). Per ``SIM_META`` in ``sim_quota``."""
    try:
        from sim_quota import SIM_META
    except Exception:  # noqa: BLE001
        return set()
    return {s for s, m in SIM_META.items() if not bool(m.get("multi_capable"))}


def _exclusive_sim_active(overrides: Dict[str, str], user_conf, uname: str) -> bool:
    """An EXCLUSIVE sim is ON for this client — in the engine/registry override
    (the spoke's quota-engine assignment, e.g. dhcp_fail) OR a human per-user
    pin in user-overrides.conf ``[uname]``. An exclusive sim monopolizes the
    client (no gateway → no other sim can run), so the client-side ambient
    traffic pick must NOT stack a shareable sim onto it. This is the
    ``multi_capable`` contract the quota engine already enforces for quota
    sims; the ambient pick is client-side, so the spoke suppresses it here
    (serve-time) rather than hard-coding ``if dhcp_fail`` in the client."""
    excl = _exclusive_sim_ids()
    if not excl:
        return False
    for sim_id in excl:
        if str(overrides.get(sim_id, "")).strip().lower() == "on":
            return True
    if user_conf is not None and uname and user_conf.has_section(uname):
        for sim_id in excl:
            if str(user_conf.get(uname, sim_id, fallback="")).strip().lower() == "on":
                return True
    return False


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
    """Build the cs client-API FastAPI app — the single route layer used by
    BOTH hub mode (``CSControlPlane.run``) and standalone (``run_standalone_mode``).

    Sim VMs phone home here: ``GET /api/config?hostname=`` (effective profile =
    bucket + persisted overrides + any live demo flags), ``POST /api/status``
    (registry upsert), ``ws /ws/client`` (live command push), plus the
    operator surface (kill switch, clients/control panel, commands, inbox,
    script download). The local dashboard at ``/`` (``local_ui_routes.py``) and
    ``/static/*`` are mounted here too. A client-API key (``_require_key_dep``)
    gates mutating routes when set; absent = open (the t3 agent path)."""
    app = FastAPI(title="cs spoke client API", docs_url=None, redoc_url=None)
    require_key = _require_key_dep(spoke)

    # ── health / kill switch ───────────────────────────────────────────────
    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "spoke_id": spoke.spoke_id,
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
        # Back-pressure: return a throttle interval (s) when the Proxmox host is
        # under load so the client's HTTP heartbeat backs off (dashboard.sh reads
        # throttle_interval). None = normal cadence. Was hard-coded None (dead).
        return {"status": "ok", "client": entry.get("hostname"),
                "throttle_interval": _client_throttle_interval(spoke)}

    # ── client key (auth bootstrap) ────────────────────────────────────────
    @app.get("/api/client/key")
    async def api_client_key() -> Dict[str, str]:
        return {"client_api_key": _client_key(spoke)}

    # ── config delivery ────────────────────────────────────────────────────
    @app.get("/api/config")
    async def api_config(hostname: str = Query("")) -> PlainTextResponse:
        sim_conf, user_conf = sim_config.load_configs(CONFIGS_DIR)
        # Merge the file-based user-overrides.conf [username] sections INTO the
        # served config. The client parses ONLY simulation.conf, and its
        # apply_override() reads per-user overrides via `get_value $username`
        # from those [username] sections — so they must be present in the
        # delivered config. The port loaded user-overrides.conf (user_conf) but
        # never included it in the rendered output, silently dropping every
        # file-based per-user override (site/phy/sim_id/ssid/…). Later keys win
        # (user-overrides is authoritative over the base bucket).
        for section in user_conf.sections():
            if not sim_conf.has_section(section):
                sim_conf.add_section(section)
            for k, v in user_conf.items(section):
                sim_conf.set(section, k, v)
        overrides: Dict[str, str] = {}
        if hostname and spoke.registry is not None:
            entry = spoke.registry.get(hostname)
            if entry and isinstance(entry.get("overrides"), dict):
                overrides = {str(k): str(v) for k, v in entry["overrides"].items()}
        # Layer any active demo-scenario flags on top (ephemeral, demo wins).
        # Demos live in-memory on the spoke's DemoManager; they never mutate the
        # persisted registry overrides.
        demo = getattr(spoke, "demo", None)
        if demo is not None and hostname:
            overrides.update(demo.effective_flags(hostname))
        # ── Pool / SSID serve-time injection (design doc §4, §7, §12) ──────────
        # Per-server binding: if the client's HOSTING PXMX server is pinned to a
        # REAL site (RF chamber), inject that wsite so it's frozen and the client
        # joins the reachable SSID. If the server is in the tenant-wide pool
        # (value "Tenant-Wide Pool" or unmapped), leave wsite to placement / the
        # bucket. Engine placement wsite (in the registry overrides) and a human
        # [username] wsite still win.
        _TENANT_POOL = "Tenant-Wide Pool"
        def _physical_site(host: str) -> str:
            try:
                deploy = getattr(spoke, "deploy", None)
                n2h = deploy.name_to_host() if deploy is not None else {}
                srv = n2h.get(str(host).strip().lower())
                if srv:
                    s = str(spoke.local_store.get_pxmx_site_map().get(srv) or "").strip()
                    return "" if s == _TENANT_POOL else s   # tenant pool = not physical
            except Exception:  # noqa: BLE001
                pass
            return ""
        resolved_site = str(overrides.get("wsite") or "").strip()  # engine placement wins
        try:
            if hostname:
                phys = _physical_site(hostname)
                if phys:
                    overrides.setdefault("wsite", phys)   # engine wsite (if set) kept
                    resolved_site = resolved_site or phys
        except Exception:  # noqa: BLE001
            pass
        if not resolved_site and hostname:  # fall back to a human/bucket wsite
            _un = sim_config.username_for(hostname)
            if user_conf.has_section(_un):
                resolved_site = str(user_conf.get(_un, "wsite", fallback="") or "").strip()
        # Deliver the ambient random-pool policy for this site + the randomizable
        # sim set as [simulation] globals the client rotation reads (Slice 5).
        try:
            pool_map = spoke.local_store.get_random_pool()
            rand_sims = spoke.local_store.get_randomizable_sims()
            if not sim_conf.has_section("simulation"):
                sim_conf.add_section("simulation")
            pool_on = bool(pool_map.get(resolved_site, pool_map.get("*", False)))
            sim_conf.set("simulation", "random_pool", "on" if pool_on else "off")
            if rand_sims:
                sim_conf.set("simulation", "randomizable_sims",
                             " ".join(str(s) for s in rand_sims))
            # Ambient weighting (HUB mode). ambient_pct is the automatic uniform
            # weight; when the operator opts into steering, ambient_control=on and
            # the per-sim weights ride in the [ambient_weights] section.
            # Ambient distribution (see docs §7.1). Two-step model: ambient_pct is
            # the LEVEL (% of the fleet that is ambient-active); the per-sim
            # relative weights split those active clients (weight 3 → 3x the
            # clients of weight 1). Site load weight scales the LEVEL per site.
            base_pct = spoke.local_store.get_ambient_pct()
            control_on = spoke.local_store.get_ambient_control()
            sim_conf.set("simulation", "ambient_control",
                         "on" if control_on else "off")
            # multi_capable contract: an EXCLUSIVE sim (dhcp_fail / dns_fail /
            # assoc_fail / auth_fail / ssidpw_fail / port_flap) monopolizes this
            # client, so the client-side ambient traffic pick must NOT stack a
            # shareable sim onto it. Suppress ambient_pct for this client
            # (serve-time, driven by SIM_META.multi_capable) — the client's roll
            # gate `RANDOM % 100 < ambient_pct` is then always false → inactive.
            _uname = sim_config.username_for(hostname) if hostname else ""
            if _exclusive_sim_active(overrides, user_conf, _uname):
                sim_conf.set("simulation", "ambient_pct", "0")
            elif control_on:
                # Scale THIS client's level by its site's load weight (default 1),
                # so a site weighted 3 has 3x the ambient-active clients. Folded
                # here so the client just rolls the served level.
                site_w = spoke.local_store.get_ambient_site_weights()
                sfactor = site_w.get(resolved_site) or 1
                eff_level = max(0, min(100, int(round(base_pct * sfactor))))
                sim_conf.set("simulation", "ambient_pct", str(eff_level))
                # Serve the relative per-sim weights the client picks among.
                weights = spoke.local_store.get_ambient_weights()
                if weights:
                    if not sim_conf.has_section("ambient_weights"):
                        sim_conf.add_section("ambient_weights")
                    for _sim, _w in weights.items():
                        sim_conf.set("ambient_weights", str(_sim), str(_w))
            else:
                sim_conf.set("simulation", "ambient_pct", str(base_pct))
        except Exception:  # noqa: BLE001
            pass
        # Deliver the engine/registry overrides into the client's [username]
        # section — the layer simulation.sh's apply_override() resolves LAST, so
        # it WINS over the client's bucket no matter which bucket the client is
        # pinned to. render_ini_for_client (below) only bakes into the crc32 hash
        # bucket [sX]; a client moved off that bucket by a per-user simulation_id
        # override would never see a quota assignment. The [username] layer is
        # where the client authoritatively resolves every sim flag + wsite, so a
        # SimQuotaEngine assignment must land there to actually turn the sim on.
        #
        # This is DELIVERY-ONLY: it edits the in-memory served config, never the
        # on-disk user-overrides.conf — so quota assignments stay transient (no
        # GitHub sync) and self-clean the moment the engine releases (the next
        # render simply omits them). A human Model-A per-user override already in
        # user-overrides.conf [username] is preserved — the engine never fights a
        # human pin.
        if hostname and overrides:
            uname = sim_config.username_for(hostname)
            human_keys = (set(user_conf.options(uname))
                          if user_conf.has_section(uname) else set())
            if not sim_conf.has_section(uname):
                sim_conf.add_section(uname)
            for k, v in overrides.items():
                if k in human_keys:
                    continue  # human per-user override wins (Model A)
                sim_conf.set(uname, str(k), str(v))
        # HUB mode (web_server=on): the client ignores the s0–s9 buckets entirely
        # — the engine drives placement through the [username] override written
        # above, which simulation.sh resolves LAST. Strip the dead bucket sections
        # from the served config: a smaller payload at 5000-client scale and no way
        # a stale bucket flag can be read. The overrides already live in [username],
        # so skip the redundant crc32-[sX] bake too. STANDALONE (web_server=off)
        # keeps the buckets + bake exactly as before (GitHub-synced deployments
        # still resolve their sims from the bucket).
        ws_on = str(sim_conf.get("simulation", "web_server", fallback="") or "").strip().lower() == "on"
        if ws_on:
            for _b in (f"s{i}" for i in range(10)):
                if sim_conf.has_section(_b):
                    sim_conf.remove_section(_b)
            text = sim_config.render_ini_for_client(sim_conf, hostname, None)
        else:
            text = sim_config.render_ini_for_client(sim_conf, hostname, overrides or None)
        return PlainTextResponse(text)

    @app.get("/api/config/overrides")
    async def api_config_overrides() -> PlainTextResponse:
        path = CONFIGS_DIR / "user-overrides.conf"
        return PlainTextResponse(path.read_text(encoding="utf-8") if path.exists() else "")

    @app.get("/api/config/parsed")
    async def api_config_parsed() -> JSONResponse:
        parser = sim_config.load_ini(CONFIGS_DIR / "simulation.conf")
        return JSONResponse(sim_config.sections_dict(parser))

    # ── scripts ────────────────────────────────────────────────────────────
    def _scripts_dir(platform: str) -> Path:
        if platform not in _VALID_PLATFORMS:
            raise HTTPException(status_code=404, detail=f"unknown platform: {platform}")
        return (CLIENTS_DIR / platform).resolve()

    @app.get("/api/scripts/list")
    async def api_scripts_list(platform: str = Query(...)) -> List[str]:
        # MUST return a BARE JSON ARRAY (["agent.sh", ...]), NOT
        # {"platform":.., "scripts":[..]}. The linux update.sh
        # (tr -d '[]"' | tr ',') and windows update.ps1
        # (ConvertFrom-Json | foreach) both iterate the top-level array
        # directly; wrapping it in an object made them parse bogus tokens like
        # "{platform:linux" and fail EVERY script sync ("sync incomplete").
        # Matches the original webui-spoke endpoint (list[str]).
        sd = _scripts_dir(platform)
        if not sd.is_dir():
            return []
        return sorted(p.name for p in sd.iterdir() if p.is_file())

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
        # A (re)connecting client is a fresh pool member — nudge the SimQuotaEngine
        # so an under-filled quota can top up from it within seconds instead of
        # waiting for the 60s sweep. Fire-and-forget; guarded (standalone builds
        # may not wire a SimQuotaEngine onto the spoke).
        _sq = getattr(spoke, "_trigger_sim_quota_reconcile", None)
        if callable(_sq):
            try:
                _sq()
            except Exception as exc:  # noqa: BLE001
                logger.debug("client WS connect: sim-quota nudge failed: %s", exc)
        try:
            await ws.send_json({"type": "hello", "hostname": hn,
                                "version": spoke.get_version()})
            # Back-pressure: if the Proxmox host is loaded, tell the agent to
            # widen its WS send interval (agent.sh honours {type:throttle}). Sent
            # on connect + refreshed after each status report so it tracks load.
            _thr = _client_throttle_interval(spoke)
            if _thr:
                await ws.send_json({"type": "throttle", "interval": _thr})
            # Push any pending commands immediately (marks them delivered).
            await push_pending(spoke, hn)

            while True:
                msg = await ws.receive_json()
                mtype = str(msg.get("type") or "").lower()
                if mtype == "status":
                    await spoke.registry.apply_status(hn, msg.get("payload") or {})
                    await ws.send_json({"type": "status_ack"})
                    _thr = _client_throttle_interval(spoke)
                    if _thr:
                        await ws.send_json({"type": "throttle", "interval": _thr})
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
            # WS drop → the engine re-evaluates this runner's gap promptly (the
            # next sweep would catch it, but a dropped WS is a strong "going
            # down" signal). We do NOT mutate last_seen here — that field drives
            # the out-of-contact alerting + Clients view, and a transient WS
            # blip with HTTP beacons continuing must not flicker a client
            # offline. The engine's own _is_online (last_seen within 300s) still
            # gates whether a substitute is actually picked.
            _sq = getattr(spoke, "_trigger_sim_quota_reconcile", None)
            if callable(_sq):
                try:
                    _sq()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("client WS close: sim-quota nudge failed: %s", exc)

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

    # ── local standalone dashboard (Simulations/Clients — see local_ui_routes.py
    # module docstring) ──────────────────────────────────────────────────────
    # Available in both --standalone and hub-connected mode (same client API
    # surface either way, per run_standalone_mode's design) so a small
    # deployment gets a working local UI without needing an LM hub at all.
    app.include_router(build_local_ui_router(spoke))
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/")
        async def dashboard() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "dashboard.html"))

    return app