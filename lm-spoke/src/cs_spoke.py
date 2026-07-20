"""CSSpoke — the cs module's command surface (LM spoke + shared modules).

This is the entry point the LM hub (and, in Phase 2, the local mgmt API) drives.
``handle_command`` dispatches the ``CS_*`` command contract to the underlying
plain modules (``SimulationEngine`` for Phase 1; ``ClientRegistry`` /
``CommandQueue`` / ``ProxmoxDeploy`` added in Phases 2–3). Business logic lives in
those modules — ``handle_command`` is a thin dispatcher so the spoke is drivable
identically from an LM hub command or an HTTP client.

Phase 1 command subset (config + simulation state + kill switch + loop control):
    GET_VERSION, CS_GET_VERSION
    CS_TRIGGER_ITERATION
    CS_GET_SIMULATION_STATE
    CS_SET_SIMULATION_PROFILE
    CS_GET_CONFIG, CS_UPDATE_CONFIG, CS_UPDATE_USER_OVERRIDES
    CS_KILL_SWITCH
Legacy aliases: TRIGGER_ITERATION, SET_SIMULATION_PROFILE, GET_SIMULATION_STATE,
    UPDATE_CONFIG.

Retired commands (no longer sent by the LM hub): CS_START_SIMULATION,
    CS_STOP_SIMULATION, CS_GET_STATUS, CS_GET_TELEMETRY, CS_GET_CLIENTS.
    These went away when the hub's pre-native /api/sim/* block was removed;
    per-agent Client-Simulation mode now lives on the pxmx agents.
    SimulationEngine.start/stop remain on the engine API for the standalone
    HTTP mode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict

from simulation_engine import SimulationEngine
import sim_config
from proxmox_deploy import ProxmoxDeploy
from command_queue import CommandQueue, CSSettings
from token_store import TokenStore, sync_all_sim_tags
from client_registry import ClientRegistry
from demo_scenarios import DemoManager
from local_store import LocalStore
from central_poller import CentralPoller
from mist_poller import MistPoller
import client_api  # for client_api.push_pending (live command delivery to WS agents)

try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke  # type: ignore
from command_handlers import (
    AgentCommandsMixin,
    ClientCommandsMixin,
    ConfigCommandsMixin,
    IngestCommandsMixin,
    SimCommandsMixin,
)
# _deep_merge_cfg moved to the agents handler (its sole user, SET_AGENT_CONFIG);
# re-exported here so the historical ``from cs_spoke import _deep_merge_cfg`` path
# keeps working.
from command_handlers.handlers_agents import _deep_merge_cfg  # noqa: F401

logger = logging.getLogger("CSSpoke")


class CSSpoke(AgentCommandsMixin, SimCommandsMixin, ConfigCommandsMixin,
              ClientCommandsMixin, IngestCommandsMixin, BaseSpoke):
    """Client simulator spoke. Owns the sim engine (+ registry/queue/deploy later)."""

    def __init__(self, spoke_id: str, config: Dict[str, Any] | None = None):
        super().__init__(spoke_id, config or {})
        # Resolve repo-relative dirs from this file so cwd doesn't matter.
        # data/ stays under lm-spoke/ (runtime state, gitignored). configs/ lives
        # at the REPO root (sibling of lm-spoke/, not a child) — install_cs.sh
        # clones the whole repo to /opt/lm/cs, so <repo>/configs/simulation.conf
        # is the canon the engine + /api/config serve (matches webui-spoke
        # REPO_DIR/"configs").
        base = Path(__file__).resolve().parent.parent  # lm-spoke/
        config_dir = Path(__file__).resolve().parent.parent.parent / "configs"
        data_dir = base / "data"
        self.engine = SimulationEngine(spoke_id, config_dir=config_dir, data_dir=data_dir)
        # The Proxmox deploy module (per-host state + telemetry ingest + relay
        # payload) is wired (D1), and the persisted command queue + cs settings
        # store are wired (D2). The client registry (Phase 2) backs the client
        # API status/control surface.
        self.registry = ClientRegistry(
            data_dir,
            bucket_resolver=lambda hn: sim_config.pure_bucket_profile(hn, config_dir),
        )
        self.settings = CSSettings(data_dir, config_dir)
        self.queue = CommandQueue(data_dir, self.settings)
        self.deploy = ProxmoxDeploy()
        # GitHub push (Source of Truth = GitHub): repo/token pushed by the hub via
        # CS_CONFIG_UPDATE(github_config); held in memory only (never persisted).
        # _git_lock serialises git ops on REPO_DIR so a config push + a repo sync
        # can't race on .git/index.lock. _gh_push_tasks retains fire-and-forget
        # push tasks so they aren't GC'd mid-flight.
        self._github_config: Dict[str, Any] = {}
        self._git_lock = asyncio.Lock()
        self._gh_push_tasks: set = set()
        # Phase F: per-host Proxmox token store + sim-tag sync (registry=None in
        # Phase 2/3, so sim-tag sync is a no-op until the client registry lands).
        self.tokens = TokenStore(data_dir)
        # In-memory per-client demo-scenario overrides (TTL + auto-expiry). The
        # live flags are layered on top of the registry's persisted overrides at
        # config delivery time (client_api /api/config); demos never touch the
        # persisted store. Expiry sweep is started by control_plane.run().
        self.demo = DemoManager(on_change=self._on_client_override_changed)
        self._sim_tag_cache: Dict[tuple, set] = {}
        self._sim_tag_sync_lock = asyncio.Lock()
        # Sim-tag sweep is DEBOUNCED off the per-frame telemetry hot path: at most
        # once per _SIM_TAG_MIN_INTERVAL, and it backs off hard when a sweep has
        # PUT failures (Proxmox unreachable / wrong host:port / bad token) so a
        # misconfigured target can't re-storm every 10s telemetry frame — the
        # regression behind the recurring CS_INGEST_TELEMETRY Request Timeouts.
        self._sim_tag_last_ts = 0.0
        self._sim_tag_backoff_until = 0.0
        # Control-plane back-reference, set by CSControlPlane.run() so the
        # GET_AGENTS / SPOKE_RELAY / SET_AGENT_CONFIG handlers can reach
        # connected_agents / approve_pending_agent / send_to_agent (mirrors
        # ProxmoxSpoke(control_plane=...)). None when driven standalone.
        self.control_plane = None
        # Local Setup-tab config this spoke now owns itself instead of an LM
        # hub tenant store: auto-provisioning knobs (hub_config) + Aruba
        # Central credentials/sites (central_config/central_sites_config).
        # See local_store.py / central_poller.py module docstrings.
        self.local_store = LocalStore(data_dir)
        # Populated by CentralPoller in the exact shape sim-views.js's
        # Simulations Checks/Hardware/Client-Count tabs already expect
        # (status/hardware_alerts/client_count_status) — started by
        # CSControlPlane.run()/run_standalone_mode() (needs a running loop).
        self.central_status: Dict[str, Any] = {}
        self.central_poller = CentralPoller(self)
        # Juniper Mist twin of central_status/central_poller — populated by
        # MistPoller in the same Simulations Checks/Hardware/Client-Count shape
        # so sim-views.js's tabs render Mist data identically. Started by
        # CSControlPlane.run()/run_standalone_mode() alongside the Central loop.
        self.mist_status: Dict[str, Any] = {}
        self.mist_poller = MistPoller(self)
        # SimQuotaEngine — keeps each declared sim quota filled from the online
        # pool (alert/insight → sim + N clients + site). Reconciles against the
        # hub-pushed effective_sim_quotas; started by CSControlPlane.run().
        from sim_quota_engine import SimQuotaEngine
        self.sim_quota_engine = SimQuotaEngine(self)
        # RepoSync — periodic pull of the tenant's GitHub config branch (fetch
        # --prune origin + checkout -B <branch> origin/<branch>), so edits made
        # directly on GitHub reach the spoke and a fresh install switches off
        # main onto the tenant branch. See repo_sync.py; started by
        # CSControlPlane.run(). No-op when source_of_truth != github.
        from repo_sync import RepoSync
        self.repo_sync = RepoSync(self)

    # ── BaseSpoke: status (fallback for *_GET_STATUS) ───────────────────────
    async def get_status(self) -> Dict[str, Any]:
        """BaseSpoke override: snapshot of the sim engine + kill switch.

        Used as the fallback reply for ``*_GET_STATUS`` when the hub polls a
        spoke that has no command-specific status handler. Returns the spoke
        id, ``module="simulation"``, the current simulation_id/iteration, the
        active-sim count, and the global kill-switch state."""
        state = self.engine.get_current_state()
        return {
            "spoke_id": self.spoke_id,
            "module": "simulation",
            "mode": "simulator",
            "simulation_id": state["simulation_id"],
            "active_sims": state["active_simulations"],
            "status": state["status"],
            "iteration": state["iteration"],
            "kill_switch": self.engine.kill_switch_active(),
        }

    def get_version(self) -> str:
        """BaseSpoke override: the cs repo's autobumped VERSION string.

        Reads ``<repo>/VERSION`` (deployed at ``/opt/lm/cs/VERSION`` by
        ``install_cs.sh``) with an mtime-keyed cache so the frequent
        ``/ws/client``-connect calls don't re-open the file on the shared
        event loop. Returns ``"unknown"`` only if no VERSION file is found."""
        # cs_spoke.py lives at <repo>/lm-spoke/src/cs_spoke.py; the tracked,
        # autobumped VERSION file is at the REPO ROOT (<repo>/VERSION, deployed
        # at /opt/lm/cs/VERSION per install_cs.sh), one dir above the legacy
        # lm-spoke/VERSION path. Try repo-root first, then lm-spoke/ as a
        # fallback for any layout that places VERSION beside the spoke.
        here = Path(__file__).resolve().parent  # .../lm-spoke/src
        paths = (
            here.parent.parent / "VERSION",   # <repo>/VERSION  (dev + /opt/lm/cs/VERSION)
            here.parent / "VERSION",          # <repo>/lm-spoke/VERSION (fallback)
        )
        # mtime-keyed cache. /ws/client connects (client_api.py:298) call this
        # per connect on the cs spoke's shared event loop; reading the VERSION
        # file every connect is a sync open/read/close that stalls the loop
        # (contributes to the hub's "Request Timeout"). The file only changes on
        # an autobump release, so re-read only when the first existing path's
        # mtime changes; otherwise one inode-cached stat returns the cached
        # string. Mirrors command_queue._sim_phy_cached.
        vp = None
        for p in paths:
            try:
                if p.exists():
                    vp = p
                    break
            except Exception:  # noqa: BLE001
                pass
        if vp is not None:
            try:
                mtime = vp.stat().st_mtime_ns
            except OSError:
                mtime = 0
            cache = getattr(self, "_version_cache", None)
            if cache is not None and cache[0] == mtime:
                return cache[1]
            try:
                v = vp.read_text(encoding="utf-8").strip()
                if v:
                    self._version_cache = (mtime, v)
                    return v
            except Exception:  # noqa: BLE001
                pass
        return "unknown"

    # ── agent registry (cs-dialed pxmx agents) ──────────────────────────────

    def _get_agents(self) -> Dict[str, Any]:
        """List connected + pending cs-dialed agents (mirrors ProxmoxSpoke).

        cs-dialed agents are pxmx agents that dial this cs spoke's
        ``/ws/agent`` instead of the pxmx spoke. The cs WebUI renders them; the
        cs-relevant fields are hostname / version / last_seen (no Proxmox
        nodes/vms — those live on the pxmx-dial path)."""
        if not self.control_plane:
            return {"status": "SUCCESS", "agents": [], "pending_agents": []}
        agents = []
        for aid, info in self.control_plane.connected_agents.items():
            agents.append({
                "agent_id":  aid,
                "hostname":  info.get("hostname", aid),
                "last_seen": info.get("last_seen", 0),
                "version":   info.get("version", "unknown"),
                "status":    "connected",
            })
        pending = [
            {"agent_id": aid, "status": "pending"}
            for aid in self.control_plane.pending_agents
        ]
        return {"status": "SUCCESS", "agents": agents, "pending_agents": pending}

    # ── cert distribution (hub-brokered; le spoke issued/renewed a cert) ──────
    async def _install_cert_relay(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Relay a hub-delivered TLS cert to each managed pxmx agent.

        Mirrors ``ProxmoxSpoke``'s INSTALL_CERT branch
        (``proxmox_spoke.py:203-213``): in the split topology THIS cs spoke owns
        the pxmx agents (they dial ``wss://<cs>:443/ws/agent``), so the hub
        routes the ``simulation`` cert target here and we relay ``INSTALL_CERT``
        to each managed node's agent. The agent runs ``pvenode cert set --force
        --restart`` and verifies the deployed cert by fingerprint on its own
        timeout (pxmx agent ``install_cert``) — we never touch Proxmox directly.

        Targeting: an explicit ``agent_id`` (or ``identifier`` — the hub's
        INSTALL_CERT payload carries the target ``identifier``, which for a
        per-node ``simulation`` target IS the pxmx agent_id; parity with the
        pxmx spoke's ``_agent_for_node(data.get("identifier"))`` fallback)
        deploys to one node; otherwise broadcast to EVERY connected agent so
        one ``simulation`` cert target covers the whole fleet (the operator's
        "pxmx servers" plural). Relays run concurrently so wall-clock
        ≈ 620s, not N×620s, and a per-agent error does NOT abort the rest.

        The 620s relay window is > the agent's 600s pvenode wait and < the hub's
        640s INSTALL_CERT timeout, so neither peer times out first and masks a
        deploy still in progress (the agent reports SUCCESS on a slow restart
        via its fingerprint check).
        """
        if not self.control_plane:
            return {"status": "ERROR", "message": "not connected to a control plane"}
        connected = dict(self.control_plane.connected_agents or {})
        explicit = d.get("agent_id") or d.get("identifier")
        if explicit and explicit in connected:
            # A specific, currently-connected agent (per-node target).
            agent_ids = [explicit]
        else:
            # No explicit agent, OR the identifier is NOT a connected agent id.
            # The wildcard fan-out sends the SPOKE id as ``identifier`` (never an
            # agent id), which used to hard-fail "agent <spoke> not connected" even
            # though the spoke AND its agents were up (the reported bug). Treat any
            # non-agent identifier as a spoke/group-level target and deploy to
            # EVERY connected agent — correct for the fleet-wide wildcard device
            # cert, and a specific-but-offline node falls through to the fleet too.
            if explicit and explicit not in connected:
                logger.info("INSTALL_CERT: identifier %r is not a connected agent — "
                            "broadcasting to all %d connected agent(s)",
                            explicit, len(connected))
            agent_ids = list(connected.keys())
        if not agent_ids:
            # No agents connected at all → DEFER, don't fail. The hub retries
            # failed/deferred targets each distribution sweep, so this installs
            # itself on the agents' reconnect; surfacing it as a hard FAILED just
            # alarms the operator about a transient, self-healing condition.
            return {"status": "DEFERRED",
                    "message": "no managed pxmx agents connected — deferred, retries on reconnect"}

        relay_timeout = 620.0  # > agent 600s pvenode wait; < hub 640s timeout

        async def _one(aid: str) -> Dict[str, Any]:
            try:
                r = await self.control_plane.send_to_agent(
                    "INSTALL_CERT", d, agent_id=aid, timeout=relay_timeout)
            except Exception as exc:  # noqa: BLE001 - one failure must not abort the rest
                logger.warning("INSTALL_CERT relay to %s raised: %s", aid, exc)
                return {"agent_id": aid, "status": "ERROR",
                        "message": f"{type(exc).__name__}: {exc}"}
            rret = (r.get("payload", {}).get("data", r)
                    if isinstance(r, dict) else {})
            if isinstance(rret, dict) and rret.get("status") == "SUCCESS":
                return {"agent_id": aid, "status": "SUCCESS",
                        "message": rret.get("message") or "installed"}
            return {"agent_id": aid, "status": "ERROR",
                    "message": (rret.get("message") if isinstance(rret, dict)
                                else "INSTALL_CERT failed")}

        nodes = list(await asyncio.gather(*[_one(aid) for aid in agent_ids]))
        ok = sum(1 for n in nodes if n["status"] == "SUCCESS")
        total = len(nodes)
        failed = [n for n in nodes if n["status"] != "SUCCESS"]
        overall = "SUCCESS" if ok == total and total else "ERROR"
        msg = (f"deployed to {ok}/{total} node(s)"
               + (f" — {len(failed)} FAILED: {failed[0]['message']}" if failed else ""))
        logger.info("INSTALL_CERT relay: %s", msg)
        return {"status": overall, "message": msg, "nodes": nodes}

    # ── Phase F: sim-tag sync (driven off CS_INGEST_TELEMETRY / token store) ──
    _SIM_TAG_MIN_INTERVAL = 60.0    # at most one sweep per minute
    _SIM_TAG_FAIL_BACKOFF = 600.0   # 10 min after a sweep with PUT failures

    async def _maybe_sync_sim_tags(self) -> None:
        """Best-effort sim-tag sweep — DEBOUNCED off the telemetry hot path.

        Runs at most once per ``_SIM_TAG_MIN_INTERVAL``; a sweep with PUT
        failures (Proxmox unreachable / wrong host:port / bad token) backs off
        for ``_SIM_TAG_FAIL_BACKOFF`` so a misconfigured target can't re-storm
        every telemetry frame (each old call also rebuilt an SSL context per VM,
        burning loop CPU — the cause of the CS_INGEST Request Timeouts). No-op
        until the client registry is wired. Never raises."""
        if self.registry is None:
            return  # nothing to sync until the client registry lands
        now = time.time()
        if now < self._sim_tag_backoff_until:
            return  # in failure back-off — a target is unreachable/misconfigured
        if now - self._sim_tag_last_ts < self._SIM_TAG_MIN_INTERVAL:
            return  # debounced — already swept within the interval
        if self._sim_tag_sync_lock.locked():
            return  # a sweep is already running — don't pile another on
        try:
            async with self._sim_tag_sync_lock:
                self._sim_tag_last_ts = time.time()
                _updated, failures = await sync_all_sim_tags(
                    self.deploy, self.tokens, self.registry,
                    applied_cache=self._sim_tag_cache)
                if failures:
                    self._sim_tag_backoff_until = time.time() + self._SIM_TAG_FAIL_BACKOFF
                    logger.warning(
                        "sim-tag sync: %d VM(s) failed to tag (Proxmox unreachable / "
                        "wrong host:port / bad token?) — backing off %.0fm",
                        failures, self._SIM_TAG_FAIL_BACKOFF / 60)
        except Exception as e:  # noqa: BLE001
            self._sim_tag_backoff_until = time.time() + self._SIM_TAG_FAIL_BACKOFF
            logger.debug("sim-tag sync skipped: %s", e)

    # ── command dispatch ───────────────────────────────────────────────────
    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a ``CS_*`` command from the LM hub (or local API) to the sim modules.

        Thin dispatcher over the plain modules: ``SimulationEngine`` (sim
        state/profile/kill switch), ``ClientRegistry`` (per-client control
        panel), ``DemoManager`` (ephemeral fault scenarios), ``CommandQueue``
        (VM actions for the pxmx agent), ``ProxmoxDeploy`` (telemetry ingest +
        ``relay_payload``), ``LocalStore`` (auto-prov/Aruba config), and the
        cs-dialed agent registry (``GET_AGENTS``/``SET_AGENT_CONFIG``/
        ``SPOKE_RELAY``). Returns a ``{"status": "SUCCESS"|"ERROR", ...}`` dict
        so the spoke is drivable identically from a hub WS command or an HTTP
        client. See the module docstring for the full command contract."""
        # DEBUG, not INFO: the hub polls GET_AGENTS / CS_POLL_AGENT_INBOX /
        # CS_GET_USB_CONFIG every ~5s, so logging every command at INFO floods
        # the steady-state log. Meaningful commands emit their own INFO line in
        # their handler (e.g. "CS_CONFIG_UPDATE: applied ..."). See
        # logging-observability-contract.md (normalization / level discipline).
        logger.debug("Command: %s", command_type)
        cmd = command_type.upper()
        d = data or {}

        # ── identity / status ──────────────────────────────────────────────
        if cmd in ("GET_VERSION", "CS_GET_VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}

        # ── domain dispatch (handlers in command_handlers/*) ────────────────
        # The ~900-line CS_* if-chain was moved verbatim into per-domain mixin
        # dispatchers (agents / sim / config / clients / ingest) that CSSpoke
        # inherits. Each returns a result dict for its own commands or None to
        # defer; command sets are disjoint, so order is irrelevant and the first
        # non-None result wins.
        for _dispatch in (self._dispatch_agents, self._dispatch_sim,
                          self._dispatch_config, self._dispatch_clients,
                          self._dispatch_ingest):
            _result = await _dispatch(cmd, d)
            if _result is not None:
                return _result

        # Phase 2/3 commands (queue/proxmox/clients) return NotImplemented until
        # those modules land, so the LM hub sees a clear "not yet" rather than a
        # silent error.
        if cmd.startswith("CS_") and cmd.split("_")[1] in {
            "QUEUE", "GET", "CLEAR", "DEPLOY", "RECLONE", "VM", "APPROVE",
            "REJECT", "UPDATE", "SELF",
        }:
            if cmd in ("CS_GET_PROXMOX_STATUS", "CS_GET_PROXMOX_LOGS"):
                return {"status": "SUCCESS", "reachable": False,
                        "message": "Proxmox deploy lands in Phase 3",
                        "vms": [], "log": []}
            return {"status": "NOT_IMPLEMENTED",
                    "message": f"{cmd} lands in a later phase", "command": cmd}

        return {"status": "ERROR", "message": f"Unknown command: {command_type}"}

    def _trigger_sim_quota_reconcile(self) -> None:
        """Immediate best-effort SimQuotaEngine reconcile on an
        effective_sim_quotas push or a sim-config change (the periodic loop also
        sweeps every 60s)."""
        eng = getattr(self, "sim_quota_engine", None)
        if eng is not None:
            eng.trigger()

    async def _push_config_refresh_to_clients(self) -> None:
        """Enqueue ``update_now`` to every registered client and push it live to
        any currently-connected one, so a hub-side sim/user-override edit reaches
        the clients' local ``simulation.conf`` without waiting for a manual
        update or a VERSION bump. ``update_now`` runs ``update.sh``, which
        re-fetches ``/api/config`` + ``/api/config/overrides`` and diffs before
        applying — idempotent, so an unchanged config is a no-op. Best-effort:
        a client offline now picks up the command on its next inbox poll."""
        try:
            hostnames = list((self.registry.get_all() or {}).keys()) if \
                getattr(self, "registry", None) is not None else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_now[%s]: registry read failed: %s",
                           self.spoke_id, exc)
            return
        if not hostnames:
            logger.debug("update_now[%s]: no registered clients — skipping",
                         self.spoke_id)
            return
        await self._push_update_now_to(hostnames)

    async def _push_update_now_to(self, hostnames: List[str]) -> None:
        """Enqueue ``update_now`` to each given hostname + live-push it to any
        currently-connected one. Shared by the bulk refresh (all clients) and
        the per-client ``_on_client_override_changed`` (one client). Idempotent
        (CommandQueue dedups by target+action) + best-effort (an offline client
        picks up the command on its next inbox poll)."""
        if not hostnames:
            return
        pushed = 0
        for hn in hostnames:
            try:
                await self.queue.enqueue(hn, "update_now", {},
                                         command_type="update_now")
                if await client_api.push_pending(self, hn):
                    pushed += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("update_now[%s]: enqueue/push for %s failed: %s",
                             self.spoke_id, hn, exc)
        logger.info("update_now[%s]: enqueued to %d client(s); %d live-pushed",
                    self.spoke_id, len(hostnames), pushed)

    async def _on_client_override_changed(self, hostname: str) -> None:
        """An override layer (registry override OR demo scenario) for *hostname*
        changed on the spoke — enqueue ``update_now`` so the client re-fetches
        ``/api/config`` and its LOCAL ``simulation.conf`` picks up the new
        ``[username]`` section.

        This is the source-of-the-bug fix: a click (sim-bar toggle, Demo column
        Go/normal) or the 120-min demo expiry changes the spoke's served config,
        but ``update.sh`` runs ONLY on ``update_now`` / a VERSION bump (the 1-min
        watchdog runs ``sys_mon``, not ``update.sh``), so without this push the
        client kept its stale local file — a demo that expired on the spoke left
        the client serving a stale ``[username]`` forever (the "overrides still
        there after they should have auto-cleared" symptom). Idempotent (queue
        dedups) + best-effort; wired as DemoManager's ``on_change`` callback so
        demo apply / clear / the expiry sweep all propagate too."""
        if not hostname:
            return
        await self._push_update_now_to([hostname])

    # ── GitHub push (Source of Truth = GitHub) ───────────────────────────────
    async def _git(self, *args: str, timeout: float = 120.0,
                   env: "Dict[str, str] | None" = None) -> str:
        """Run a git command in REPO_DIR (config_dir's parent). Raises on failure.
        GIT_TERMINAL_PROMPT=0 so git never blocks on a credential prompt."""
        import os
        repo_dir = self.settings.config_dir.parent
        git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_ASKPASS": "/bin/echo", **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=git_env)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err.decode().strip()}")
        return out.decode().strip()

    async def _push_files_to_github(self, file_map: "Dict[str, str]", commit_message: str) -> bool:
        """Commit + push ``{repo-relative-path: content}`` to the tenant's GitHub
        repo using the hub-delivered token. Ports cs/webui-spoke proxmox_agent.
        _push_to_github: the token is fed via a temp GIT_ASKPASS script (never
        written into the remote URL / .git/config).

        To avoid a non-fast-forward rejection (the cs repo gets frequent CI VERSION
        bumps), we fetch + hard-reset onto origin/branch FIRST, then re-write the
        edited files from ``file_map`` and commit on top — so the push always
        fast-forwards. Untracked files (hub-*-overrides.conf, hub-config-source)
        are gitignored/untracked and survive the reset. Serialised on _git_lock;
        never raises (a failure is logged at WARNING so it surfaces in the hub
        Logs view via the relay handler — the operator can catch it)."""
        import os
        import uuid as _uuid
        sid = self.spoke_id
        gc = self._github_config or {}
        token = str(gc.get("github_token") or "").strip()
        repo_url = str(gc.get("repo_url") or "").strip()
        branch = str(gc.get("repo_branch") or "").strip() or "main"
        repo_dir = self.settings.config_dir.parent
        if not token:
            logger.warning("github push[%s]: no github_token in memory — skipping "
                           "(spoke restarted since the key was delivered? re-save the "
                           "GitHub credentials to re-deliver it)", sid)
            return False
        if not repo_url:
            logger.warning("github push[%s]: no repo_url configured — skipping", sid)
            return False
        if not (repo_dir / ".git").exists():
            logger.warning("github push[%s]: %s is not a git repo — skipping",
                           sid, repo_dir)
            return False
        logger.info("github push[%s]: starting — repo=%s branch=%s files=%s",
                    sid, repo_url, branch, list(file_map))
        askpass = repo_dir / f".git-askpass-{_uuid.uuid4().hex}.sh"
        try:
            async with self._git_lock:
                try:
                    await self._git("config", "user.name")
                except Exception:  # noqa: BLE001
                    await self._git("config", "user.name", "Client Simulator")
                try:
                    await self._git("config", "user.email")
                except Exception:  # noqa: BLE001
                    await self._git("config", "user.email", "client-sim@localhost")
                askpass.write_text(
                    "#!/bin/sh\ncase \"$1\" in\n"
                    "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                    "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\nesac\n",
                    encoding="utf-8")
                os.chmod(askpass, 0o700)
                push_env = {"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                            "GITHUB_TOKEN": token}
                await self._git("remote", "set-url", "origin", repo_url)
                logger.debug("github push[%s]: fetched origin", sid)
                # Sync onto origin/branch so the commit fast-forwards, then re-apply
                # the edits (the reset discarded the local working-tree write).
                await self._git("fetch", "--prune", "origin", env=push_env)
                await self._git("checkout", "-B", branch, f"origin/{branch}")
                logger.debug("github push[%s]: reset to origin/%s", sid, branch)
                for rel, content in file_map.items():
                    p = repo_dir / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(str(content), encoding="utf-8")
                await self._git("add", *list(file_map.keys()))
                staged = False
                try:
                    await self._git("diff", "--cached", "--quiet")
                except RuntimeError:
                    staged = True
                if not staged:
                    logger.info("github push[%s]: no changes vs origin for %s — nothing to push",
                                sid, list(file_map))
                    return False
                await self._git("commit", "-m", commit_message)
                await self._git("push", "origin", f"HEAD:{branch}", env=push_env)
                new_head = await self._git("rev-parse", "HEAD")
                logger.info("github push[%s]: pushed %s to %s @ %s — HEAD=%s",
                            sid, list(file_map), repo_url, branch, new_head[:12])
                return True
        except Exception as exc:  # noqa: BLE001 — never let a push crash the loop
            logger.warning("github push[%s]: FAILED — %s", sid, exc)
            return False
        finally:
            try:
                askpass.unlink()
            except Exception:  # noqa: BLE001
                pass

