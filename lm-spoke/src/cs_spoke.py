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
from pathlib import Path
from typing import Any, Dict

from simulation_engine import SimulationEngine
import sim_config
from proxmox_deploy import ProxmoxDeploy
from command_queue import CommandQueue, CSSettings
from token_store import TokenStore, sync_all_sim_tags
from client_registry import ClientRegistry
import client_api  # for client_api.push_pending (live command delivery to WS agents)

try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke  # type: ignore

logger = logging.getLogger("CSSpoke")


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
        self.registry = ClientRegistry(data_dir)
        self.settings = CSSettings(data_dir, config_dir)
        self.queue = CommandQueue(data_dir, self.settings)
        self.deploy = ProxmoxDeploy()
        # Phase F: per-host Proxmox token store + sim-tag sync (registry=None in
        # Phase 2/3, so sim-tag sync is a no-op until the client registry lands).
        self.tokens = TokenStore(data_dir)
        self._sim_tag_cache: Dict[tuple, set] = {}
        self._sim_tag_sync_lock = asyncio.Lock()

    # ── BaseSpoke: status (fallback for *_GET_STATUS) ───────────────────────
    async def get_status(self) -> Dict[str, Any]:
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
        # cs_spoke.py lives at <repo>/lm-spoke/src/cs_spoke.py; the tracked,
        # autobumped VERSION file is at the REPO ROOT (<repo>/VERSION, deployed
        # at /opt/lm/cs/VERSION per install_cs.sh), one dir above the legacy
        # lm-spoke/VERSION path. Try repo-root first, then lm-spoke/ as a
        # fallback for any layout that places VERSION beside the spoke.
        here = Path(__file__).resolve().parent  # .../lm-spoke/src
        for p in (
            here.parent.parent / "VERSION",   # <repo>/VERSION  (dev + /opt/lm/cs/VERSION)
            here.parent / "VERSION",          # <repo>/lm-spoke/VERSION (fallback)
        ):
            try:
                if p.exists():
                    v = p.read_text().strip()
                    if v:
                        return v
            except Exception:  # noqa: BLE001
                pass
        return "unknown"

    # ── Phase F: sim-tag sync (driven off CS_INGEST_TELEMETRY / token store) ──
    async def _maybe_sync_sim_tags(self) -> None:
        """Best-effort sim-tag sweep (legacy ``_sync_all_vm_sim_tags``).

        Re-entrant via a lock so overlapping telemetry frames / token stores
        don't double-run. No-op until the client registry is wired
        (``registry=None`` → ``_client_sim_map`` returns ``{}``). Never raises —
        a failure here must not break telemetry ingest."""
        if self.registry is None:
            return  # nothing to sync until the client registry lands
        try:
            async with self._sim_tag_sync_lock:
                await sync_all_sim_tags(self.deploy, self.tokens, self.registry,
                                         applied_cache=self._sim_tag_cache)
        except Exception as e:  # noqa: BLE001
            logger.debug("sim-tag sync skipped: %s", e)

    # ── command dispatch ───────────────────────────────────────────────────
    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Command: %s", command_type)
        cmd = command_type.upper()
        d = data or {}

        # ── identity / status ──────────────────────────────────────────────
        if cmd in ("GET_VERSION", "CS_GET_VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}

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
            base = Path(__file__).resolve().parent.parent.parent / "configs"
            return {"status": "SUCCESS", "mode": "local",
                    "simulation_conf": _read(base / "simulation.conf"),
                    "user_overrides": _read(base / "user-overrides.conf")}

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
        "usb_vidpids", "usb_ignored_vidpids", "usb_auto_provision",
        "usb_missing_timeout", "usb_max_slots", "vm_image_1_pct",
        "reclone_concurrency", "l1_vlan_start", "l1_vlan_end",
        "vmid_start", "vm_set_override", "use_all_dongles",
        "guest_agent_watchdog_enabled", "guest_agent_grace_minutes",
        "guest_agent_check_interval_minutes", "guest_agent_reboot_after_minutes",
        "guest_agent_reclone_after_minutes", "watchdog_reboot_enabled",
        "cpu_provision_threshold", "mem_provision_threshold",
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
        for key in self._HUB_DIRECT_KEYS:
            if key in patch:
                update[key] = patch[key]
                applied.append(key)
        for hub_key, settings_key in self._HUB_KEY_REMAP.items():
            if hub_key in patch:
                update[settings_key] = patch[hub_key]
                applied.append(f"{hub_key}->{settings_key}")
        # Optional simulation.conf / user-overrides.conf INI text overrides.
        # None = clear the local override file so the GitHub-pulled file applies;
        # a string = write it to configs/hub-*-overrides.conf (merged on top of
        # simulation.conf by usb_config_payload's sim_phy read).
        for override_key, filename in (
            ("sim_conf_override", "hub-sim-overrides.conf"),
            ("user_conf_override", "hub-user-overrides.conf"),
        ):
            if override_key not in patch:
                continue
            text = patch[override_key]
            override_path = self.settings.config_dir / filename
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
        if applied:
            self.settings.update(update)
        logger.info("CS_CONFIG_UPDATE: applied %s",
                    ", ".join(applied) if applied else "no changes")
        return applied


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""