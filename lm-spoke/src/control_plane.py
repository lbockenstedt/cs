# ── Dependency self-heal (must run BEFORE the third-party imports below) ──────
# A skewed auto-update / partial install can leave the venv missing a declared
# dep (e.g. websockets) → hard crash at `import websockets` below, crash-looping
# the spoke under Restart=always. dep_guard is stdlib-only so it imports even
# when third-party deps are absent; it parses requirements.txt, find_spec-checks
# each top-level package, and runs `pip install -r` in this venv if any are
# missing. LM_DEP_GUARD_DISABLE=1 opts out. PYTHONPATH ($LM_DIR + $LM_DIR/core/src)
# resolves both `core.src.dep_guard` and the bare `dep_guard` fallback.
# PEP 563: make ALL annotations lazy strings (never evaluated at import) so a
# missing typing import in a signature can't crash the module on startup — the
# defect that crash-looped the cs fleet (see cs-telemetry-conditional-relay).
# Must be the first statement (comments above are fine).
from __future__ import annotations
import os as _os
import sys as _sys
try:
    from core.src.dep_guard import ensure_requirements as _ensure_requirements
except ImportError:  # lm core not on path as a package — bare module on core/src
    from dep_guard import ensure_requirements as _ensure_requirements
_req = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                     "requirements.txt")
_ensure_requirements(_req)
del _os, _sys, _ensure_requirements, _req

import asyncio
import uuid
import time
import ssl
import tempfile
import websockets
import logging
import argparse
import sys
import os
from pathlib import Path
from typing import Any, Dict, Optional
import uvicorn

# Sibling modules (simulation_engine, cs_spoke, sim_config, sim_primitives,
# data_models) live next to this file. When launched as `python -m
# src.control_plane` the module's own directory is NOT added to sys.path
# (only the cwd / PYTHONPATH entries are), so the bare sibling imports below
# raise ModuleNotFoundError and the spoke crash-loops. Put this file's
# directory on the front of sys.path so every sibling import resolves
# regardless of how the process is started (-m, direct script, or import).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cs_spoke import CSSpoke
from client_api import build_client_api_app
from client_rows import build_client_rows
try:
    from core.src.messaging.agent_hosting import AgentHostingControlPlane
except ImportError:
    from messaging.agent_hosting import AgentHostingControlPlane

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
configure_logging()
logger = logging.getLogger("CSControlPlane")

class CSControlPlane(AgentHostingControlPlane):
    """cs spoke control plane.

    Subclasses ``AgentHostingControlPlane`` (shared with pxmx) so it can host
    inbound pxmx agents directly in the split (per-module-LXC) topology — a
    cs-dialed agent connects to this spoke's ``/ws/agent`` and is managed via
    the cs WebUI. The listener starts only when ``LM_CS_AGENT_LISTENER=1``;
    ``install_cs.sh`` writes that env by DEFAULT (the agent listener is ON for
    a standalone cs spoke), and ``--no-agent-listener`` opts out for the rare
    all-in-one / relay-only cs spoke that must never bind ``:443`` (co-located
    cs agents then keep going through the hub ``/ws/agent`` byte-proxy → pxmx
    → ``CSBridgePoller`` path).
    """

    # cs-specific tuning of the mixin's class attrs.
    MODULE_TYPE = "simulation"
    AGENT_PORT_ENV = "LM_CS_AGENT_PORT"
    AGENT_LOOPBACK_ENV = "LM_CS_AGENT_LOOPBACK"
    AGENT_LISTENER_ENV = "LM_CS_AGENT_LISTENER"
    AGENT_CONFIG_PATH = "/etc/lm-cs-agent/config.json"
    # cs agent listener is opt-in (env-gated) so all-in-one stays relay-only.
    AGENT_LISTENER_OPT_IN = True
    AGENT_LOOPBACK_PORT = 8443
    AGENT_WSS_PORT = 443
    AGENT_FALLBACK_PORT = 8767

    def get_service_name(self) -> str:
        return "lm-cs"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None,
                 onboarding_psk: str = None, tenant_id_hint: str = None,
                 api_host: str = None, api_port: int = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url,
                         onboarding_psk=onboarding_psk, tenant_id_hint=tenant_id_hint)
        self.startup_config = config or {}
        # Client API listener (the spoke that owns the DHCP scope 169.253.1.1/24
        # is also the client API gateway on 169.253.1.1:8080). Bound 0.0.0.0 so
        # the listener lands on the DHCP NIC; configurable via CS_API_PORT/HOST.
        # NOTE: 8080 (not 8000) — the LM hub serves its own admin WebUI/API on
        # 0.0.0.0:8000, and in hub mode the cs spoke runs on the SAME box, so
        # binding 8000 here collided with the hub and broke the WebUI. The cs
        # client API takes 8080; sim clients reach it at 169.253.1.1:8080.
        self.api_host = api_host or os.getenv("CS_API_HOST", "0.0.0.0")
        try:
            self.api_port = int(api_port if api_port is not None
                                else os.getenv("CS_API_PORT", "8080"))
        except (TypeError, ValueError):
            self.api_port = 8080
        # Last client-simulation config the hub pushed for each agent (keyed by
        # agent_id), captured in CSSpoke's SET_AGENT_CONFIG handler. Re-pushed on
        # every agent (re)connect via _on_agent_registered so a restarted /
        # self-updated agent gets its config back from the spoke locally, without
        # waiting for the hub to re-send SET_AGENT_CONFIG through a possibly
        # backlogged link — the recurring cause of "provision loop not running"
        # after an agent restart. Complements the agent-side config persistence.
        self._agent_config_cache: Dict[str, Any] = {}
        # Local webui (8080 uvicorn) TLS cert paths, set by _apply_local_cert
        # when a le-issued cert is delivered via INSTALL_CERT. None until then.
        self._api_app = None
        self._api_server = None
        self._api_task = None
        # Set by an agent-frame ingest to WAKE the conditional relay loop
        # immediately, so a state change (VM deleting/recloning, a client going
        # stopped/started) relays in ~0.1s instead of waiting out the idle SLOW
        # interval. Created lazily on first use to avoid binding to a loop here.
        self._relay_wake: Optional[asyncio.Event] = None

    # ── local webui TLS (le cert distribution → this spoke's own dashboard) ───
    # The 8080 uvicorn server serves the local dashboard + client API. INSTALL_CERT
    # applies the le cert here too (pxmx servers AND the cs spoke's own webui):
    # validate → atomic-write → re-bind the server with ssl_certfile/ssl_keyfile
    # in-process (no restart/installer change). run() re-reads the persisted cert
    # at startup so a restart keeps HTTPS. Mirrors statuspage _apply_cert +
    # _ensure_web_server and the hub's _install_cert_on_hub validation.
    _TLS_DIR_ENV = "LM_CS_TLS_DIR"

    def _tls_dir(self) -> Path:
        """Where the local-webui fullchain/privkey live. Env-overridable; default
        is <lm-spoke>/data/tls — the spoke's own data dir, always spoke-writable
        (CSSpoke already persists settings/registry/queue there), so applying a
        cert needs no installer or service-user change."""
        d = os.getenv(self._TLS_DIR_ENV)
        if d:
            return Path(d)
        # control_plane.py is <lm-spoke>/src/ → parent.parent = <lm-spoke>/
        return Path(__file__).resolve().parent.parent / "data" / "tls"

    def _local_tls_paths(self):
        """Return (cert, key) paths if a local-webui cert is persisted on disk,
        else (None, None). Used by run() to bind HTTPS on startup."""
        d = self._tls_dir()
        cert, key = d / "fullchain.pem", d / "privkey.pem"
        if cert.is_file() and key.is_file():
            return str(cert), str(key)
        return None, None

    # The client API serves the ISOLATED sim-client segment (169.253.1.0/24),
    # where the linux/windows agents HARD-CODE http://169.253.1.1:8080. It must
    # never run HTTPS: a le cert delivered here (INSTALL_CERT, or le-module
    # testing) silently re-binds 8080 to TLS and every plain-http client then
    # gets "empty reply from server" — the client can't fetch config or beacon,
    # so it never comes online. Plaintext is the default; an operator who truly
    # wants TLS on the client webui sets LM_CS_ALLOW_LOCAL_CERT=1.
    _ALLOW_LOCAL_CERT_ENV = "LM_CS_ALLOW_LOCAL_CERT"

    def _local_cert_allowed(self) -> bool:
        return os.getenv(self._ALLOW_LOCAL_CERT_ENV) == "1"

    def _purge_local_cert(self) -> None:
        """Delete any persisted local-webui cert so the client API binds plaintext.
        Called on startup and whenever an INSTALL_CERT is refused, so a stray le
        cert can't linger and flip 8080 to HTTPS on the next restart."""
        d = self._tls_dir()
        for name in ("fullchain.pem", "privkey.pem"):
            p = d / name
            try:
                if p.exists():
                    p.unlink()
                    logger.warning(
                        "Removed local-webui cert %s — cs client API is "
                        "plaintext-only on the isolated sim segment", p)
            except OSError as exc:  # noqa: BLE001
                logger.warning("Could not remove local-webui cert %s: %s", p, exc)

    def _agent_listener_tls_paths(self):
        """Prefer the LE cert applied by ``_apply_local_cert`` (persisted under
        the TLS dir) for the 443 ``/ws/agent`` listener, falling back to the
        installer-provisioned ``LM_TLS_CERT`` / ``LM_TLS_KEY`` env. Without
        this the agent listener kept serving the old/self-signed cert (or
        plaintext) even after INSTALL_CERT applied a fresh LE cert to the 8080
        webui — so the agent→spoke leg couldn't be verified against LE even
        with ``LM_HUB_TLS_VERIFY=1``.

        Self-contained (does not call ``super()``): the base hook lives in the
        sibling lm ``core`` repo, which the cs spoke runs against at whatever
        version is deployed (``/opt/lm/core`` in prod, the sibling checkout in
        dev) — it may not yet have the hook. Inlining the env fallback keeps
        the override correct against any base version (and mirrors the base's
        own default exactly)."""
        cert, key = self._local_tls_paths()
        if cert and key:
            return cert, key
        cert = os.environ.get("LM_TLS_CERT", "").strip()
        key = os.environ.get("LM_TLS_KEY", "").strip()
        return cert, key

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    async def _rebind_api_server(self, ssl_certfile=None, ssl_keyfile=None) -> None:
        """Tear down the current 8080 uvicorn server and start a fresh one,
        optionally with TLS. Awaited so the old socket is released before the
        new bind (avoids EADDRINUSE). The app is reused (self._api_app)."""
        old = self._api_server
        old_task = self._api_task
        if old is not None:
            old.should_exit = True
        if old_task is not None:
            try:
                await asyncio.wait_for(old_task, timeout=5.0)
            except Exception:  # noqa: BLE001 - shutdown timeout/Cancel is fine
                pass
        kwargs = dict(host=self.api_host, port=self.api_port, log_config=None)
        if ssl_certfile and ssl_keyfile:
            kwargs["ssl_certfile"] = ssl_certfile
            kwargs["ssl_keyfile"] = ssl_keyfile
        self._api_server = uvicorn.Server(uvicorn.Config(self._api_app, **kwargs))
        self._api_task = asyncio.create_task(self._api_server.serve())

    async def _apply_local_cert(self, fullchain: str, privkey: str) -> Dict[str, Any]:
        """Apply a delivered TLS cert to this spoke's own local webui (8080).
        Validate in a throwaway ssl context first (fail-fast — a bad cert must
        not brick the listener), atomic-write fullchain+privkey, then re-bind
        the uvicorn server with HTTPS in-process."""
        # The cs client API is plaintext-only on the isolated sim segment (the
        # agents speak plain http). Refuse to apply a cert here unless explicitly
        # allowed, and scrub any that a prior push left behind. Return SUCCESS so
        # the hub/le doesn't flag the spoke as a cert-install failure.
        if not self._local_cert_allowed():
            self._purge_local_cert()
            logger.info("INSTALL_CERT ignored for cs client API — plaintext-only "
                        "on the isolated sim segment (set %s=1 to override)",
                        self._ALLOW_LOCAL_CERT_ENV)
            return {"status": "SUCCESS",
                    "message": "cs client API is plaintext-only; cert not applied"}
        if not (fullchain and privkey):
            return {"status": "ERROR", "message": "missing cert material"}
        # Validate before touching live files.
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as cf:
                cf.write(fullchain); cp = cf.name
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False) as kf:
                kf.write(privkey.encode("utf-8")); kp = kf.name
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cp, kp)
            finally:
                try: os.unlink(cp)
                except OSError: pass
                try: os.unlink(kp)
                except OSError: pass
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "message": f"invalid cert material: {exc}"}
        # Atomic write to the TLS dir.
        try:
            d = self._tls_dir()
            d.mkdir(parents=True, exist_ok=True)
            cert_p, key_p = d / "fullchain.pem", d / "privkey.pem"
            self._atomic_write_text(cert_p, fullchain)
            os.chmod(cert_p, 0o644)
            self._atomic_write_bytes(key_p, privkey.encode("utf-8"))
            os.chmod(key_p, 0o600)
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "message": f"write failed: {exc}"}
        # Re-bind the 8080 server with HTTPS.
        try:
            await self._rebind_api_server(str(cert_p), str(key_p))
        except Exception as exc:  # noqa: BLE001
            return {"status": "ERROR", "message": f"rebind failed: {exc}"}
        # Re-bind the /ws/agent listener (443) so it presents the new cert too
        # — run_agent_server reads the cert at serve-start, so without a rebind
        # the agent→spoke leg keeps serving the old cert after a renew. Connected
        # agents drop + reconnect (re-onboarded; agent_id stable). Best-effort:
        # a rebind failure must not mask the successful webui apply.
        try:
            await self._rebind_agent_server()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent listener TLS rebind failed: %s", exc)
        logger.info("Local webui TLS applied — re-bound https://%s:%s",
                    self.api_host, self.api_port)
        return {"status": "SUCCESS", "message": "local webui HTTPS applied"}

    async def _on_agent_registered(self, agent_id: str) -> None:
        """Re-push the agent's last-known client-simulation config the moment it
        (re)connects, so an agent restart / self-update re-enters CS mode (and
        auto-provisioning) without depending on the hub re-sending
        SET_AGENT_CONFIG. Fire-and-forget: this hook runs BEFORE the agent
        message loop starts, so awaiting send_to_agent's response here would
        just block until timeout — we only need the send to go out (the loop,
        now starting, resolves the response)."""
        cfg = self._agent_config_cache.get(agent_id)
        if cfg:
            asyncio.create_task(self._repush_agent_config(agent_id, cfg))

    async def _repush_agent_config(self, agent_id: str, cfg: Dict[str, Any]) -> None:
        try:
            await self.send_to_agent("UPDATE_CONFIG", cfg, agent_id=agent_id)
            logger.info(f"Re-pushed cached client-sim config to agent '{agent_id}'")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to re-push config to agent '{agent_id}': {e}")

    async def run(self):
        """Boot the cs spoke: register ``CSSpoke``, (conditionally) start the
        cs-dialed agent listener, start the demo TTL sweep + Aruba Central
        poller, launch the client-API uvicorn server, then enter the hub WS
        loop (``super().run()``). The client API is a long-lived task that
        survives hub reconnects; it is torn down in ``finally`` on shutdown."""
        logger.info(f"Starting CS (Client Simulator) -> {self.hub_url}")
        cs_spoke = CSSpoke(self.spoke_id, self.startup_config)
        # Wire the control-plane reference so CSSpoke's GET_AGENTS / SPOKE_RELAY
        # handlers can reach connected_agents / approve_pending_agent / send_to_agent
        # (mirrors ProxmoxSpoke(control_plane=self)).
        cs_spoke.control_plane = self
        self.register_module("cs", cs_spoke)
        # Start the agent listener when LM_CS_AGENT_LISTENER=1. install_cs.sh
        # writes that env by DEFAULT (standalone cs accepts cs-dialed pxmx
        # agents); --no-agent-listener opts out so an all-in-one / relay-only cs
        # spoke never binds :443 on the hub box.
        if self._agent_listener_enabled():
            self._start_agent_server_task()
            logger.info("CS agent listener enabled (cs-dialed agents accepted)")
        else:
            logger.info("CS agent listener disabled (relay-only; --no-agent-listener was passed)")
        # Start the demo-scenario TTL expiry sweep (no-op without a loop).
        cs_spoke.demo.start()
        # Start the Aruba Central poll loop (see central_poller.py). Runs
        # regardless of hub-connection — its output feeds both the local
        # dashboard's Simulations tab AND (via _cs_telemetry_relay_loop below)
        # the hub's Simulations tab when this spoke is hub-connected.
        cs_spoke.central_poller.start()
        # Start the Juniper Mist poll loop (twin of the Central loop above).
        # Same 5-min cadence; writes cs_spoke.mist_status in the central_status
        # shape so the hub/local Simulations tabs render Mist data identically.
        cs_spoke.mist_poller.start()
        # Start the SimQuotaEngine self-heal loop (reconciles client assignments
        # against the hub-pushed effective_sim_quotas every 60s; an immediate
        # reconcile also fires on each effective_sim_quotas push).
        if getattr(cs_spoke, "sim_quota_engine", None) is not None:
            cs_spoke.sim_quota_engine.start()
        # Start the GitHub config-branch pull loop (repo_sync.py): periodically
        # fetch + reset onto origin/<branch> when source=github + creds are set;
        # no-op otherwise. Survives hub reconnects like the loops above.
        if getattr(cs_spoke, "repo_sync", None) is not None:
            cs_spoke.repo_sync.start()
        # Start the client API server as a long-lived task that SURVIVES hub
        # reconnects (NOT via _create_spoke_tasks, which the base class tears
        # down per-connection). Server.serve() is awaitable (vs blocking
        # uvicorn.run), so it shares super().run()'s event loop — same pattern
        # as webui-spoke running the LM relay as a background task.
        app = build_client_api_app(cs_spoke)
        self._api_app = app  # retained so _apply_local_cert can re-bind with TLS
        # Client API is plaintext-only by default (isolated sim segment; agents
        # hard-code http://169.253.1.1:8080). Scrub any stray le cert so 8080
        # binds HTTP. Only when LM_CS_ALLOW_LOCAL_CERT=1 do we honor a persisted
        # cert and bind HTTPS on startup (see _apply_local_cert / _purge_local_cert).
        if self._local_cert_allowed():
            tls_cert, tls_key = self._local_tls_paths()
            _tls_kwargs = ({"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
                           if tls_cert and tls_key else {})
        else:
            self._purge_local_cert()
            _tls_kwargs = {}
        self._api_server = uvicorn.Server(
            uvicorn.Config(app, host=self.api_host, port=self.api_port,
                           log_config=None, **_tls_kwargs))
        self._api_task = asyncio.create_task(self._api_server.serve())
        logger.info("CS client API on %s://%s:%s",
                    "https" if _tls_kwargs else "http", self.api_host, self.api_port)
        try:
            await super().run()
        finally:
            self._api_server.should_exit = True
            try:
                await self._api_task
            except Exception as exc:  # noqa: BLE001
                logger.debug("CS API server shutdown: %s", exc)

    def _create_spoke_tasks(self, websocket):
        """Attach the CS telemetry relay loop.

        Every CS_TELEMETRY_INTERVAL_S (default 10s) the cs spoke re-emits its
        per-host Proxmox state (built by ``CSSpoke.deploy.relay_payload``) to the
        hub as a signed ``CS_TELEMETRY`` frame. The hub caches it in
        ``simulations_cache[spoke_id]`` (main.py) and the Simulations/VM Server
        view serves from that cache. Returned tasks are cancelled/awaited by the
        base class when the hub connection closes.
        """
        return [asyncio.create_task(self._cs_telemetry_relay_loop(websocket))]

    @staticmethod
    def _relay_content_sig(payload: Dict[str, Any]) -> Optional[str]:
        """Stable hash of the STATE that matters in a relay frame — so an idle
        fleet stops re-sending byte-identical frames every 10s (conditional
        relay). Deliberately EXCLUDES per-cycle noise (timestamps, rolling
        CPU/mem averages) but INCLUDES every client/VM state signal
        (running/cloning/shedding/stopped/started, prov/delete-gate, USB,
        quarantine, central/mist status, command queue) — those transitions are
        exactly what the Quota Engine + UI act on, so a change to any of them
        changes the sig and forces an immediate send. Returns None on any error
        → caller treats that as "changed" and sends (fail-safe: never skip)."""
        try:
            import hashlib
            import json
            parts: list = []
            for h in payload.get("proxmox_hosts") or []:
                h = h or {}
                hpx = h.get("proxmox") or {}
                for v in h.get("proxmox_vms") or []:
                    v = v or {}
                    parts.append(("vm", v.get("vmid"), v.get("status"), v.get("prov_status"),
                                  tuple(sorted(str(t) for t in (v.get("tags") or [])))))
                for u in h.get("usb_devices") or []:
                    u = u or {}
                    parts.append(("usb", u.get("bus_path") or u.get("bus"),
                                  u.get("vidpid"), u.get("state")))
                pr = hpx.get("prov_run") or {}
                parts.append(("prov", bool(pr.get("running")),
                              tuple(sorted((str((it or {}).get("vmid")), str((it or {}).get("status")))
                                           for it in (pr.get("items") or [])))))
                dg = hpx.get("delete_gate") or {}
                parts.append(("gate", dg.get("reason"), dg.get("threshold_exceeded")))
                parts.append(("qt", tuple(sorted(str((q or {}).get("bus_path"))
                                                 for q in (hpx.get("quarantine") or [])))))
                pv = hpx.get("provision") or {}
                parts.append(("provn", pv.get("reason"), pv.get("halt"),
                              pv.get("loop_running"), pv.get("auto_provision_on")))
                parts.append(("range", str(hpx.get("vmid_range")), hpx.get("connected"),
                              hpx.get("agent_version")))
            for c in payload.get("clients") or []:
                c = c or {}
                parts.append(("cli", c.get("hostname"), c.get("online"), c.get("status"),
                              tuple(sorted(str(s) for s in (c.get("active_simulations") or []))),
                              c.get("tier"), c.get("simulation_id")))
            cen = payload.get("central") or {}
            parts.append(("cen", cen.get("status") or cen.get("token_state"),
                          cen.get("wireless_clients"), cen.get("hardware_alerts")))
            mist = payload.get("mist") or {}
            parts.append(("mist", mist.get("status") or mist.get("token_state"),
                          mist.get("wireless_clients")))
            for q in payload.get("command_queue") or []:
                q = q or {}
                parts.append(("q", q.get("id") or q.get("cs_cmd_id"), q.get("status")))
            parts.append(("drain", bool(payload.get("draining"))))
            return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
        except Exception:
            return None

    async def _cs_telemetry_relay_loop(self, websocket) -> None:
        """Re-emit a signed ``CS_TELEMETRY`` frame to the hub every
        ``CS_TELEMETRY_INTERVAL_S`` (default 10s). The payload is
        ``ProxmoxDeploy.relay_payload`` (per-host VM state + the ``provision``
        diagnostic) with the ``central`` field overlaid from
        ``CentralPoller`` and the command-queue snapshot inlined so the hub's
        VM Server / Command Queue views read from cache. ``collect_dhcp_status``
        is offloaded to a thread so its ``systemctl`` call can't stall the
        shared event loop.

        Backpressure: this is the spoke-side of the hub's throttling ladder. The
        per-tick sleep is gated by ``self._bp_send_interval(interval)`` (bottom of
        the loop), so an ``LM_BACKPRESSURE`` slow-down from the hub stretches the
        send interval. Because each frame already carries the latest full snapshot,
        sending less often is latest-wins coalescing done ON THE SPOKE — the hub
        pushes the merge work down here rather than shedding our telemetry. See
        lm/docs/backpressure-throttling.md §6 (spoke-side cooperation)."""
        interval = 10
        try:
            interval = max(2, int(os.environ.get("CS_TELEMETRY_INTERVAL_S", "10")))
        except Exception:
            pass
        # Conditional relay (default OFF: relay EVERY ingested frame). The
        # hand-maintained content signature was incomplete (missed qt_state,
        # reclone progress, per-client gateway_reachable/ip), so real state
        # changes could be stranded up to the heartbeat while the sig also told
        # the hub to skip its broadcast. Relaying every frame removes that strand
        # class — the payload already carries all state, and the FAST/SLOW +
        # heartbeat cadence and _relay_wake early-send are all preserved.
        # Set CS_TELEMETRY_CONDITIONAL=1 to re-enable the old skip-unchanged gate.
        _CONDITIONAL = (os.environ.get("CS_TELEMETRY_CONDITIONAL", "0") != "0")
        _HEARTBEAT_S = 60
        _FAST = min(interval, 3)      # a state change just happened → stay snappy
        _SLOW = max(interval, 30)     # idle → back off (heartbeat still bounds it)
        _last_sig: Optional[str] = None
        _last_send_ts = 0.0
        _force_send = True            # first tick after (re)connect always sends
        # Wake event: an agent-frame ingest sets it so a state change relays
        # immediately instead of waiting out the idle SLOW interval (created here
        # so it binds to THIS running loop).
        self._relay_wake = asyncio.Event()
        # Stagger the first send so a freshly-ingested frame is more likely.
        await asyncio.sleep(2)
        while True:
            try:
                cs_mod = self.modules.get("cs")
                deploy = getattr(cs_mod, "deploy", None) if cs_mod else None
                if deploy is None:
                    await asyncio.sleep(interval)
                    continue
                # collect_dhcp_status() runs a blocking `systemctl is-active`
                # subprocess (up to 3s). Offload it so this 10s loop never stalls
                # the shared event loop — that recurring stall backed up inline
                # hub-command handling into 5s/30s Request Timeouts. Best-effort:
                # on any failure fall back to letting relay_payload probe inline.
                dhcp = None
                try:
                    try:
                        from dhcp_status import collect_dhcp_status
                    except ImportError:
                        from .dhcp_status import collect_dhcp_status
                    dhcp = await asyncio.to_thread(collect_dhcp_status)
                except Exception:
                    dhcp = None
                payload = deploy.relay_payload(self.spoke_id, self.spoke_id, dhcp=dhcp)
                # relay_payload's own "central" field is a placeholder ({}) —
                # overlay this spoke's real CentralPoller output so a
                # hub-connected deployment's Simulations tab gets live data too.
                payload["central"] = getattr(cs_mod, "central_status", {}) or {}
                # Mist twin: overlay this spoke's real MistPoller output so a
                # hub-connected deployment's Simulations tab gets live Mist
                # data too (same shape as the Central block above).
                payload["mist"] = getattr(cs_mod, "mist_status", {}) or {}
                # Command queue → telemetry so the hub's VM Server → Command
                # Queue view serves from its CS_TELEMETRY cache (instant) instead
                # of a live 15s request_response that stalls when this spoke is
                # busy. ≤10s staleness is fine for a queue view; mutations
                # (Send/Delete/Clear) still hit the live spoke and the WebUI
                # forces a live re-fetch afterward. Best-effort: never break
                # telemetry over a queue read.
                try:
                    _q = getattr(cs_mod, "queue", None)
                    if _q is not None:
                        payload["command_queue"] = await _q.list_commands()
                except Exception as e:  # noqa: BLE001
                    logger.debug("command_queue telemetry overlay failed: %s", e)
                # Client registry → telemetry so the hub's Simulations "Clients"
                # tab shows this spoke's connected sim clients. The hub reads
                # data["clients"] (service.get_clients_data); relay_payload never
                # carried it, so the registry lived only on the spoke (local
                # /api/clients showed clients but the hub view was always empty).
                # Mirror local_ui_routes.aggregate_clients' row shape (online =
                # seen within 300s) so the hub and local Clients views match.
                registry = getattr(cs_mod, "registry", None)
                if registry is not None:
                    # Row shape + tier join + Site/PHY/Sim-ID resolution live in
                    # the SHARED builder (client_rows.build_client_rows) so this
                    # hub-telemetry path and the local dashboard
                    # (local_ui_routes.aggregate_clients) can never diverge.
                    clients, tier_updates = build_client_rows(cs_mod)
                    payload["clients"] = clients
                    if tier_updates:
                        try:
                            await registry.record_tiers_batch(tier_updates)
                        except Exception as e:  # noqa: BLE001
                            logger.debug("record_tiers_batch failed: %s", e)
                # Dongle-quarantine per-host failed/total + per-bus failure for
                # the hub's bulk/single-bus alarm engine (the spoke's detection
                # sweep populates engine._qt_telemetry each reconcile).
                _eng = getattr(cs_mod, "engine", None)
                if _eng is not None:
                    try:
                        payload["qt_state"] = getattr(_eng, "_qt_telemetry", {}) or {}
                    except Exception as e:  # noqa: BLE001
                        logger.debug("qt_state telemetry overlay failed: %s", e)
                # Draining flag: True while a self-update is running (git pull +
                # about to os._exit+relaunch). The hub reads this and, while set,
                # queues CS_CONFIG_UPDATE (and other request/reply) pushes to the
                # mailbox instead of firing a 5s request_response that would time
                # out when we exit mid-reply. A fresh process starts False, so
                # the first post-restart frame tells the hub to clear drain.
                payload["draining"] = bool(getattr(self, "_draining", False))
                # ── Conditional relay: send only on a state change, a forced
                # reseed, or the heartbeat ceiling. A skipped tick sends NOTHING
                # (the hub keeps the last frame, which is identical) — that's the
                # transfer + hub-load saving. Any client/VM state transition
                # changes _sig → sends immediately, so the Quota Engine/UI are
                # never delayed. Next interval is FAST right after a change, SLOW
                # when idle. ``draining`` must always get through, so treat it as
                # a force.
                _now = time.time()
                _sig = self._relay_content_sig(payload)
                _changed = (_sig is None) or (_sig != _last_sig)
                _due_heartbeat = (_now - _last_send_ts) >= _HEARTBEAT_S
                _draining_now = bool(payload.get("draining"))
                _do_send = ((not _CONDITIONAL) or _force_send or _changed
                            or _due_heartbeat or _draining_now)
                if _do_send:
                    # Carry the content sig ONLY when conditional relay is enabled,
                    # so the HUB can skip its memo invalidation + browser broadcast
                    # on an unchanged heartbeat frame (see main._handle_cs_telemetry).
                    # With conditional relay OFF (the default) we relay every frame
                    # AND omit the sig so the hub never suppresses its broadcast —
                    # every ingested state change reflects immediately (no strand).
                    if _CONDITIONAL:
                        payload["_content_sig"] = _sig
                    msg = {
                        "header": {
                            "message_id": str(uuid.uuid4()),
                            "timestamp": time.time(),
                            "sender_id": self.spoke_id,
                            "destination_id": "hub",
                        },
                        "payload": {"type": "CS_TELEMETRY", "data": payload},
                    }
                    await websocket.send(self._encode_frame(msg))
                    _last_sig = _sig
                    _last_send_ts = _now
                    _force_send = False
                    interval = _FAST if _changed else _SLOW
                else:
                    interval = _SLOW
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("CS telemetry relay send failed: %s", e)
            # Honor the hub's LM_BACKPRESSURE slow-down: send no faster than the
            # requested interval (_bp_send_interval = max(base, _bp_min_interval),
            # a no-op when not throttled). Stretching the interval slows our send
            # cadence locally so the hub distributes the merge work down to the
            # spoke rather than shedding our frames (see the loop docstring and
            # lm/docs/backpressure-throttling.md §6).
            # Interruptible sleep: wake early when an agent frame is ingested
            # (a state change to relay) — else time out after the interval (the
            # heartbeat/idle cadence). This is what makes a delete/reclone/stop
            # reflect in ~0.1s instead of up to the SLOW interval.
            try:
                await asyncio.wait_for(self._relay_wake.wait(),
                                       timeout=self._bp_send_interval(interval))
            except asyncio.TimeoutError:
                pass
            self._relay_wake.clear()

    def run_standalone_mode(self):
        """Standalone FastAPI server: the full client API surface on
        0.0.0.0:8080, driven by the same CSSpoke hub mode uses.

        ``build_client_api_app`` is the single source for the route layer, so
        standalone and hub mode serve identical surfaces (health / kill-switch /
        status / config / scripts / clients / commands / inbox / ``/ws/client``
        + the mgmt routes). Run without ``--hub`` to use it.
        """
        logger.info("Starting CS Module in STANDALONE MODE on %s:%s",
                    self.api_host, self.api_port)
        spoke = CSSpoke(self.spoke_id, self.startup_config)
        app = build_client_api_app(spoke)
        # uvicorn.run() manages its own event loop (not yet running at this
        # point), so the Central poller must start from a FastAPI startup
        # hook rather than a direct call here (mirrors cs_spoke.demo.start()'s
        # own "needs a running loop" guard, just triggered later).
        app.add_event_handler("startup", spoke.central_poller.start)
        app.add_event_handler("startup", spoke.mist_poller.start)
        if getattr(spoke, "sim_quota_engine", None) is not None:
            app.add_event_handler("startup", spoke.sim_quota_engine.start)
        uvicorn.run(app, host=self.api_host, port=self.api_port)

if __name__ == "__main__":
    import os
    import socket
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Lab Manager Generic Agent")
    # --id is OPTIONAL: when neither --id nor the SPOKE_ID env is supplied the
    # spoke derives its id from the current OS hostname at startup, so a
    # cloned+renamed container reconnects under a new id (correlated to the old
    # one via the install UUID by the hub) instead of being frozen to the
    # hostname captured at install time. A pinned --id (install_all.sh) wins.
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID") or None)
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", nargs='?', default=os.getenv("HUB_SECRET", ""), const="")
    # --hub is OPTIONAL: when neither --hub nor HUB_URL is supplied the spoke
    # auto-discovers the hub via DNS (lm-hub.<dns-suffix>) then mDNS
    # (_lm-hub._tcp.local.) at startup (see BaseControlPlane.run). Pass an empty
    # value to force discovery; --standalone opts out of hub mode entirely.
    parser.add_argument("--hub",        default=os.getenv("HUB_URL") or None)
    # Standalone (hub-less local) mode is now an explicit opt-in. Previously an
    # empty --hub triggered it; with auto-discovery an empty --hub means "discover".
    parser.add_argument("--standalone", action="store_true",
                        help="run without a hub (local-only mode)")
    # Client API listener (169.253.1.1:8080 on the DHCP NIC). 0.0.0.0 binds it
    # onto every interface, including the sim-client DHCP NIC, so clients on
    # 169.253.1.0/24 reach it directly (the cs Kea serves no router option).
    # 8080, not 8000: the LM hub owns 0.0.0.0:8000 (admin WebUI/API) and in hub
    # mode this spoke shares that box — 8000 here collided with the hub.
    parser.add_argument("--port",       type=int, default=os.getenv("CS_API_PORT", "8080"))
    parser.add_argument("--host",       default=os.getenv("CS_API_HOST", "0.0.0.0"))
    # PSK self-provisioning (optional). Falls back to env LM_ONBOARDING_PSK /
    # LM_TENANT_ID_HINT when the flags are absent; a spoke without either
    # connects as before (pending admin approval).
    parser.add_argument("--onboarding-psk",  default=os.getenv("LM_ONBOARDING_PSK", ""))
    parser.add_argument("--tenant-id-hint",  default=os.getenv("LM_TENANT_ID_HINT", ""))
    args = parser.parse_args()
    if not args.id:
        # Derived id = the bare hostname (no "-spoke" suffix). A pinned --id /
        # SPOKE_ID still wins; only the unpinned/derived case is affected.
        args.id = socket.gethostname()
    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub,
                        onboarding_psk=args.onboarding_psk,
                        tenant_id_hint=args.tenant_id_hint,
                        api_host=args.host, api_port=args.port)
    if args.standalone:
        cp.run_standalone_mode()
    else:
        asyncio.run(cp.run())
