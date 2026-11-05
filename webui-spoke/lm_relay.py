"""LM hub bridge for the combined Client-Sim spoke.

Runs alongside ``server.py`` (same process) and connects the spoke to the
Lab Manager hub (``lm/core``) over the LM signed websocket as module type
``Client-Sim``. It does two things:

1. **Telemetry relay** — periodically pushes the spoke's full state
   (``server._build_relay_telemetry_payload``) to the hub as ``CS_TELEMETRY``
   frames. The hub caches the latest per spoke and the Simulations read API
   serves from that cache.
2. **Command dispatch** — handles ``CS_*`` commands the hub sends via
   ``request_response``. Phase 1 implements status; action commands are
   filled in by Phase 4, routed through ``server._apply_relay_command_batch``
   (the same code path the old webui-hub relay used) plus dedicated action
   functions.

The standalone spoke UI (part 1) is untouched. This bridge only runs when
``settings["lm_hub_enabled"]`` is truthy; when it runs, the legacy
webui-hub ``relay_loop`` is skipped so the spoke relays to exactly one hub.

Imported lazily by ``server.py`` from ``lifespan`` to avoid a circular import
(server defines the state this module reads, and this module is wired into
server's lifespan). ``server`` is imported lazily inside methods for the same
reason.
"""

# Postpone annotation evaluation so the forward reference "LMControlPlane" is fine.
from __future__ import annotations

# asyncio runs the telemetry loop and sleeps between frames.
import asyncio
# logging reports bridge errors and status.
import logging
# os builds fallback import paths and reads HUB_URL/SPOKE_ID style env vars.
import os
# sys.path is extended when the lm repo is not already importable.
import sys
# time stamps telemetry frames and command updates.
import time
# uuid creates unique message and command ids.
import uuid
# Type hints for the loosely-typed dict payloads.
from typing import Any, Dict, Optional

# --- Locate the shared BaseControlPlane (lm/core/src/messaging/control_plane) ---
# Preferred: it's already importable (PYTHONPATH includes the lm repo root so
# ``core`` is a package). Fallback: derive it from this file's location —
# webui-spoke lives at <ws>/cs/webui-spoke, so lm core is at <ws>/lm/core.
# First try the normal import path.
try:
    from core.src.messaging.control_plane import BaseControlPlane  # type: ignore
# Not importable: compute the lm repo location relative to this file and add it to sys.path.
except ImportError:  # pragma: no cover - dev convenience
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _LM_CORE_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "lm", "core", "src"))
    if _LM_CORE_SRC not in sys.path:
        sys.path.insert(0, _LM_CORE_SRC)
    # ``core`` package sits one level above core/src's parent's parent; ensure
    # the directory containing the ``core`` package is importable too.
    _LM_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "lm"))
    if _LM_ROOT not in sys.path:
        sys.path.insert(0, _LM_ROOT)
    # Retry the import now that the paths are in place.
    from core.src.messaging.control_plane import BaseControlPlane  # type: ignore

# One named logger for every bridge message.
logger = logging.getLogger("LMRelay")

# Default telemetry push interval (seconds). Overridable via settings.
DEFAULT_TELEMETRY_INTERVAL = 15.0


class CSBridge:
    """Module registered with the control plane so the hub can route ``CS_*``
    commands to it. Phase 1: status. Phase 4: action dispatch."""

    def __init__(self, cp: "LMControlPlane"):
        """Remember the owning control plane so the bridge can read its spoke id."""
        self._cp = cp

    async def get_status(self) -> Dict[str, Any]:
        """Latest telemetry snapshot — backs CS_GET_STATUS / SPOKE_GET_STATUS."""
        # Build the snapshot from server.py; any failure becomes an ERROR reply instead of a crash.
        try:
            import server  # lazy; same process, module name ``server``
            return await server._build_relay_telemetry_payload(self._cp.spoke_id)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("CSBridge.get_status failed")
            return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}

    async def handle_command(self, cmd_type: str, data: Dict[str, Any]) -> Any:
        """Dispatch a hub-originated ``CS_*`` command.

        Status/config commands are handled directly. Action commands
        (``CS_QUEUE_COMMAND`` / ``CS_GET_COMMANDS`` / ``CS_POLL_AGENT_INBOX`` /
        ``CS_ACK_COMMAND`` / ``CS_GET_USB_CONFIG`` / ``CS_CLEAR_COMMANDS``)
        route into the existing ``server.py`` machinery — the same code path the
        legacy webui-hub relay used — so the hub's ``CSBridgePoller`` delivery
        loop and the VM Server UI's command queue work end-to-end on the LM WS
        path.
        """
        # Status requests return the live telemetry snapshot.
        if cmd_type in ("CS_GET_STATUS", "CS_GET_TELEMETRY"):
            return await self.get_status()

        # Version request: report the installer version server.py was built with.
        if cmd_type in ("CS_GET_VERSION",):
            try:
                import server  # type: ignore
                return {"status": "SUCCESS", "version": getattr(server, "INSTALLER_VERSION", "unknown")}
            except Exception as exc:  # pragma: no cover
                return {"status": "ERROR", "message": str(exc)}

        # Hub pushed a configuration update.
        if cmd_type == "CS_CONFIG_UPDATE":
            # Hub-owned config push (central_api/central_config/notifications/
            # sim_conf_override/user_conf_override/relay_onboarding_psk + the
            # HUB_CONFIG_OWNED_KEYS). server._apply_hub_config is the same path
            # the legacy webui-hub relay used for config_update commands.
            try:
                import server  # type: ignore
                result = await server._apply_hub_config(data or {})  # type: ignore[attr-defined]
                # Pass through results that already carry a status; otherwise wrap them as SUCCESS.
                if isinstance(result, dict) and result.get("status"):
                    return result
                return {"status": "SUCCESS", "result": result}
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("CSBridge CS_CONFIG_UPDATE failed")
                return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}

        # Action commands are each delegated to a dedicated handler below.
        if cmd_type == "CS_QUEUE_COMMAND":
            return await self._handle_queue_command(data or {})
        if cmd_type == "CS_GET_COMMANDS":
            return await self._handle_get_commands()
        if cmd_type == "CS_POLL_AGENT_INBOX":
            return await self._handle_poll_inbox(data or {})
        if cmd_type == "CS_ACK_COMMAND":
            return await self._handle_ack_command(data or {})
        if cmd_type == "CS_CLEAR_COMMANDS":
            return await self._handle_clear_commands(data or {})
        if cmd_type == "CS_GET_USB_CONFIG":
            return await self._handle_get_usb_config(data or {})
        if cmd_type == "CS_GET_CENTRAL_AVAILABLE":
            return await self._handle_get_central_available()

        # Anything else is not supported by this build; reply with an ERROR the hub can display.
        logger.info("CSBridge: %s not implemented yet (Phase 4)", cmd_type)
        return {
            "status": "ERROR",
            "message": f"{cmd_type} not implemented in this build (action dispatch lands in Phase 4)",
        }

    # ── Action command handlers (Phase 4 / Wave 1) ──────────────────────────
    # These reuse server.py's existing command-queue + relay-batch machinery so
    # the LM WS path behaves identically to the legacy webui-hub relay path.

    # cmd_type values that _apply_relay_command_batch dispatches specially
    # (fleet/control operations). Everything else falls through to the generic
    # target/action enqueue (the VM action path).
    _FLEET_CMD_TYPES = frozenset({
        "gkill_switch", "repo_sync", "self_update", "refresh_webui",
        "proxmox_agent_update", "proxmox_approve_agent", "proxmox_revoke_agent",
        "proxmox_agent_command", "unlock_template", "clear_reclone_state",
        "proxmox_reclone_all", "backup", "reseed", "config_update", "config_clear",
    })

    async def _handle_queue_command(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Enqueue a VM action or run a fleet/control operation.

        ``data`` = {target, action, args, type}. Fleet operations (``type`` in
        ``_FLEET_CMD_TYPES``) are routed through ``_apply_relay_command_batch``
        so every existing spoke branch (reclone-all, agent update, gkill, …)
        is reused verbatim; the result is captured via the ack callback and
        returned. Plain VM actions (start_vm/stop_vm/reboot_vm/snapshot_vm/
        reclone_vm/delete_vm/update_agent) enqueue directly via
        ``_queue_command`` which returns the created command.
        """
        # server is imported lazily to avoid a circular import at module load time.
        import server  # type: ignore
        # Normalise the request fields; target defaults to the proxmox agent.
        target = str(data.get("target") or "proxmox").strip()
        action = str(data.get("action") or "").strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        cmd_type = data.get("type") or None

        # Nothing to do without either an action or a fleet command type.
        if not action and not cmd_type:
            return {"status": "ERROR", "message": "missing 'action'"}

        # Fleet / control operation → reuse the legacy relay batch dispatch.
        if cmd_type and cmd_type in self._FLEET_CMD_TYPES:
            # Collects the result the batch dispatcher reports through the ack callback.
            holder: Dict[str, Any] = {}

            async def _capture_ack(_cid: str, status: str, result: Any) -> None:
                """Ack callback: store the operation's final status and result for the reply."""
                holder["status"] = status
                holder["result"] = result

            # Wrap the request in the same shape the legacy relay sent.
            remote_cmd = {
                "id": str(uuid.uuid4()),
                "type": cmd_type,
                "target": target,
                "action": action,
                "args": args,
                "payload": args,  # fleet branches read payload_data
            }
            # Run it through the shared dispatcher; failures become an ERROR reply.
            try:
                await server._apply_relay_command_batch([remote_cmd], _capture_ack)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - defensive
                logger.exception("CSBridge CS_QUEUE_COMMAND (fleet %s) failed", cmd_type)
                return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
            # A result flagged success=False is reported as an error with its detail message.
            result = holder.get("result")
            if isinstance(result, dict) and result.get("success") is False:
                return {"status": "ERROR",
                        "message": str(result.get("detail") or "operation failed"),
                        "result": result}
            return {"status": "SUCCESS", "result": result}

        # Plain VM action → enqueue for the proxmox agent (or a sim client).
        # Enqueue the action; refusals (e.g. protected VMID) come back as an ERROR reply.
        try:
            cmd = await server._queue_command(target, action, args, command_type=cmd_type)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - protected VMID → 403 etc.
            detail = getattr(exc, "detail", None) or str(exc)
            logger.debug("CSBridge CS_QUEUE_COMMAND enqueue refused: %s", detail)
            return {"status": "ERROR", "message": str(detail)}
        return {"status": "SUCCESS", "queued": True, "command": cmd}

    async def _handle_get_commands(self) -> Dict[str, Any]:
        """Return the full command queue (for the Command Queue UI list)."""
        import server  # type: ignore
        # Under the shared state lock, purge expired commands and snapshot the rest.
        async with server.state_lock:  # type: ignore[attr-defined]
            server._cleanup_commands_locked()  # type: ignore[attr-defined]
            serialized = server._serialize_commands()  # type: ignore[attr-defined]
        return {"commands": serialized}

    async def _handle_poll_inbox(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return pending commands for a hostname and mark them delivered.

        The hub's ``CSBridgePoller`` calls this per connected pxmx agent; the
        returned commands are relayed to the agent as ``CS_COMMAND`` and the
        poller later acks the terminal result via ``CS_ACK_COMMAND``.
        """
        import server  # type: ignore
        # Without a hostname there is no inbox to poll.
        hostname = str(data.get("hostname") or "").strip()
        if not hostname:
            return {"commands": []}
        # Under the lock, take the host's pending commands, serialise them for the agent and
        # mark them delivered so they are not handed out twice.
        async with server.state_lock:  # type: ignore[attr-defined]
            pending, _exp, _pur = server._peek_pending_agent_commands_locked(hostname)  # type: ignore[attr-defined]
            payload = [server._serialize_command_for_agent(c) for c in pending]  # type: ignore[attr-defined]
            server._mark_commands_delivered_locked([c["id"] for c in pending])  # type: ignore[attr-defined]
            serialized = server._serialize_commands()  # type: ignore[attr-defined]
        # If anything changed, tell connected UIs so the queue view refreshes.
        if pending:
            try:
                await server.broadcast({"type": "commands_update", "commands": serialized})  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - broadcast is best-effort
                logger.debug("CSBridge: commands_update broadcast failed")
        return {"commands": payload}

    async def _handle_ack_command(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Mark a command completed/failed (terminal ack from the poller)."""
        import server  # type: ignore
        # Record the terminal status; validation errors from server.py become an ERROR reply.
        try:
            await server._ack_command_internal(data or {})  # type: ignore[attr-defined]
            return {"status": "SUCCESS"}
        except Exception as exc:  # noqa: BLE001 - 404/422 etc.
            return {"status": "ERROR", "message": str(getattr(exc, "detail", exc))}

    async def _handle_clear_commands(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Clear the command queue (the Command Queue UI 'Clear' button)."""
        import server  # type: ignore
        # Optional target filter; an empty target clears every target's queue.
        target = str(data.get("target") or "").strip()
        now = time.time()
        # Under the lock, expire every pending/delivered command that matches.
        async with server.state_lock:  # type: ignore[attr-defined]
            cleared = 0
            # Skip commands for other targets when a filter was given.
            for cmd in server.commands:  # type: ignore[attr-defined]
                if target and cmd.get("target") != target:
                    continue
                # Only commands not yet finished are expired; finished ones keep their result.
                if cmd.get("status") in {"pending", "delivered"}:
                    cmd["status"] = "expired"
                    cmd["updated_at"] = now
                    cmd["purge_after"] = now + server.COMMAND_RESULT_RETENTION_SECS  # type: ignore[attr-defined]
                    cleared += 1
            # Persist the change if anything was cleared, then snapshot the queue.
            if cleared:
                await server._async_save_commands()  # type: ignore[attr-defined]
            serialized = server._serialize_commands()  # type: ignore[attr-defined]
        # Notify connected UIs; a broadcast failure is harmless.
        if cleared:
            try:
                await server.broadcast({"type": "commands_update", "commands": serialized})  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return {"status": "SUCCESS", "cleared": cleared}

    async def _handle_get_usb_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return the USB-provisioning config blob for a pxmx agent.

        The hub's ``CSBridgePoller`` diffs this against the last pushed blob and
        pushes it to the agent on change. Best-effort: only the hub-owned USB
        knobs the spoke is authoritative for are included.
        """
        import server  # type: ignore
        # Collect the USB provisioning settings, with defaults for unset keys.
        s = server.settings  # type: ignore[attr-defined]
        cfg = {
            "usb_vidpids": s.get("usb_vidpids", ""),
            "usb_ignored_vidpids": s.get("usb_ignored_vidpids", ""),
            "usb_auto_provision": s.get("usb_auto_provision", "off"),
            "usb_missing_timeout": s.get("usb_missing_timeout", ""),
            "usb_max_slots": s.get("usb_max_slots", ""),
        }
        # Include the protected-VMID guard if the spoke publishes one.
        protected = getattr(server, "PROTECTED_VMIDS", None)  # type: ignore[attr-defined]
        if protected:
            # Bad values in the guard list are ignored rather than failing the whole reply.
            try:
                cfg["protected_vmids"] = sorted(int(v) for v in protected)
            except Exception:  # noqa: BLE001
                pass
        return {"usb_config": cfg}

    async def _handle_get_central_available(self) -> Dict[str, Any]:
        """Return the spoke's Aruba Central available-checks catalog
        (``{alerts, insights, warning}``) for the hub's Central API editor.

        Reuses the spoke's existing ``api_central_available`` so the catalog is
        identical to what the standalone spoke UI serves at ``GET /api/central/available``.
        """
        import server  # type: ignore
        # Call the spoke endpoint function directly; errors become a warning in the reply.
        try:
            fn = getattr(server, "api_central_available", None)  # type: ignore[attr-defined]
            # Older spokes may not have the endpoint at all.
            if fn is None:
                return {"alerts": [], "insights": [], "warning": "Spoke has no central-available endpoint."}
            data = await fn()
            return data or {"alerts": [], "insights": [], "warning": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("CSBridge: CS_GET_CENTRAL_AVAILABLE failed: %s", exc)
            return {"alerts": [], "insights": [], "warning": f"{type(exc).__name__}: {exc}"}


class LMControlPlane(BaseControlPlane):
    """Control plane that advertises ``module_type = "Client-Sim"`` and relays
    the combined spoke's telemetry to the LM hub."""

    def __init__(self, spoke_id: str, secret: Optional[str] = None,
                 hub_secret: Optional[str] = None, hub_url: Optional[str] = None,
                 telemetry_interval: float = DEFAULT_TELEMETRY_INTERVAL):
        # Initialise the shared websocket/signing machinery, then identify as a Client-Sim module.
        super().__init__(spoke_id=spoke_id, secret=secret, hub_secret=hub_secret, hub_url=hub_url)
        self.module_type = "Client-Sim"
        self.telemetry_interval = telemetry_interval
        # Register the bridge module so hub CS_* commands route to it.
        self.register_module("cs", CSBridge(self))

    # --- Per-connection telemetry task (hook added to BaseControlPlane) ---
    def _create_spoke_tasks(self, websocket) -> list:
        """Hook called by BaseControlPlane per connection: start the telemetry push task."""
        return [asyncio.create_task(self._telemetry_loop(websocket))]

    async def _telemetry_loop(self, websocket) -> None:
        """Push CS_TELEMETRY frames until the connection closes, honoring the hub's
        backpressure signal.

        Like the cs ``lm-spoke`` relay (``cs/lm-spoke/src/control_plane.py``), the
        base cadence ``self.telemetry_interval`` is passed through
        ``self._bp_send_interval(...)`` so an ``LM_BACKPRESSURE`` slow-down from the
        hub stretches this relay's send interval too. Each frame carries the latest
        full snapshot, so sending less often is latest-wins coalescing done on the
        spoke (the hub pushes merge work down to the spoke rather than shedding it).
        See lm/docs/backpressure-throttling.md §6."""
        # Short initial delay so the first frame lands after the handshake/approval
        # race rather than mid-handshake.
        await asyncio.sleep(2.0)
        # Keep sending until the connection drops or the task is cancelled.
        while True:
            try:
                # Only send once we have a signing secret (post-approval). Before
                # that the hub would drop unsigned non-heartbeat frames anyway.
                if not self.signer:
                    await asyncio.sleep(5.0)
                    continue
                # Build the latest full snapshot and wrap it in the LM message envelope.
                import server  # lazy
                payload = await server._build_relay_telemetry_payload(self.spoke_id)  # type: ignore[attr-defined]
                msg = {
                    "header": {
                        "message_id": str(uuid.uuid4()),
                        "timestamp": time.time(),
                        "sender_id": self.spoke_id,
                        "destination_id": "hub",
                    },
                    "payload": {"type": "CS_TELEMETRY", "data": payload},
                }
                # Sign/encode and send the frame.
                await websocket.send(self._encode_frame(msg))
                # Honor the hub's backpressure signal: _bp_send_interval stretches
                # the base cadence under LM_BACKPRESSURE so this relay slows down in
                # step with the cs lm-spoke relay. Sending less often is latest-wins
                # coalescing on the spoke (each frame is the full snapshot), so work
                # is pushed down rather than shed. See docs/backpressure-throttling.md §6.
                await asyncio.sleep(self._bp_send_interval(self.telemetry_interval))
            except asyncio.CancelledError:
                # Cancellation means shutdown: re-raise so the task actually ends.
                raise
            except Exception as exc:  # connection gone / build failure
                logger.debug("Telemetry send failed; letting main loop reconnect: %s", exc)
                return


def _truthy(val: Any) -> bool:
    """Return True for common "enabled" spellings such as 1/on/true/yes."""
    return str(val).strip().lower() in ("1", "on", "true", "yes", "enabled")


def build_lm_control_plane() -> Optional[LMControlPlane]:
    """Construct an LMControlPlane from webui-spoke settings, or return None
    when the LM relay is disabled. Reads ``lm_hub_*`` keys (with graceful
    fallback to the legacy webui-hub relay_* keys so an existing spoke can be
    pointed at LM by flipping one flag)."""
    # Settings live in server.py; if they cannot be read the bridge stays off.
    try:
        import server  # type: ignore
        s = server.settings  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        logger.warning("LMRelay: could not read server.settings; LM bridge disabled.")
        return None

    # The bridge only runs when explicitly enabled.
    if not _truthy(s.get("lm_hub_enabled", "off")):
        return None

    # A hub URL is required, from settings or the HUB_URL environment variable.
    hub_url = (s.get("lm_hub_url") or os.environ.get("HUB_URL") or "").strip()
    if not hub_url:
        logger.warning("LMRelay: lm_hub_enabled but no lm_hub_url set; bridge not started.")
        return None

    # Identity and secrets: LM-specific settings first, then legacy relay keys/env vars, then
    # defaults; empty secrets become None so the base class treats them as unset.
    spoke_id = (s.get("lm_spoke_id") or s.get("relay_spoke_id")
               or os.environ.get("SPOKE_ID") or "cs-spoke-1").strip()
    secret = (s.get("lm_spoke_secret") or os.environ.get("SPOKE_SECRET") or "").strip() or None
    hub_secret = (s.get("lm_hub_secret") or os.environ.get("HUB_SECRET") or "").strip() or None

    # Telemetry interval from settings; unparsable values fall back to the default.
    try:
        interval = float(s.get("lm_hub_poll_interval", s.get("relay_poll_interval", DEFAULT_TELEMETRY_INTERVAL)))
    except (TypeError, ValueError):
        interval = DEFAULT_TELEMETRY_INTERVAL

    # Log the effective configuration and build the control plane (interval at least 2 s).
    logger.info("LMRelay: starting Client-Sim spoke '%s' -> %s (telemetry every %.1fs)",
                spoke_id, hub_url, interval)
    return LMControlPlane(
        spoke_id=spoke_id,
        secret=secret,
        hub_secret=hub_secret,
        hub_url=hub_url,
        telemetry_interval=max(2.0, interval),
    )