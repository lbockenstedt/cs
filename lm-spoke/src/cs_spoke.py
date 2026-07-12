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
from demo_scenarios import DemoManager, DEMO_SCENARIOS
from local_store import LocalStore
from central_poller import CentralPoller
import client_api  # for client_api.push_pending (live command delivery to WS agents)

try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke  # type: ignore

logger = logging.getLogger("CSSpoke")


def _deep_merge_cfg(base, incoming):
    """Recursively merge ``incoming`` into ``base`` (dicts), replacing non-dict
    leaves (lists included) whole. Mirrors the pxmx agent's UPDATE_CONFIG merge.

    Used so the SET_AGENT_CONFIG cache doesn't lose sibling sub-trees: the hub
    sends agent config from TWO sources — the Agent-Config UI save
    (``client_simulation:{enabled,tenant_id}``, no usb_config) and the CS bridge
    (``client_simulation:{...,usb_config:{vidpids:[...]}}``). A blind
    ``cache[agent_id] = cfg`` let the enabled/tenant-only save wipe the cached
    ``usb_config.vidpids``; the spoke then re-pushed a vidpid-less config on the
    agent's next reconnect and the provision loop reported
    "no dongle_vidpids configured". Merging preserves usb_config while still
    letting a real usb_config push replace the vidpids list."""
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        return incoming
    out = dict(base)
    for k, v in incoming.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge_cfg(out[k], v)
        else:
            out[k] = v
    return out


class CSSpoke(BaseSpoke):
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
        self.demo = DemoManager()
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
        # SimQuotaEngine — keeps each declared sim quota filled from the online
        # pool (alert/insight → sim + N clients + site). Reconciles against the
        # hub-pushed effective_sim_quotas; started by CSControlPlane.run().
        from sim_quota_engine import SimQuotaEngine
        self.sim_quota_engine = SimQuotaEngine(self)

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
        if explicit:
            if explicit not in connected:
                return {"status": "ERROR",
                        "message": f"agent {explicit} not connected"}
            agent_ids = [explicit]
        else:
            agent_ids = list(connected.keys())
        if not agent_ids:
            return {"status": "ERROR",
                    "message": "no managed pxmx agents connected"}

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

        # ── agent hosting (cs-dialed pxmx agents, split topology) ───────────
        # These mirror ProxmoxSpoke so the LM hub can list/approve/relay-to a
        # pxmx agent that dials THIS cs spoke (wss://<cs>:443/ws/agent) instead
        # of the pxmx spoke. The cs WebUI's cs-agents panel + the hub's
        # CSBridgePoller drive them. Only active when CSControlPlane has its
        # agent listener enabled (LM_CS_AGENT_LISTENER=1); otherwise
        # control_plane.connected_agents is empty and these return empty/error.
        if cmd == "GET_AGENTS":
            return self._get_agents()

        if cmd == "SET_AGENT_CONFIG":
            agent_id = d.get("agent_id")
            cfg = d.get("config", {})
            if not agent_id:
                return {"status": "ERROR", "message": "Missing agent_id"}
            if self.control_plane:
                # Cache so the agent gets its config re-pushed on every
                # (re)connect (restart / self-update) without the hub having to
                # re-send SET_AGENT_CONFIG — see _on_agent_registered. Stored
                # even if the immediate send below fails (agent momentarily
                # offline), so the next connect still delivers it.
                # Deep-merge into the cached config so an enabled/tenant-only UI
                # save doesn't wipe the usb_config the CS bridge cached (and vice
                # versa), and push the FULL merged config so even a pre-merge-fix
                # agent gets its usb_config.vidpids on this push and every
                # reconnect. Without this, "no dongle_vidpids configured" persists
                # after any Agent-Config save. See _deep_merge_cfg.
                cache = getattr(self.control_plane, "_agent_config_cache", None)
                if cache is not None:
                    cfg = _deep_merge_cfg(cache.get(agent_id) or {}, cfg)
                    cache[agent_id] = cfg
                return await self.control_plane.send_to_agent(
                    "UPDATE_CONFIG", cfg, agent_id=agent_id)
            return {"status": "ERROR", "message": "Agent not connected"}

        if cmd == "SPOKE_RELAY":
            target = d.get("target_agent_id")
            command = d.get("command")
            if command == "APPROVAL_SUCCESS" and target and self.control_plane:
                await self.control_plane.approve_pending_agent(target)
                return {"status": "SUCCESS", "message": f"Agent {target} approved"}
            if command == "REVOKE_AGENT" and target and self.control_plane:
                await self.control_plane.revoke_agent(target)
                return {"status": "SUCCESS", "message": f"Agent {target} disconnected"}
            # Generic forward: relay an arbitrary command (e.g. CS_COMMAND from
            # the hub's CSBridgePoller) to a specific cs-dialed agent. The
            # agent's AGENT_RESPONSE data is returned to the hub.
            if command and target and self.control_plane:
                inner = d.get("data") or {}
                # Long ops (delete/reclone/snapshot/clone/provision) ack ACCEPTED
                # immediately and stream the real result via CS_COMMAND_RESULT,
                # but that ACCEPTED can slip past the default 15s window when the
                # agent is busy (e.g. auto-prov cloning clients during a bulk
                # delete) — producing a false "Agent response timeout" and a
                # FAILED queue entry even though the op runs. Give long ops a
                # wider sync window; fast commands keep the 15s fail-fast. Match
                # on the relay command OR the inner action (path-agnostic).
                _LONG = {"delete_vm", "reclone_vm", "snapshot_vm", "clone_lxc",
                         "provision_unassigned", "reclone_all", "proxmox_reclone_all"}
                _act = command if command in _LONG else (
                    inner.get("action") if isinstance(inner, dict) else None)
                # Timeouts are hub-configurable (Setup → General, pushed via
                # CS_CONFIG_UPDATE) so WAN / busy-agent sites can tune them.
                _to = (getattr(self, "_relay_timeout_long", 60.0) if _act in _LONG
                       else getattr(self, "_relay_timeout_fast", 15.0))
                return await self.control_plane.send_to_agent(command, inner, agent_id=target, timeout=_to)
            return {"status": "ERROR", "error": "Unknown relay command"}

        # ── cert distribution (hub-brokered; le spoke issued/renewed a cert) ──
        # INSTALL_CERT relays the cert to each managed pxmx agent (→ pvenode cert
        # set on that node's pveproxy) — the cs spoke owns the agents in the split
        # topology. A 'simulation' target ALSO applies the cert to THIS spoke's
        # own 8080 dashboard (control_plane._apply_local_cert) so the operator's
        # browser gets HTTPS with the LE cert. A 'hypervisor' target routed here
        # (split topology: the pxmx agents dial the cs spoke, not the pxmx spoke)
        # is destined for a pxmx node only — it must NOT rebind this spoke's
        # dashboard, so it relays and returns.
        if cmd == "INSTALL_CERT":
            mt = (d.get("module_type") or "").lower()
            relay = await self._install_cert_relay(d)
            if mt == "hypervisor":
                return relay
            webui = {"status": "ERROR", "message": "no control plane"}
            cp = self.control_plane
            if cp is not None and hasattr(cp, "_apply_local_cert"):
                try:
                    webui = await cp._apply_local_cert(
                        d.get("fullchain", ""), d.get("privkey", ""))
                except Exception as exc:  # noqa: BLE001 - webui apply must not mask the relay result
                    webui = {"status": "ERROR",
                             "message": f"{type(exc).__name__}: {exc}"}
            relay["webui"] = webui.get("message", "")
            # The node relay owns the target status: a cert deployed to the
            # nodes is SUCCESS even when the local-dashboard HTTPS rebind fails
            # (that failure is surfaced in the message, not dropped — but it
            # must not flip a successful node deploy red, which was the
            # operator's complaint: a deployed cert showing failed). A relay
            # ERROR stays ERROR regardless of the webui outcome.
            relay["message"] = (relay.get("message", "")
                                + f"; webui {webui.get('status', 'ERROR').lower()}"
                                + (f": {webui.get('message', '')}" if webui.get("message") else ""))
            return relay

        # ── VNC console relay (agent-terminates-WSS) ────────────────────────
        # PORT of ProxmoxSpoke.VNC_START/FRAME_DOWN/DISCONNECT. In the all-cs-
        # hosted topology the pxmx agents dial THIS cs spoke, so the cs spoke —
        # not a pxmx spoke — must relay VNC to them. Without these handlers a
        # request_response(cs_spoke, "VNC_START") hit no branch and fell through
        # to a generic error, which the hub surfaced as "agent refused
        # VNC_START". VNC_START is sync (returns the Proxmox ticket = the RFB
        # password noVNC must present); frames/disconnect are fire-and-forget.
        if cmd == "VNC_START":
            if not hasattr(self, "vnc_sessions"):
                self.vnc_sessions = {}
            session_id = d.get("session_id") or ""
            agent_id = d.get("agent_id") or d.get("target_agent_id")
            if not agent_id and session_id:
                agent_id = self.vnc_sessions.get(session_id)
            if not agent_id and self.control_plane:
                node = str(d.get("node") or "")
                uid = str(d.get("unique_id") or "")
                cluster = uid.split("/")[0] if "/" in uid else ""
                for aid, info in self.control_plane.connected_agents.items():
                    hosts = {info.get("hostname"), info.get("cluster_name")}
                    if (node and node in hosts) or (cluster and cluster in hosts):
                        agent_id = aid
                        break
                if not agent_id and len(self.control_plane.connected_agents) == 1:
                    agent_id = next(iter(self.control_plane.connected_agents))
            if not agent_id:
                return {"status": "ERROR", "message": "No agent resolved for VNC_START"}
            if session_id:
                self.vnc_sessions[session_id] = agent_id
            return await self.control_plane.send_to_agent(
                "VNC_START", d, agent_id=agent_id, timeout=45.0)

        if cmd == "VNC_FRAME_DOWN":
            aid = getattr(self, "vnc_sessions", {}).get(d.get("session_id") or "")
            if aid and self.control_plane:
                await self.control_plane.send_raw_to_agent(aid, "VNC_FRAME_DOWN", d)
            return {"status": "OK"}

        if cmd == "VNC_DISCONNECT":
            aid = getattr(self, "vnc_sessions", {}).pop(d.get("session_id") or "", None)
            if aid and self.control_plane:
                await self.control_plane.send_raw_to_agent(aid, "VNC_DISCONNECT", d)
            return {"status": "OK"}

        # ── simulation execution ────────────────────────────────────────────
        if cmd in ("CS_TRIGGER_ITERATION", "TRIGGER_ITERATION"):
            result = await self.engine.run_iteration()
            return {"status": "SUCCESS", **result}

        if cmd in ("CS_GET_SIMULATION_STATE", "GET_SIMULATION_STATE"):
            return {"status": "SUCCESS", **self.engine.get_current_state()}

        if cmd in ("CS_SET_SIMULATION_PROFILE", "SET_SIMULATION_PROFILE"):
            self.engine.update_config(d.get("profile", {}))
            return {"status": "SUCCESS",
                    "message": f"Profile patched for {self.engine.hostname}"}

        # ── config ─────────────────────────────────────────────────────────
        if cmd in ("CS_GET_CONFIG",):
            # Return the MERGED config (base repo file + hub-managed override
            # layered on top) so the hub's Sim Config editor reads back the
            # effective config, not the raw base. Without the merge, edits saved
            # via CS_CONFIG_UPDATE (which writes hub-*-overrides.conf) would be
            # invisible on Refresh. Mirrors legacy GET /api/config + /api/config/overrides.
            base = Path(__file__).resolve().parent.parent.parent / "configs"
            sim_conf, user_conf = sim_config.load_configs(base)
            return {"status": "SUCCESS", "mode": "local",
                    "simulation_conf": sim_config.serialize_ini(sim_conf),
                    "user_overrides": sim_config.serialize_ini(user_conf)}

        if cmd in ("CS_UPDATE_CONFIG", "UPDATE_CONFIG"):
            content = d.get("content")
            if content is None:
                return {"status": "ERROR", "message": "missing 'content'"}
            try:
                sim_config.validate_ini_text(content)
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            base = Path(__file__).resolve().parent.parent.parent / "configs"
            (base / "simulation.conf").write_text(content, encoding="utf-8")
            self.engine.reload_config()
            return {"status": "SUCCESS", "message": "simulation.conf updated"}

        if cmd in ("CS_UPDATE_USER_OVERRIDES",):
            content = d.get("content")
            if content is None:
                return {"status": "ERROR", "message": "missing 'content'"}
            try:
                sim_config.validate_ini_text(content)
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            base = Path(__file__).resolve().parent.parent.parent / "configs"
            (base / "user-overrides.conf").write_text(content, encoding="utf-8")
            self.engine.reload_config()
            return {"status": "SUCCESS", "message": "user-overrides.conf updated"}

        # ── kill switch ────────────────────────────────────────────────────
        if cmd in ("CS_KILL_SWITCH",):
            on = bool(d.get("on", d.get("kill_switch", False)))
            self.engine.set_kill_switch(on)
            return {"status": "SUCCESS", "kill_switch": on}

        if cmd in ("CS_GET_KILL_SWITCH",):
            # Read for the hub's kill-switch banner. Sits BEFORE the
            # NOT_IMPLEMENTED matcher (whose set includes "GET") so the hub's
            # GET /kill-switch doesn't hit a dead command.
            return {"status": "SUCCESS",
                    "kill_switch": self.engine.kill_switch_active()}

        # ── demo scenarios (named per-client failure presets, TTL + auto-expiry)
        # None of CS_DEMO_* / CS_GET_DEMO_* match the NOT_IMPLEMENTED matcher's
        # second-segment set {QUEUE,GET,CLEAR,...} except the GET pair ("GET" is
        # in the set), so all four sit here before the matcher.
        if cmd in ("CS_DEMO_SCENARIO",):
            hostname = str(d.get("hostname") or "").strip()
            scenario = str(d.get("scenario") or "").strip()
            if not hostname or not scenario:
                return {"status": "ERROR", "message": "missing 'hostname' or 'scenario'"}
            try:
                summ = await self.demo.apply(hostname, scenario,
                                             str(d.get("triggered_by") or ""))
            except ValueError as exc:
                return {"status": "ERROR", "message": str(exc)}
            return {"status": "SUCCESS", **summ}

        if cmd in ("CS_DEMO_CLEAR",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            cleared = await self.demo.clear(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}

        if cmd in ("CS_GET_DEMO_ACTIVE",):
            return {"status": "SUCCESS",
                    "active": await self.demo.active_summary()}

        if cmd in ("CS_GET_DEMO_SCENARIOS",):
            return {"status": "SUCCESS", "scenarios": DEMO_SCENARIOS}

        # ── per-client override control panel (hub/UI → registry overrides) ──
        # The legacy cs webui-spoke exposed a per-client "Control Panel" with
        # live sim-flag toggles (kill_switch/dns_fail/iperf/download/www_traffic/
        # ping_test/ssidpw_fail/auth_fail/dhcp_fail/port_flap/assoc_fail) +
        # Apply / Clear / Apply-to-ALL. The hub forwards each action here as a
        # CS_* command; these handlers wrap ClientRegistry.set_overrides /
        # clear_overrides (the SAME persisted store the /api/config delivery
        # reads, unlike the ephemeral demo flags). GET + CLEAR sit before the
        # NOT_IMPLEMENTED matcher (both second-segments are in its set); SET is
        # not in the set but would fall through to "Unknown command", so it gets
        # an explicit handler too.
        if cmd in ("CS_GET_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            entry = self.registry.get(hostname) or {}
            return {"status": "SUCCESS", "hostname": hostname,
                    "overrides": entry.get("overrides", {})}

        if cmd in ("CS_SET_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            overrides = d.get("overrides") or {}
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            if not isinstance(overrides, dict):
                return {"status": "ERROR", "message": "'overrides' must be an object"}
            entry = await self.registry.set_overrides(hostname, overrides)
            return {"status": "SUCCESS", "hostname": hostname,
                    "overrides": entry.get("overrides", {})}

        if cmd in ("CS_CLEAR_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            await self.registry.clear_overrides(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": True}

        if cmd in ("CS_SET_ALL_CLIENT_OVERRIDES",):
            overrides = d.get("overrides") or {}
            if not isinstance(overrides, dict):
                return {"status": "ERROR", "message": "'overrides' must be an object"}
            applied = 0
            for hostname in list(self.registry.get_all().keys()):
                await self.registry.set_overrides(hostname, dict(overrides))
                applied += 1
            return {"status": "SUCCESS", "applied": applied,
                    "overrides": dict(overrides)}

        if cmd in ("CS_PURGE_CLIENTS",):
            # The "Purge Clients" button (original cs-webui
            # DELETE /api/clients/history): drop every registered client from
            # memory + delete clients.json on disk — irreversible. The hub/UI
            # forwards here via DELETE /sim/api/{tenant}/clients. Returns the
            # count removed so the UI can confirm. Sits before the
            # NOT_IMPLEMENTED matcher below (second-segment "PURGE" isn't in
            # its set, but without this handler it would fall through to
            # "Unknown command").
            res = await self.registry.purge()
            return {"status": "SUCCESS", **res}

        # ── Per-host USB VMID overrides ──────────────────────────────────────
        # Optional per-host vmid_start/vmid_end/vm_set_override that override the
        # global range for one proxmox host (the pxmx agent honors a non-default
        # range over its own hostname-suffix batch derivation). Persisted by
        # CSSettings in cs_settings.json under ``host_usb_overrides``.
        if cmd == "CS_GET_HOST_USB_OVERRIDES":
            return {"status": "SUCCESS",
                    "overrides": self.settings.all_host_usb_overrides()}

        if cmd == "CS_SET_HOST_USB_OVERRIDE":
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            knobs = d.get("knobs") or d.get("overrides") or {}
            if not isinstance(knobs, dict):
                return {"status": "ERROR", "message": "'knobs' must be an object"}
            merged = self.settings.set_host_usb_override(hostname, knobs)
            return {"status": "SUCCESS", "hostname": hostname, "knobs": merged}

        if cmd == "CS_CLEAR_HOST_USB_OVERRIDE":
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            cleared = self.settings.clear_host_usb_override(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}

        # ── Client-Simulation ingest (unified pxmx agent → hub → here) ───────
        # The hub's AGENT_RELAY_UP CS_* dispatcher forwards each CS_* agent event
        # here as a CS_INGEST_* (or CS_STORE_PROXMOX_TOKEN) command carrying the
        # agent's hostname + the event data. D1 wires telemetry end-to-end; the
        # event/log/progress/hw/reset handlers are recorded (best-effort) and
        # fully wired in Phase E; CS_INGEST_COMMAND_RESULT also closes the
        # deferred long-op ack loop; CS_STORE_PROXMOX_TOKEN persists the token
        # and kicks sim-tag sync (Phase F).
        if cmd == "CS_INGEST_TELEMETRY":
            hostname = (d.get("hostname") or "").strip()
            entry = self.deploy.ingest_telemetry(hostname, d)
            if not entry:
                return {"status": "ERROR", "message": "missing hostname"}
            # Phase F: a fresh VM list is the trigger for sim-tag sync (legacy
            # drives it off client-status ingest; here the per-host VM list is
            # the source of truth). Best-effort background sweep — no-op until
            # the client registry lands (registry=None). Never blocks the ack.
            asyncio.create_task(self._maybe_sync_sim_tags())
            return {"status": "SUCCESS", "hostname": hostname,
                    "vm_count": entry.get("vm_count", 0)}

        if cmd in ("CS_INGEST_LOG", "CS_INGEST_PROGRESS",
                   "CS_INGEST_WATCHDOG_EVENT", "CS_INGEST_HW_RESET"):
            hostname = (d.get("hostname") or "").strip()
            kind = cmd[len("CS_INGEST_"):]
            self.deploy.ingest_event(hostname, kind.lower(), d)
            return {"status": "SUCCESS", "hostname": hostname, "ingested": kind.lower()}

        if cmd == "CS_INGEST_COMMAND_RESULT":
            # Terminal result of a long op. Record it for the per-host event
            # buffer AND close the deferred ack loop so the cs UI marks the
            # command completed/failed (Phase E). cs_cmd_id is the correlation
            # key the bridge deferred the ack on.
            hostname = (d.get("hostname") or "").strip()
            self.deploy.ingest_event(hostname, "command_result", d)
            cs_cmd_id = d.get("cs_cmd_id")
            status = d.get("status")  # completed | failed
            if cs_cmd_id and status:
                ack_status = "completed" if str(status).lower() == "completed" else "failed"
                try:
                    await self.queue.ack_command(cs_cmd_id, ack_status,
                                                 d.get("message"), d.get("result"))
                except Exception as e:  # noqa: BLE001 — best-effort; the event is recorded
                    logger.warning("CS_INGEST_COMMAND_RESULT ack failed for %s: %s",
                                   cs_cmd_id, e)
            return {"status": "SUCCESS", "hostname": hostname,
                    "ingested": "command_result", "acked": bool(cs_cmd_id and status)}

        if cmd == "CS_STORE_PROXMOX_TOKEN":
            # Phase F: persist the per-host Proxmox API token and kick sim-tag
            # sync. The token secret is stored to data/proxmox_tokens.json and
            # is NEVER logged (TokenStore.save logs only the hostname). Reply
            # carries no token — only {stored, hostname, token_set} so the hub
            # log can't leak it via request_response's result echo.
            hostname = (d.get("hostname") or "").strip()
            token = d.get("token")
            if not hostname or not token:
                return {"status": "ERROR",
                        "message": "missing 'hostname' or 'token'"}
            self.tokens.save(hostname, token)
            # A fresh token may fix the very auth failure that put us in back-off
            # — clear it (and the debounce) so the sync retries now with the new
            # token instead of waiting out the 10-min penalty.
            self._sim_tag_backoff_until = 0.0
            self._sim_tag_last_ts = 0.0
            asyncio.create_task(self._maybe_sync_sim_tags())
            return {"status": "SUCCESS", "stored": True, "hostname": hostname,
                    "token_set": True}

        # ── Client-Simulation command queue (D2) ────────────────────────────
        # The cs UI enqueues VM actions here (CS_QUEUE_COMMAND); the LM hub's
        # CSBridgePoller polls the inbox (CS_POLL_AGENT_INBOX), relays each
        # command to the unified pxmx agent as CS_COMMAND, and acks the terminal
        # result back (CS_ACK_COMMAND). USB config (CS_GET_USB_CONFIG) is read
        # by the bridge and pushed to the agent's client_simulation.usb_config.
        # These handlers sit BEFORE the NOT_IMPLEMENTED matcher below so the
        # matcher's {"QUEUE","GET",...} set doesn't swallow them.
        if cmd == "CS_QUEUE_COMMAND":
            target = str(d.get("target") or "proxmox").strip() or "proxmox"
            action = str(d.get("action") or "").strip()
            if not action:
                return {"status": "ERROR", "message": "missing 'action'"}
            try:
                res = await self.queue.enqueue(target, action,
                                               d.get("args") or {},
                                               command_type=d.get("type"))
            except ValueError as exc:
                # Safeguard refusal (protected vmid / below sim floor).
                return {"status": "ERROR", "message": str(exc)}
            # Live-deliver to a connected client WS agent (no waiting for sync).
            await client_api.push_pending(self, target)
            return {"status": "SUCCESS", "command": res["command"],
                    "created": res["created"], "expired": res["expired"],
                    "purged": res["purged"]}

        if cmd == "CS_POLL_AGENT_INBOX":
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            res = await self.queue.poll_agent_inbox(hostname)
            return {"status": "SUCCESS", **res}

        if cmd == "CS_ACK_COMMAND":
            res = await self.queue.ack_command(d.get("id"), d.get("status"),
                                               d.get("message"), d.get("result"))
            if not res.get("ok"):
                return {"status": "ERROR", "message": res.get("message", "ack failed")}
            return {"status": "SUCCESS", **res}

        if cmd == "CS_GET_USB_CONFIG":
            hostname = str(d.get("hostname") or "").strip() or None
            cfg = await self.queue.get_usb_config(hostname)
            return {"status": "SUCCESS", "usb_config": cfg}

        if cmd == "CS_GET_COMMANDS":
            return {"status": "SUCCESS",
                    "commands": await self.queue.list_commands()}

        if cmd == "CS_CLEAR_COMMANDS":
            # Cancel all non-terminal commands (optionally scoped to a target).
            # Sits BEFORE the NOT_IMPLEMENTED matcher below (whose set includes
            # "CLEAR") so the hub's Clear-queue + pre-teardown-expire routes work.
            target = str(d.get("target") or "").strip() or None
            res = await self.queue.clear_commands(target)
            return {"status": "SUCCESS", **res}

        if cmd == "CS_DELETE_COMMAND":
            # Per-row delete (any status). "DELETE" isn't in the matcher set, so
            # without this handler it would fall through to "Unknown command".
            res = await self.queue.delete_command(d.get("id"))
            if not res.get("ok"):
                return {"status": "ERROR",
                        "message": res.get("message", "command not found")}
            return {"status": "SUCCESS", **res}

        if cmd == "CS_UPDATE_SETTINGS":
            # cs UI edits a USB-provision / watchdog knob; persisted to data/cs_settings.json.
            patch = d.get("settings") or d.get("patch") or {}
            if not isinstance(patch, dict):
                return {"status": "ERROR", "message": "'settings' must be an object"}
            return {"status": "SUCCESS",
                    "settings": self.settings.update(patch)}

        if cmd == "CS_CONFIG_UPDATE":
            # Hub pushes hub-owned provisioning config (usb_vidpids,
            # usb_ignored_vidpids, usb_auto_provision, template ids, VLAN
            # ranges, reclone concurrency, ... + optional sim/user-overrides
            # INI text). The legacy cs webui-spoke applied these via
            # _apply_hub_config; this spoke MUST do the same or certification
            # pushes are silently dropped: usb_vidpids stays "[]" in settings,
            # the cs_bridge pulls an empty ``vidpids`` via CS_GET_USB_CONFIG
            # every 60s, the agent's _dongle_vidpids returns 0, and
            # auto-provision never fires ("no dongle_vidpids configured").
            applied = self._apply_hub_config(d if isinstance(d, dict) else {})
            return {"status": "SUCCESS", "applied": applied}

        # ── local Setup-tab config (hub_config / central) ───────────────────
        # This spoke owns these knobs itself now (local_store.py) instead of
        # relaying them from an LM hub tenant store — see that module's
        # docstring. _apply_hub_config below is the SAME logic CS_CONFIG_UPDATE
        # already uses, so a locally-saved hub_config flows to the settings
        # store (and any cs-dialed pxmx agent) exactly like a hub-pushed one.
        if cmd in ("CS_GET_HUB_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_hub_config()}

        if cmd in ("CS_SET_HUB_CONFIG",):
            enabled = bool(d.get("hub_config_enabled", False))
            hc = d.get("hub_config") or {}
            self.local_store.set_hub_config(enabled, hc)
            applied = self._apply_hub_config(hc) if enabled else []
            return {"status": "SUCCESS", "applied": applied}

        if cmd in ("CS_RESET_HUB_CONFIG",):
            result = self.local_store.reset_hub_config()
            if result.get("hub_config_enabled"):
                self._apply_hub_config(result.get("hub_config") or {})
            return {"status": "SUCCESS", **result}

        if cmd in ("CS_GET_CENTRAL_CONFIG",):
            return {"status": "SUCCESS", "central_config": self.local_store.get_central_config()}

        if cmd in ("CS_SET_CENTRAL_CONFIG",):
            self._merge_central_config(d.get("central_config") or {})
            return {"status": "SUCCESS"}

        if cmd in ("CS_GET_CENTRAL_SITES_CONFIG",):
            return {"status": "SUCCESS", **self.local_store.get_central_sites_config()}

        if cmd in ("CS_SET_CENTRAL_SITES_CONFIG",):
            cfg = d if isinstance(d, dict) else {}
            # Validate sim_quotas against the sims this tenant's simulation.conf
            # actually offers; drop unknown/invalid entries and surface errors so
            # the UI can report them. The rest of central_sites_config
            # (monitored_checks/hardware_checks/site_mappings) passes through
            # unchanged — sim_quotas is an additive field.
            try:
                import sim_quota
                sims = [s["sim_id"] for s in sim_quota.available_sims(self.settings.config_dir)]
                clean, errs = sim_quota.validate_sim_quotas(cfg.get("sim_quotas"), sims)
                if errs:
                    logger.warning("CS_SET_CENTRAL_SITES_CONFIG: sim_quotas errors: %s", errs)
                cfg = {**cfg, "sim_quotas": clean}
            except Exception as exc:  # noqa: BLE001 — never block the save
                logger.warning("sim_quotas validate failed: %s", exc)
            self.local_store.set_central_sites_config(cfg)
            self.central_poller.reload()
            return {"status": "SUCCESS"}

        if cmd in ("CS_GET_CENTRAL_AVAILABLE",):
            return await self.central_poller.available_checks()

        if cmd in ("CS_TEST_CENTRAL",):
            return await self.central_poller.test_connection()

        if cmd in ("CS_CENTRAL_BROWSE",):
            return await self.central_poller.browse()

        if cmd == "CS_GET_SIM_QUOTA_CATALOG":
            # The Sim-Quota UI (Config → Sim Quotas) renders against this: the
            # sims/sites derived from this tenant's simulation.conf + the global
            # suggested alert→sim marriages. Sims come from simulation.conf, not
            # a hardcoded list, so a tenant that adds a flag to its buckets sees
            # it here automatically.
            try:
                import sim_quota
                csc = self.local_store.get_central_sites_config() or {}
                cat = sim_quota.sim_quota_catalog(
                    self.settings.config_dir, csc.get("site_mappings"))
                return {"status": "SUCCESS", **cat}
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_GET_SIM_QUOTA_CATALOG failed: %s", exc)
                return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}",
                        "sims": [], "sites": [], "suggested": {}, "meta": {}}

        if cmd == "CS_GET_SIM_QUOTA_STATE":
            # Engine ledger snapshot for the quota-state view (Chunk 4): which
            # clients are currently assigned to each effective quota.
            eng = getattr(self, "sim_quota_engine", None)
            snap = eng.snapshot() if eng is not None else {}
            return {"status": "SUCCESS",
                    "effective": self.local_store.get_effective_sim_quotas(),
                    "ledger": snap}

        if cmd == "CS_GET_PXMX_SITE_MAP":
            # Operator-assigned pxmx server → site map (Config → PXMX Sites). The
            # engine resolves a client's site via its hosting server's entry.
            # Also return the currently-connected pxmx agents so the UI can list
            # assignable servers + flag servers whose agent has dropped.
            agents = []
            try:
                agents = self._get_agents().get("agents", [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_GET_PXMX_SITE_MAP: agents list failed: %s", exc)
            return {"status": "SUCCESS",
                    "pxmx_site_map": self.local_store.get_pxmx_site_map(),
                    "agents": agents}

        if cmd == "CS_SET_PXMX_SITE_MAP":
            # Validate each assigned site against the sites this tenant's
            # simulation.conf + Central site_mappings actually offer; drop
            # unknown sites (keep the host mapping so the operator can fix the
            # typo in-place) and surface errors. Unknown HOSTS are kept too — a
            # server may be temporarily disconnected but still assigned.
            raw = d.get("pxmx_site_map") if isinstance(d, dict) else None
            raw = raw if isinstance(raw, dict) else (d if isinstance(d, dict) else {})
            errs: list = []
            try:
                import sim_quota
                csc = self.local_store.get_central_sites_config() or {}
                valid = set(sim_quota.available_sites(
                    self.settings.config_dir, csc.get("site_mappings")))
                clean = {}
                for host, site in raw.items():
                    h = str(host).strip()
                    s = str(site or "").strip()
                    if not h:
                        continue
                    if s and valid and s not in valid:
                        errs.append(f"{h}: unknown site '{s}'")
                    clean[h] = s
            except Exception as exc:  # noqa: BLE001 — never block the save
                logger.warning("CS_SET_PXMX_SITE_MAP: validate failed: %s", exc)
                clean = {str(k).strip(): str(v or "").strip()
                         for k, v in raw.items() if str(k).strip()}
            saved = self.local_store.set_pxmx_site_map(clean)
            # Re-resolve sites on the next sweep — the engine reads the map at
            # the top of each reconcile, so just nudge it now for promptness.
            self._trigger_sim_quota_reconcile()
            return {"status": "SUCCESS", "pxmx_site_map": saved,
                    "errors": errs}

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

    # ── hub-pushed config (CS_CONFIG_UPDATE) ───────────────────────────────
    # Keys the hub sends that map 1:1 to a CSSettings key (consumed by
    # ``CSSettings.usb_config_payload`` → cs_bridge → agent usb_config).
    _HUB_DIRECT_KEYS = (
        "usb_vidpids", "usb_ignored_vidpids",
        "t1_pci_vidpids", "t3_pci_vidpids", "usb_auto_provision",
        "usb_missing_timeout", "usb_max_slots", "vm_image_1_pct",
        "reclone_concurrency", "l1_vlan_start", "l1_vlan_end",
        "vmid_start", "vmid_end", "vm_set_override", "use_all_dongles",
        "guest_agent_watchdog_enabled", "guest_agent_grace_minutes",
        "guest_agent_check_interval_minutes", "guest_agent_reboot_after_minutes",
        "guest_agent_reclone_after_minutes", "watchdog_reboot_enabled",
        "cpu_provision_threshold", "cpu_delete_threshold",
        "mem_provision_threshold", "mem_delete_threshold",
        "protected_vmids",
    )
    # Hub keys that must be renamed to land in their CSSettings counterpart
    # (the hub UI/label uses ``vm_image_*``; the settings store + agent read
    # ``image*_template_*``). Without this remap the template IDs never reach
    # the agent even after certification is unblocked.
    _HUB_KEY_REMAP = {
        "vm_image_1_template_id":  "image1_template_id",
        "vm_image_2_template_id":  "image2_template_id",
        "vm_image_1_template_spec": "image1_template_spec",
        "vm_image_2_template_spec": "image2_template_spec",
    }

    def _merge_central_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Sentinel-merge a Central API config patch into local_store and rebuild
        the poller's ArubaClient. Shared by CS_SET_CENTRAL_CONFIG (standalone
        local UI) and the hub-pushed CS_CONFIG_UPDATE path (_apply_hub_config) so
        BOTH entry points persist creds AND reload the client — mirrors the source
        webui-spoke _apply_hub_config central_config handling (server.py).

        Sentinel rule: an empty/None value KEEPS the stored value (so a partial
        save — e.g. changing only Mode, or a hub push that omits unchanged
        secrets — never wipes creds). A new key with an empty value is still
        written (first-time provisioning of a placeholder field)."""
        current = self.local_store.get_central_config()
        merged = dict(current)
        for k, v in (cfg or {}).items():
            if v not in (None, ""):
                merged[k] = v
            elif k not in current:
                merged[k] = v
        self.local_store.set_central_config(merged)
        self.central_poller.reload()
        return merged

    def _apply_hub_config(self, patch: Dict[str, Any]) -> list:
        """Apply a hub-pushed CS_CONFIG_UPDATE patch to the cs settings store.

        Mirrors the legacy webui-spoke ``_apply_hub_config`` for the keys this
        spoke consumes (the ``usb_config_payload`` knobs + the sim/user-override
        INI files). Hub keys with no CSSettings equivalent (repo_branch,
        reclone_schedule_*, vm_silent_timeout, ignored_hostnames) are ignored
        here — they are legacy-only and this spoke has no consumer for them.
        Returns the list of applied keys (for the hub log / reply).
        """
        if not isinstance(patch, dict) or not patch:
            return []
        update: Dict[str, Any] = {"hub_managed": True}
        applied: list = []
        # Aruba Central creds pushed from the hub (Setup -> Central API -> Save).
        # WITHOUT this branch the push is silently dropped: local_store never gets
        # the creds and the poller keeps _client=None, so browse() returns zero
        # sites and the Central-site dropdown / Sites-Alerts-Clients tabs stay
        # empty. The source webui-spoke applies central_config in _apply_hub_config
        # too (server.py) — this mirrors it via the shared sentinel-merge helper.
        if "central_config" in patch:
            cc = patch.get("central_config")
            self._merge_central_config(cc if isinstance(cc, dict) else {})
            applied.append("central_config")
        # Hub-pushed central_sites_config (monitored_checks/hardware_checks/
        # site_mappings + sim_quotas): apply to local_store + reload the poller
        # so a hub-side Config → Sim Quotas / Central save reaches this spoke.
        if "central_sites_config" in patch:
            csc = patch.get("central_sites_config")
            if isinstance(csc, dict):
                self.local_store.set_central_sites_config(csc)
                self.central_poller.reload()
                applied.append("central_sites_config")
        # Hub-pushed effective sim quotas (global defaults merged with this
        # tenant's overrides, enabled-only) — the SimQuotaEngine's input. Persist
        # + trigger a reconcile so the engine picks up the new target set.
        if "effective_sim_quotas" in patch:
            eff = patch.get("effective_sim_quotas")
            if isinstance(eff, list):
                self.local_store.set_effective_sim_quotas(eff)
                self._trigger_sim_quota_reconcile()
                applied.append("effective_sim_quotas")
        for key in self._HUB_DIRECT_KEYS:
            if key in patch:
                update[key] = patch[key]
                applied.append(key)
        for hub_key, settings_key in self._HUB_KEY_REMAP.items():
            if hub_key in patch:
                update[settings_key] = patch[hub_key]
                applied.append(f"{hub_key}->{settings_key}")
        # Spoke-side relay timeouts (send_to_agent long-op / fast windows) —
        # hub-configurable via Setup → General. Not a CSSettings/agent key: stored
        # on the spoke and read by the SPOKE_RELAY forward (send_to_agent).
        for _k, _attr in (("agent_relay_timeout_long_s", "_relay_timeout_long"),
                          ("agent_relay_timeout_fast_s", "_relay_timeout_fast")):
            if _k in patch:
                try:
                    setattr(self, _attr, max(1.0, float(patch[_k])))
                    applied.append(_k)
                except (TypeError, ValueError):
                    pass
        # GitHub repo/token (Source of Truth = GitHub) — held in memory only.
        if "github_config" in patch:
            gc = patch.get("github_config")
            self._github_config = dict(gc) if isinstance(gc, dict) else {}
            applied.append("github_config")

        # Effective Source of Truth for this write: prefer the patch value, else
        # the persisted flag, else 'github'.
        _cfg_dir = self.settings.config_dir
        if "config_source" in patch:
            _source = "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
        else:
            try:
                _source = (_cfg_dir / "hub-config-source").read_text(encoding="utf-8").strip().lower()
            except Exception:  # noqa: BLE001
                _source = "github"
        _gh_token = bool(str((self._github_config or {}).get("github_token") or "").strip())

        # Optional simulation.conf / user-overrides.conf INI text.
        #  - Source=GitHub WITH a token: write the REPO file and commit+push it so
        #    GitHub stays authoritative (edit survives the next repo sync); the
        #    stale hub override is removed so load_configs doesn't double-apply it.
        #  - otherwise: write configs/hub-*-overrides.conf (hub-managed override).
        #    None = clear the override so the base file applies.
        _push_map = {}  # repo-relative path -> content, for the fetch+reset+push
        for override_key, hub_filename, repo_filename in (
            ("sim_conf_override", "hub-sim-overrides.conf", "simulation.conf"),
            ("user_conf_override", "hub-user-overrides.conf", "user-overrides.conf"),
        ):
            if override_key not in patch:
                continue
            text = patch[override_key]
            if _source == "github" and text is not None and not _gh_token:
                # Source=GitHub but this spoke has NO token in memory. The token
                # is held in-memory only (never persisted), so a spoke restart
                # (hourly self-update / reboot / crash) wipes it until the hub
                # re-delivers github_config. The hub accepted the save (its store
                # has the token), but we can't push — warn LOUDLY so the operator
                # catches it, instead of seeing a silent revert on the next repo
                # sync (the "old GitHub version on sync" symptom). Fall through to
                # the hub-override write so the edit at least applies locally.
                logger.warning(
                    "CS_CONFIG_UPDATE[%s]: %s received with source=github but no "
                    "github_token in memory (spoke restarted since the key was "
                    "delivered?) — will NOT push; the repo file reverts on the next "
                    "sync. Re-save the GitHub credentials (Setup → Sim Config → "
                    "GitHub) to re-deliver the token.",
                    self.spoke_id, repo_filename)
            if _source == "github" and _gh_token and text is not None:
                repo_path = _cfg_dir / repo_filename
                try:
                    repo_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = repo_path.with_suffix(".tmp")
                    tmp.write_text(str(text), encoding="utf-8")
                    tmp.replace(repo_path)   # immediate local effect
                    hub_path = _cfg_dir / hub_filename  # drop stale hub override
                    if hub_path.exists():
                        hub_path.unlink()
                    _push_map[f"configs/{repo_filename}"] = str(text)
                    applied.append(f"{repo_filename}:github")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CS_CONFIG_UPDATE[%s]: %s (github) write failed: %s",
                                   self.spoke_id, repo_filename, exc)
                continue
            override_path = _cfg_dir / hub_filename
            try:
                if text is None:
                    if override_path.exists():
                        override_path.unlink()
                    applied.append(f"{override_key}:cleared")
                else:
                    override_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = override_path.with_suffix(".tmp")
                    tmp.write_text(str(text), encoding="utf-8")
                    tmp.replace(override_path)
                    applied.append(f"{override_key}:updated")
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE: %s write failed: %s",
                               override_path, exc)
        # Fire-and-forget the git commit+push (async; _apply_hub_config is sync).
        if _push_map:
            try:
                _t = asyncio.get_event_loop().create_task(
                    self._push_files_to_github(_push_map, "WebUI: update simulation config"))
                self._gh_push_tasks.add(_t)
                def _push_done(t, _set=self._gh_push_tasks) -> None:
                    _set.discard(t)
                    if t.cancelled():
                        logger.warning("github push[%s]: task cancelled before completion",
                                       self.spoke_id)
                        return
                    exc = t.exception()  # defensive — _push_files_to_github catches its own
                    if exc:
                        logger.warning("github push[%s]: task raised %r", self.spoke_id, exc)
                _t.add_done_callback(_push_done)
                logger.info("CS_CONFIG_UPDATE[%s]: scheduled github push for %s",
                            self.spoke_id, list(_push_map))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE[%s]: github push schedule failed: %s",
                               self.spoke_id, exc)
        # Config Source of Truth ('hub' | 'github'). In 'hub' mode sim_config.
        # load_configs uses the hub override files as the WHOLE config and ignores
        # the repo base (so a repo pull can never revert hub edits). Persisted as a
        # flag file the loader reads. Default 'github' preserves the repo-base merge.
        if "config_source" in patch:
            src = "hub" if str(patch.get("config_source")).lower() == "hub" else "github"
            try:
                (self.settings.config_dir / "hub-config-source").write_text(src, encoding="utf-8")
                applied.append(f"config_source:{src}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CONFIG_UPDATE: config_source write failed: %s", exc)
        if applied:
            self.settings.update(update)
        logger.info("CS_CONFIG_UPDATE: applied %s",
                    ", ".join(applied) if applied else "no changes")
        return applied

    def _trigger_sim_quota_reconcile(self) -> None:
        """Immediate best-effort SimQuotaEngine reconcile on an
        effective_sim_quotas push (the periodic loop also sweeps every 60s)."""
        eng = getattr(self, "sim_quota_engine", None)
        if eng is not None:
            eng.trigger()

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

