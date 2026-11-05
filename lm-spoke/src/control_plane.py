"""Entry point and control plane for the cs (client simulator) spoke.

Connects the cs spoke to the LM hub over a WebSocket, hosts pxmx agents that
dial in directly, serves the client API / local dashboard on port 8080, and can
also run on its own without a hub (``--standalone``).
"""
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
# Private os/sys handles used only by the dependency check below.
import os as _os
import sys as _sys
# Load the dependency checker from lm core (package or bare module).
try:
    from core.src.dep_guard import ensure_requirements as _ensure_requirements
except ImportError:  # lm core not on path as a package — bare module on core/src
    from dep_guard import ensure_requirements as _ensure_requirements
# Install anything missing from requirements.txt, then drop the helper names.
_req = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                     "requirements.txt")
_ensure_requirements(_req)
del _os, _sys, _ensure_requirements, _req

# Async runtime for the hub connection and background tasks.
import asyncio
# Unique ids.
import uuid
# Timestamps for agent telemetry.
import time
# Validates TLS certificates before applying them.
import ssl
# Temporary files used during certificate validation.
import tempfile
# WebSocket client library (imported here so a missing install fails early).
import websockets
# Standard-library logging.
import logging
# Command-line argument parsing.
import argparse
# Python path setup.
import sys
# Environment variables and file operations.
import os
# Filesystem path handling.
from pathlib import Path
# Type hints only.
from typing import Any, Dict, Optional
# ASGI server that runs the client API.
import uvicorn

# Sibling modules (simulation_engine, cs_spoke, sim_config, sim_primitives,
# data_models) live next to this file. When launched as `python -m
# src.control_plane` the module's own directory is NOT added to sys.path
# (only the cwd / PYTHONPATH entries are), so the bare sibling imports below
# raise ModuleNotFoundError and the spoke crash-loops. Put this file's
# directory on the front of sys.path so every sibling import resolves
# regardless of how the process is started (-m, direct script, or import).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The cs spoke module and the client API app builder.
from cs_spoke import CSSpoke
from client_api import build_client_api_app
# Shared base class for hosting agents (lm core package or bare module).
try:
    from core.src.messaging.agent_hosting import AgentHostingControlPlane
except ImportError:
    from messaging.agent_hosting import AgentHostingControlPlane

# Shared logging setup; fall back to a simple basicConfig version if lm core is not available.
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
            """Fallback logging setup: basicConfig, optionally also writing to ``log_file``."""
            # Log to the file and the console when a file is given.
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# Set up logging and create this module's logger.
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
    # Module type and the environment variables / config path the agent listener uses.
    MODULE_TYPE = "simulation"
    AGENT_PORT_ENV = "LM_CS_AGENT_PORT"
    AGENT_LOOPBACK_ENV = "LM_CS_AGENT_LOOPBACK"
    AGENT_LISTENER_ENV = "LM_CS_AGENT_LISTENER"
    AGENT_CONFIG_PATH = "/etc/lm-cs-agent/config.json"
    # cs agent listener is opt-in (env-gated) so all-in-one stays relay-only.
    AGENT_LISTENER_OPT_IN = True
    # Ports for the agent listener (loopback, public TLS, fallback).
    AGENT_LOOPBACK_PORT = 8443
    AGENT_WSS_PORT = 443
    AGENT_FALLBACK_PORT = 8767

    def get_service_name(self) -> str:
        """Return systemd service unit name for the spoke process."""
        return "lm-cs"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None,
                 onboarding_psk: str = None, tenant_id_hint: str = None,
                 api_host: str = None, api_port: int = None):
        """Set up the control plane with hub credentials and client API host/port."""
        # Base-class setup with the hub connection details.
        super().__init__(spoke_id, secret, hub_secret, hub_url,
                         onboarding_psk=onboarding_psk, tenant_id_hint=tenant_id_hint)
        # Startup config passed through to CSSpoke.
        self.startup_config = config or {}
        # Client API listener (the spoke that owns the DHCP scope 169.253.1.1/24
        # is also the client API gateway on 169.253.1.1:8080). Bound 0.0.0.0 so
        # the listener lands on the DHCP NIC; configurable via CS_API_PORT/HOST.
        # NOTE: 8080 (not 8000) — the LM hub serves its own admin WebUI/API on
        # 0.0.0.0:8000, and in hub mode the cs spoke runs on the SAME box, so
        # binding 8000 here collided with the hub and broke the WebUI. The cs
        # client API takes 8080; sim clients reach it at 169.253.1.1:8080.
        self.api_host = api_host or os.getenv("CS_API_HOST", "0.0.0.0")
        # Port from the argument or CS_API_PORT; 8080 if invalid.
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
        # Set by run() once CSSpoke exists — lets _repush_agent_config read the
        # live CSSettings store at replay time (see there) instead of trusting
        # a possibly stale _agent_config_cache entry. None outside run().
        self._cs_spoke = None
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
    # Environment variable that overrides the TLS folder.
    _TLS_DIR_ENV = "LM_CS_TLS_DIR"

    def _tls_dir(self) -> Path:
        """Where the local-webui fullchain/privkey live. Env-overridable; default
        is <lm-spoke>/data/tls — the spoke's own data dir, always spoke-writable
        (CSSpoke already persists settings/registry/queue there), so applying a
        cert needs no installer or service-user change."""
        # Use the override folder when set.
        d = os.getenv(self._TLS_DIR_ENV)
        if d:
            return Path(d)
        # control_plane.py is <lm-spoke>/src/ → parent.parent = <lm-spoke>/
        return Path(__file__).resolve().parent.parent / "data" / "tls"

    def _local_tls_paths(self):
        """Return (cert, key) paths if a local-webui cert is persisted on disk,
        else (None, None). Used by run() to bind HTTPS on startup."""
        # Expected cert and key files.
        d = self._tls_dir()
        cert, key = d / "fullchain.pem", d / "privkey.pem"
        # Return them only if both exist.
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
        """Return True if LM_CS_ALLOW_LOCAL_CERT=1 allows TLS on the client API."""
        # Check the environment flag.
        return os.getenv(self._ALLOW_LOCAL_CERT_ENV) == "1"

    def _purge_local_cert(self) -> None:
        """Delete any persisted local-webui cert so the client API binds plaintext.
        Called on startup and whenever an INSTALL_CERT is refused, so a stray le
        cert can't linger and flip 8080 to HTTPS on the next restart."""
        # Delete each cert file that exists, logging the removal.
        d = self._tls_dir()
        for name in ("fullchain.pem", "privkey.pem"):
            p = d / name
            try:
                if p.exists():
                    p.unlink()
                    logger.warning(
                        "Removed local-webui cert %s — cs client API is "
                        "plaintext-only on the isolated sim segment", p)
            # A failed delete is only logged.
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
        # Use the applied LE cert when present.
        cert, key = self._local_tls_paths()
        if cert and key:
            return cert, key
        # Otherwise use the installer's cert paths from the environment.
        cert = os.environ.get("LM_TLS_CERT", "").strip()
        key = os.environ.get("LM_TLS_KEY", "").strip()
        return cert, key

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Write ``text`` to ``path`` via a temp file and an atomic rename."""
        # Write the temp file, then swap it in.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` via a temp file and an atomic rename."""
        # Write the temp file, then swap it in.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    async def _rebind_api_server(self, ssl_certfile=None, ssl_keyfile=None) -> None:
        """Tear down the current 8080 uvicorn server and start a fresh one,
        optionally with TLS. Awaited so the old socket is released before the
        new bind (avoids EADDRINUSE). The app is reused (self._api_app)."""
        # Ask the old server to stop.
        old = self._api_server
        old_task = self._api_task
        if old is not None:
            old.should_exit = True
        # Wait up to 5 s for it to finish.
        if old_task is not None:
            try:
                await asyncio.wait_for(old_task, timeout=5.0)
            # A timeout or cancel during shutdown is fine.
            except Exception:  # noqa: BLE001 - shutdown timeout/Cancel is fine
                pass
        # Build the new server's settings, adding TLS when both files are given.
        kwargs = dict(host=self.api_host, port=self.api_port, log_config=None)
        if ssl_certfile and ssl_keyfile:
            kwargs["ssl_certfile"] = ssl_certfile
            kwargs["ssl_keyfile"] = ssl_keyfile
        # Start the new server in the background.
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
        # Both cert and key are required.
        if not (fullchain and privkey):
            return {"status": "ERROR", "message": "missing cert material"}
        # Validate before touching live files.
        try:
            # Write both to temp files and load them into an SSL context.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as cf:
                cf.write(fullchain); cp = cf.name
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False) as kf:
                kf.write(privkey.encode("utf-8")); kp = kf.name
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cp, kp)
            # Always delete the temp files.
            finally:
                try: os.unlink(cp)
                except OSError: pass
                try: os.unlink(kp)
                except OSError: pass
        # A cert that fails to load is rejected.
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
        # Report success.
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
        # Push the cached config in the background, if there is one.
        cfg = self._agent_config_cache.get(agent_id)
        if cfg:
            asyncio.create_task(self._repush_agent_config(agent_id, cfg))

    async def _repush_agent_config(self, agent_id: str, cfg: Dict[str, Any]) -> None:
        """Regression: ``cfg`` is whatever ``_agent_config_cache`` last held from
        the hub's most recent ``SET_AGENT_CONFIG`` — which can be arbitrarily
        stale, since this cache is only ever refreshed BY a fresh hub push, not
        by the passage of time or by an agent reconnect. A WS reconnect (network
        blip, a local TLS cert rebind via ``_rebind_agent_server``, etc.)
        requires no agent process restart and is invisible to the agent's own
        systemd-level "hub config NOT confirmed" staleness guard — so a
        `usb_auto_provision` value the hub pushed as "on" during, say, initial
        setup/testing could sit cached here and get silently replayed as "on" on
        every later reconnect, long after the operator toggled it off, until the
        hub happens to push SET_AGENT_CONFIG again. Refresh the usb_config leaf
        from the spoke's own live CSSettings store (always current — see
        cs_settings.usb_config_payload) right before every replay so a
        reconnect can never resurrect a stale auto-provision/exclusion value."""
        # Refresh the USB settings from the live settings store.
        settings = getattr(self._cs_spoke, "settings", None)
        if settings is not None and isinstance(cfg.get("client_simulation"), dict):
            cfg = dict(cfg)
            cfg["client_simulation"] = dict(cfg["client_simulation"])
            cfg["client_simulation"]["usb_config"] = settings.usb_config_payload()
        # Send the config to the agent.
        try:
            await self.send_to_agent("UPDATE_CONFIG", cfg, agent_id=agent_id)
            logger.info(f"Re-pushed cached client-sim config to agent '{agent_id}'")
        # A failed send is only logged.
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to re-push config to agent '{agent_id}': {e}")

    async def _on_agent_telemetry(self, agent_id: str, rec: Optional[Dict[str, Any]],
                                  data: Dict[str, Any]) -> None:
        """Cache Proxmox nodes/vms/cluster + agent_metrics into connected_agents,
        mirroring pxmx's ``PxmxControlPlane._on_agent_telemetry``.

        This hook never existed on CS — the base ``AgentHostingControlPlane``
        default is a no-op, so ``connected_agents[agent_id]["cluster_name"]``
        stayed at its connect-time default (``agent_id``, i.e. the agent's own
        hostname) forever, and ``["nodes"]``/``["vms"]``/``["telemetry_ts"]``
        never populated at all. For a Proxmox host hosted directly by a cs
        spoke (the split-topology case), that meant the hub's shared
        GET_NODE_STATS/PXMX_LIST_VMS aggregator (``pxmx_node_vm_aggregation``)
        always read the stale hostname-as-cluster default and a permanently
        empty telemetry cache — regardless of the agent correctly resolving
        and sending its real Proxmox cluster name every tick. Filling in the
        same fields pxmx does fixes both the "cluster shows as hostname" bug
        and the freshness-based cross-agent/cross-spoke dedup (which needs
        telemetry_ts to rank a cs-hosted agent's report against others)."""
        # Store the cluster name, nodes, VMs, metrics and receipt time.
        if rec is not None:
            rec["cluster_name"] = data.get("cluster_name", agent_id)
            rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
            rec["vms"]          = data.get("vms", {}).get("vms", [])
            rec["agent_metrics"] = data.get("metrics", {})
            rec["telemetry_ts"] = time.time()

    async def run(self):
        """Boot the cs spoke: register ``CSSpoke``, (conditionally) start the
        cs-dialed agent listener, start the demo TTL sweep + Aruba Central
        poller, launch the client-API uvicorn server, then enter the hub WS
        loop (``super().run()``). The client API is a long-lived task that
        survives hub reconnects; it is torn down in ``finally`` on shutdown."""
        # Create the cs spoke module.
        logger.info(f"Starting CS (Client Simulator) -> {self.hub_url}")
        cs_spoke = CSSpoke(self.spoke_id, self.startup_config)
        # Wire the control-plane reference so CSSpoke's GET_AGENTS / SPOKE_RELAY
        # handlers can reach connected_agents / approve_pending_agent / send_to_agent
        # (mirrors ProxmoxSpoke(control_plane=self)).
        cs_spoke.control_plane = self
        # Back-reference so _repush_agent_config can read the spoke's live
        # CSSettings at replay time instead of trusting a frozen cache entry —
        # see _repush_agent_config.
        self._cs_spoke = cs_spoke
        # Register it so hub commands reach it.
        self.register_module("cs", cs_spoke)
        # Start the agent listener when LM_CS_AGENT_LISTENER=1. install_cs.sh
        # writes that env by DEFAULT (standalone cs accepts cs-dialed pxmx
        # agents); --no-agent-listener opts out so an all-in-one / relay-only cs
        # spoke never binds :443 on the hub box.
        if self._agent_listener_enabled():
            # Mint + persist an agent_secret if the listener came up without an
            # installer-provisioned one (upgraded before the secret step, or the
            # listener toggled on at runtime). Without it approve_pending_agent
            # can only ship {"secret": null} → hosted pxmx agents flap in
            # "pending / needs approval" forever. Mirrors RoleConnection.__init__
            # and is a no-op when install_cs.sh already wrote the secret.
            # getattr-guarded so a staged self-update (cs code ahead of lm-core)
            # can't crash startup if the base method isn't present yet.
            # Make sure an agent secret exists (only if lm core supports it).
            _ensure = getattr(self, "_ensure_agent_secret", None)
            if callable(_ensure):
                _ensure()
            # Start the listener.
            self._start_agent_server_task()
            logger.info("CS agent listener enabled (cs-dialed agents accepted)")
        # Listener turned off.
        else:
            logger.info("CS agent listener disabled (relay-only; --no-agent-listener was passed)")
        # Start the always-on local service loops (demo TTL sweep, registry
        # prune, stale-client reclone, cloud + on-prem Central pollers, Mist
        # poller, quota engine, repo sync). Extracted onto the CSSpoke module so
        # the generic-agent RoleConnection starts the IDENTICAL set when it hosts
        # this module as a role — see CSSpoke.start_background_loops.
        cs_spoke.start_background_loops()
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
            # TLS allowed: use the saved cert if present.
            tls_cert, tls_key = self._local_tls_paths()
            _tls_kwargs = ({"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
                           if tls_cert and tls_key else {})
        # Default: remove any cert and serve plain HTTP.
        else:
            self._purge_local_cert()
            _tls_kwargs = {}
        # Build the uvicorn server and start it in the background.
        self._api_server = uvicorn.Server(
            uvicorn.Config(app, host=self.api_host, port=self.api_port,
                           log_config=None, **_tls_kwargs))
        self._api_task = asyncio.create_task(self._api_server.serve())
        logger.info("CS client API on %s://%s:%s",
                    "https" if _tls_kwargs else "http", self.api_host, self.api_port)
        # Run the hub connection loop.
        try:
            await super().run()
        # On exit, stop the API server and wait for it.
        finally:
            self._api_server.should_exit = True
            try:
                await self._api_task
            # Shutdown errors only log at debug level.
            except Exception as exc:  # noqa: BLE001
                logger.debug("CS API server shutdown: %s", exc)

    def _create_spoke_tasks(self, websocket):
        """Attach the CS telemetry relay loop.

        The relay loop lives on the ``CSSpoke`` module (``create_spoke_tasks``)
        so it runs identically whether the module is driven by this standalone
        control plane OR hosted as a role by the generic multi-role agent
        (``RoleConnection``). Without this delegation a role-hosted simulation
        spoke would never emit ``CS_TELEMETRY`` and would be invisible in the
        hub's Simulations view. Returned tasks are cancelled/awaited by the base
        class when the hub connection closes.
        """
        # The cs module's own relay tasks (none if not registered).
        cs_mod = self.modules.get("cs")
        if cs_mod is None:
            return []
        return list(cs_mod.create_spoke_tasks(websocket) or [])

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
        # Create the spoke and the API app.
        spoke = CSSpoke(self.spoke_id, self.startup_config)
        app = build_client_api_app(spoke)
        # uvicorn.run() manages its own event loop (not yet running at this
        # point), so the Central poller must start from a FastAPI startup
        # hook rather than a direct call here (mirrors cs_spoke.demo.start()'s
        # own "needs a running loop" guard, just triggered later).
        app.add_event_handler("startup", spoke.central_poller.start)
        app.add_event_handler("startup", spoke.central_on_prem_poller.start)
        app.add_event_handler("startup", spoke.mist_poller.start)
        # Start the quota engine too, when present.
        if getattr(spoke, "sim_quota_engine", None) is not None:
            app.add_event_handler("startup", spoke.sim_quota_engine.start)
        # Run the server (blocks until exit).
        uvicorn.run(app, host=self.api_host, port=self.api_port)

def resolve_pinned_secret(name: str, cli_value: Optional[str]) -> str:
    """Prefer the .env value over a stale CLI-pinned one.

    The spoke persists rotated / zero-touch-renegotiated secrets to .env
    (SPOKE_SECRET, HUB_SECRET), but an older install_cs.sh baked the secret into
    the unit's ExecStart (``--secret=...``). argparse then let that frozen value
    win on every restart, so the hub rejected it and the spoke fell back to
    zero-touch each time. When both are set and differ, .env is the newer truth
    (an empty .env value means the spoke cleared it after a hub rejection).
    Records the override in ``cs_spoke.PINNED_SECRET_IGNORED`` for health."""
    # Use the command-line value when there is no .env value.
    cli_value = cli_value or ""
    if name not in os.environ:
        return cli_value
    env_value = os.environ.get(name, "")
    # Same value: nothing to override.
    if cli_value == env_value:
        return cli_value
    # Different: warn, record the override and use the .env value.
    logger.warning(
        "%s passed on the command line differs from %s in .env — ignoring the "
        "pinned CLI value (stale ExecStart). Re-run install_cs.sh to drop it "
        "from the systemd unit.", name, name)
    import cs_spoke
    if name not in cs_spoke.PINNED_SECRET_IGNORED:
        cs_spoke.PINNED_SECRET_IGNORED.append(name)
    return env_value


# Script entry point.
if __name__ == "__main__":
    # Extra imports used only when run as a script; load .env first.
    import os
    import socket
    from dotenv import load_dotenv
    load_dotenv()

    # Command-line options (defaults come from the environment).
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
    # Parse the arguments.
    args = parser.parse_args()
    if not args.id:
        # Derived id = the bare hostname (no "-spoke" suffix). A pinned --id /
        # SPOKE_ID still wins; only the unpinned/derived case is affected.
        args.id = socket.gethostname()
    # Prefer newer .env secrets over stale command-line ones.
    args.secret = resolve_pinned_secret("SPOKE_SECRET", args.secret)
    args.hub_secret = resolve_pinned_secret("HUB_SECRET", args.hub_secret)
    # Build the control plane.
    cp = CSControlPlane(args.id, args.secret, args.hub_secret, args.hub,
                        onboarding_psk=args.onboarding_psk,
                        tenant_id_hint=args.tenant_id_hint,
                        api_host=args.host, api_port=args.port)
    # Run standalone or connect to the hub.
    if args.standalone:
        cp.run_standalone_mode()
    else:
        asyncio.run(cp.run())
