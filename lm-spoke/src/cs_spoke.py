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
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from simulation_engine import SimulationEngine
import sim_config
from proxmox_deploy import ProxmoxDeploy
from command_queue import CommandQueue, CSSettings
from token_store import TokenStore, compute_sim_tag_map, norm_hostname as _norm_hostname
from client_registry import ClientRegistry
from client_rows import build_client_rows
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
        # VM running but its sim client stopped reporting -> reclone. See
        # stale_client_reclone.py's module docstring for why this exists and
        # how it's distinct from guest_watchdog (pxmx agent) and the dongle-
        # health ladder. Started by CSControlPlane.run().
        from stale_client_reclone import StaleClientReclone
        self.stale_client_reclone = StaleClientReclone(self, data_dir)
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
        # Per-host signature of the last sim-tag map dispatched to each agent, so
        # an unchanged map is not re-sent every debounce ({hostname: sig-tuple}).
        self._sim_tag_cache: Dict[str, tuple] = {}
        # {hostname: reason} for hosts the last sweep could NOT tag. Surfaced by
        # CS_GET_SIM_TAG_HEALTH so "this server has no tags" is answerable
        # without reading spoke logs.
        self._sim_tag_skips: Dict[str, str] = {}
        self._sim_tag_sync_lock = asyncio.Lock()
        # Sim-tag sync is DEBOUNCED off the per-frame telemetry hot path: at most
        # once per _SIM_TAG_MIN_INTERVAL. It no longer PUTs to the Proxmox API from
        # this off-host spoke (that storm was the regression behind the recurring
        # CS_INGEST_TELEMETRY Request Timeouts) — it computes the desired tags and
        # dispatches them to each host's pxmx AGENT for a LOCAL qm/pct apply.
        self._sim_tag_last_ts = 0.0
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
        # Central On-Prem twin of central_status/central_poller — a SECOND Aruba
        # Central instance (same ArubaClient/API as cloud Central, separate
        # config + status slot + tracker shard files). Populated by a second
        # CentralPoller(instance="central_on_prem") in the same Simulations
        # Checks/Hardware/Client-Count shape so the on-prem tab renders on-prem
        # data identically and INDEPENDENTLY of cloud Central (no stepping on each
        # other). Started by CSControlPlane.run()/run_standalone_mode() alongside
        # the cloud Central + Mist loops.
        self.central_on_prem_status: Dict[str, Any] = {}
        self.central_on_prem_poller = CentralPoller(self, instance="central_on_prem")
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
    def create_spoke_tasks(self, websocket):
        """Return the per-connection background tasks this module contributes.

        Currently just the CS telemetry relay loop. This hook is invoked by the
        standalone ``CSControlPlane._create_spoke_tasks`` AND by the generic
        multi-role agent's ``RoleConnection`` when simulation is hosted as a
        role, so the relay runs identically in both deployment modes.
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
                # Bus exclusions ride the signature too — they cull dongles the
                # same way quarantine does, so a change here moves the WebUI's
                # available-dongle count and must mark the frame dirty.
                parts.append(("excl", tuple(sorted(str((x or {}).get("bus_path"))
                                                   for x in (hpx.get("excluded") or [])))))
                # Missing-dongle diagnostics: signature on the missing set + the
                # cause count, NOT the whole blob (kernel sample lines and
                # generated_at churn every collection and would make the frame
                # permanently dirty, defeating the conditional relay).
                _ud = hpx.get("usb_diagnostics") or {}
                parts.append(("usbdiag",
                              tuple(sorted(str((m or {}).get("bus_path"))
                                           for m in (_ud.get("missing") or []))),
                              len(_ud.get("causes") or []),
                              bool((_ud.get("uhubctl") or {}).get("supported"))))
                # Guest-agent watchdog: signature on the ACTIONS only, not ran_at
                # (which changes every sweep and would pin the frame dirty).
                _gw = hpx.get("guest_watchdog") or {}
                parts.append(("guestwd",
                              tuple(_gw.get("reset") or []),
                              tuple(_gw.get("power_cycled") or []),
                              tuple(_gw.get("started") or [])))
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
            # Per-site pool counts drive hub-side per-site quota apportionment —
            # a shift in which sites a spoke serves must force a send so the hub
            # re-apportions on the next push, not at the next heartbeat.
            parts.append(("pool", tuple(sorted(
                (str(k), str(v)) for k, v in (payload.get("pool_by_site") or {}).items()))))
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
        per-tick sleep is gated by ``self.control_plane._bp_send_interval(interval)`` (bottom of
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
        self.control_plane._relay_wake = asyncio.Event()
        # Stagger the first send so a freshly-ingested frame is more likely.
        await asyncio.sleep(2)
        while True:
            try:
                cs_mod = self
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
                payload = deploy.relay_payload(self.control_plane.spoke_id, self.control_plane.spoke_id, dhcp=dhcp)
                # relay_payload's own "central" field is a placeholder ({}) —
                # overlay this spoke's real CentralPoller output so a
                # hub-connected deployment's Simulations tab gets live data too.
                payload["central"] = getattr(cs_mod, "central_status", {}) or {}
                # Central On-Prem twin: overlay this spoke's real on-prem
                # CentralPoller output so a hub-connected deployment's Simulations
                # tab gets live on-prem data too (same shape as the Central block
                # above; the hub's data["central_on_prem"] read finds it).
                payload["central_on_prem"] = getattr(cs_mod, "central_on_prem_status", {}) or {}
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
                # Quota-engine per-site pool → telemetry so the hub can apportion
                # a site-scoped quota ONLY across spokes that actually hold
                # clients for that site (per-site apportionment), instead of
                # every bound cs spoke. The engine computes pool_counts() each
                # reconcile anyway; surfacing by_site here lets the hub's push
                # path read it from the CS_TELEMETRY cache with NO synchronous
                # CS_GET_SIM_QUOTA_STATE forward per push. Best-effort: never
                # break telemetry over a pool read.
                _qe = getattr(cs_mod, "sim_quota_engine", None)
                if _qe is not None:
                    try:
                        payload["pool_by_site"] = ((_qe.pool_counts() or {}).get("by_site") or {})
                    except Exception as e:  # noqa: BLE001
                        logger.debug("pool_by_site telemetry overlay failed: %s", e)
                # Draining flag: True while a self-update is running (git pull +
                # about to os._exit+relaunch). The hub reads this and, while set,
                # queues CS_CONFIG_UPDATE (and other request/reply) pushes to the
                # mailbox instead of firing a 5s request_response that would time
                # out when we exit mid-reply. A fresh process starts False, so
                # the first post-restart frame tells the hub to clear drain.
                payload["draining"] = bool(getattr(self.control_plane, "_draining", False))
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
                            "sender_id": self.control_plane.spoke_id,
                            "destination_id": "hub",
                        },
                        "payload": {"type": "CS_TELEMETRY", "data": payload},
                    }
                    await websocket.send(self.control_plane._encode_frame(msg))
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
                await asyncio.wait_for(self.control_plane._relay_wake.wait(),
                                       timeout=self.control_plane._bp_send_interval(interval))
            except asyncio.TimeoutError:
                pass
            self.control_plane._relay_wake.clear()


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

    def get_local_commit(self) -> str:
        """Best-effort ``git rev-parse HEAD`` at the repo root (same directory
        as the VERSION file in get_version()). '' if this isn't a git
        checkout or git isn't available — never raises. Process-lifetime
        cached: the commit only changes on a restart-triggering update, so
        there's no need to re-shell out on every call (unlike get_version's
        mtime-keyed VERSION read, which tracks a file that can change without
        a restart)."""
        cached = getattr(self, "_local_commit_cache", None)
        if cached is not None:
            return cached
        sha = ""
        try:
            import subprocess
            repo_root = Path(__file__).resolve().parent.parent.parent
            out = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                sha = out.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        self._local_commit_cache = sha
        return sha

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
    # Tagging moved to the pxmx AGENT: this (off-host) cs spoke computes the
    # desired `sim-` tags per VM (from the client registry) and dispatches each
    # host's map to that host's agent (PXMX_APPLY_SIM_TAGS), which applies them
    # with LOCAL `qm`/`pct set --tags`. The spoke no longer PUTs to the Proxmox
    # API — that per-VM PUT (rebuilding an SSL context each call) storm was what
    # caused the recurring CS_INGEST_TELEMETRY Request Timeouts → stale VM Server
    # / Overview / quota engine across the whole fleet.
    _SIM_TAG_SYNC_ENABLED = True
    _SIM_TAG_MIN_INTERVAL = 60.0    # at most one dispatch sweep per minute
    _SIM_TAG_DISPATCH_TIMEOUT = 60.0  # per-agent PXMX_APPLY_SIM_TAGS relay timeout

    async def _maybe_sync_sim_tags(self) -> None:
        """Debounced sim-tag sync — computes desired tags off the telemetry hot
        path and dispatches them to each pxmx AGENT for a LOCAL (qm/pct) apply.

        No Proxmox API PUT from this off-host spoke (that storm caused the
        CS_INGEST_TELEMETRY Request Timeouts). Per-host maps unchanged since the
        last dispatch are skipped (signature cache), and only hosts whose agent
        is currently connected are sent. The lock serializes dispatches so only
        one runs at a time — but there's no min-interval, so a real tag change
        dispatches immediately instead of waiting out a debounce window. No-op
        until the client registry is wired. Never raises."""
        if not self._SIM_TAG_SYNC_ENABLED:
            return
        if self.registry is None:
            return  # nothing to sync until the client registry lands
        if self._sim_tag_sync_lock.locked():
            return  # a dispatch is already running — don't pile another on
        try:
            async with self._sim_tag_sync_lock:
                self._sim_tag_last_ts = time.time()
                tag_map = compute_sim_tag_map(self.deploy, self.registry)
                if not tag_map:
                    return
                # hostname -> currently-connected agent id.
                # Joined on a NORMALIZED key (lowercased, domain stripped): the
                # telemetry hostname (proxmox_states key, from the agent's own
                # CS_TELEMETRY frame) and connected_agents[aid]["hostname"] are
                # two independently-reported strings. When they disagreed — FQDN
                # vs short name, or a case difference after a host rebuild — the
                # exact-match lookup below silently skipped that host on EVERY
                # sweep, so its VMs were never tagged (or froze at whatever tags
                # they had when the names last matched). Exact match still wins;
                # the normalized form is only a fallback.
                hn_to_aid: Dict[str, str] = {}
                hn_to_aid_norm: Dict[str, str] = {}
                for aid, info in (self.control_plane.connected_agents or {}).items():
                    hn = str((info or {}).get("hostname") or "").strip()
                    if hn:
                        hn_to_aid[hn] = aid
                        hn_to_aid_norm.setdefault(_norm_hostname(hn), aid)
                deploy_states = getattr(self.deploy, "proxmox_states", {}) or {}
                skipped: Dict[str, str] = {}
                for hostname, vmid_map in tag_map.items():
                    aid = (hn_to_aid.get(hostname)
                           or hn_to_aid_norm.get(_norm_hostname(hostname)))
                    if not aid:
                        # NOT silent: a host whose agent we can't resolve gets no
                        # tags at all, which is indistinguishable from "no sims
                        # running" when you look at Proxmox. Record it so
                        # _sim_tag_skips can answer "why does this host have no
                        # tags", and log the CHANGE (not every 60s sweep).
                        skipped[hostname] = "agent_not_connected"
                        continue
                    # Desired-vs-ACTUAL reconcile: fold each VM's LIVE sim- tags
                    # (reported in telemetry) into the signature. A VM that drifted
                    # — recloned, apply lost, or the tag never took — has the desired
                    # map UNCHANGED but the tag missing on the box; without the actual
                    # tags in the sig the cache suppresses the re-send forever (the
                    # "SIMs with no labels that never self-correct" case). With them
                    # in, the sig differs while the box is out of sync → it
                    # re-dispatches, and converges once actual == desired.
                    _actual = {}
                    for vm in (deploy_states.get(hostname, {}) or {}).get("vms", []) or []:
                        _vid = vm.get("vmid")
                        if _vid is None:
                            continue
                        _actual[str(_vid)] = tuple(sorted(
                            str(t) for t in (vm.get("tags") or [])
                            if str(t).startswith("sim-")))
                    # skip re-send only when desired AND the on-box tags are unchanged
                    sig = tuple(sorted(
                        (str(v), tuple(sorted(t)), _actual.get(str(v), ()))
                        for v, t in vmid_map.items()))
                    if self._sim_tag_cache.get(hostname) == sig:
                        continue
                    try:
                        resp = await self.control_plane.send_to_agent(
                            "PXMX_APPLY_SIM_TAGS", {"tags": vmid_map},
                            agent_id=aid, timeout=self._SIM_TAG_DISPATCH_TIMEOUT)
                        # The agent ACKs immediately and applies in the background
                        # (non-blocking, so tagging never stalls its serial command
                        # loop). If it SKIPPED because a prior apply was still
                        # running, this desired map was NOT applied — don't cache
                        # the signature, so the next telemetry frame re-dispatches
                        # it instead of the cache suppressing it forever.
                        if (resp or {}).get("skipped"):
                            logger.debug("sim-tags: agent %s busy, will re-dispatch (%s)",
                                         aid, hostname)
                            continue
                        self._sim_tag_cache[hostname] = sig
                        logger.debug("sim-tags: dispatched %d VM(s) to agent %s (%s)",
                                     len(vmid_map), aid, hostname)
                    except Exception as exc:  # noqa: BLE001 - one host must not abort the rest
                        skipped[hostname] = f"dispatch_failed: {exc}"
                        logger.debug("sim-tag dispatch to %s failed: %s", hostname, exc)
                # Change-gated report. Logging every sweep would be 1440 lines a
                # day of steady state; logging only transitions makes the event
                # findable without drowning it.
                if skipped != self._sim_tag_skips:
                    for hn, why in skipped.items():
                        if self._sim_tag_skips.get(hn) != why:
                            logger.warning(
                                "sim-tags: host %s NOT tagged (%s) — its VMs keep "
                                "whatever tags they already have. Connected agents: %s",
                                hn, why, sorted(hn_to_aid) or "none")
                    for hn in self._sim_tag_skips:
                        if hn not in skipped:
                            logger.info("sim-tags: host %s tagging recovered", hn)
                    self._sim_tag_skips = skipped
        except Exception as e:  # noqa: BLE001
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
            return {"status": "SUCCESS", "version": self.get_version(),
                    "commit_sha": self.get_local_commit()}

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

