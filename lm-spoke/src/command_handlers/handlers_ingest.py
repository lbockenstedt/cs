"""Ingest command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_ingest`` looks the command up in
``_INGEST_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
import asyncio

import client_api
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


class IngestCommandsMixin:
    """Mixin handling telemetry ingestion, progress tracking, and command queueing."""

    _INGEST_COMMANDS = {
        "CS_INGEST_TELEMETRY": "_cmd_ingest_telemetry",
        "CS_INGEST_LOG": "_cmd_ingest_log",
        "CS_INGEST_PROGRESS": "_cmd_ingest_log",
        "CS_INGEST_WATCHDOG_EVENT": "_cmd_ingest_log",
        "CS_INGEST_HW_RESET": "_cmd_ingest_log",
        "CS_INGEST_COMMAND_RESULT": "_cmd_ingest_command_result",
        "CS_STORE_PROXMOX_TOKEN": "_cmd_store_proxmox_token",
        "CS_QUEUE_COMMAND": "_cmd_queue_command",
        "CS_POLL_AGENT_INBOX": "_cmd_poll_agent_inbox",
        "CS_ACK_COMMAND": "_cmd_ack_command",
        "CS_TOUCH_COMMAND": "_cmd_touch_command",
        "CS_REQUEUE_COMMAND": "_cmd_requeue_command",
        "CS_GET_USB_CONFIG": "_cmd_get_usb_config",
        "CS_GET_COMMANDS": "_cmd_get_commands",
        "CS_CLEAR_COMMANDS": "_cmd_clear_commands",
        "CS_DELETE_COMMAND": "_cmd_delete_command",
        "CS_UPDATE_SETTINGS": "_cmd_update_settings",
    }

    async def _dispatch_ingest(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the handler for ``cmd`` from ``_INGEST_COMMANDS``; None if it is not one of ours."""
        handler = self._INGEST_COMMANDS.get(cmd)
        if handler is None:
            return None
        return await getattr(self, handler)(cmd, d)

    # ── Client-Simulation ingest (unified pxmx agent → hub → here) ───────
    # The hub's AGENT_RELAY_UP CS_* dispatcher forwards each CS_* agent event
    # here as a CS_INGEST_* (or CS_STORE_PROXMOX_TOKEN) command carrying the
    # agent's hostname + the event data. D1 wires telemetry end-to-end; the
    # event/log/progress/hw/reset handlers are recorded (best-effort) and
    # fully wired in Phase E; CS_INGEST_COMMAND_RESULT also closes the
    # deferred long-op ack loop; CS_STORE_PROXMOX_TOKEN persists the token
    # and kicks sim-tag sync (Phase F).
    async def _cmd_ingest_telemetry(self, cmd, d):
        hostname = (d.get("hostname") or "").strip()
        entry = self.deploy.ingest_telemetry(hostname, d)
        if not entry:
            return {"status": "ERROR", "message": "missing hostname"}
        # Wake the conditional relay loop so any state change in this fresh
        # frame (VM deleting/recloning, client stop/start) relays to the hub
        # immediately instead of waiting out the idle interval.
        try:
            cp = getattr(self, "control_plane", None)
            if cp is not None and getattr(cp, "_relay_wake", None) is not None:
                cp._relay_wake.set()
        except Exception:  # noqa: BLE001 — relay wake is best-effort
            pass
        # Phase F: a fresh VM list is the trigger for sim-tag sync (legacy
        # drives it off client-status ingest; here the per-host VM list is
        # the source of truth). Best-effort background sweep — no-op until
        # the client registry lands (registry=None). Never blocks the ack.
        asyncio.create_task(self._maybe_sync_sim_tags())
        return {"status": "SUCCESS", "hostname": hostname,
                "vm_count": entry.get("vm_count", 0)}

    async def _cmd_ingest_log(self, cmd, d):
        hostname = (d.get("hostname") or "").strip()
        kind = cmd[len("CS_INGEST_"):]
        self.deploy.ingest_event(hostname, kind.lower(), d)
        # A CS_PROGRESS frame means a long op is genuinely running on the
        # agent — refresh the command's updated_at so the
        # STALE_DELIVERED_SECS reset doesn't re-send it while it's still
        # working (delete_vm/reclone_vm can take minutes). The frame
        # carries the cs_cmd_id correlation key (same as
        # CS_COMMAND_RESULT). Best-effort: a missing/unknown id is fine.
        if cmd == "CS_INGEST_PROGRESS":
            cs_cmd_id = d.get("cs_cmd_id")
            if cs_cmd_id:
                try:
                    await self.queue.touch_command(cs_cmd_id,
                                                  d.get("message"))
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.debug("CS_INGEST_PROGRESS touch %s failed: %s",
                                 cs_cmd_id, e)
            # Relay the per-phase progress frame UP to the hub so it can fan
            # out to the tenant's /sim/ws browsers for a realtime operations
            # feed (reclone/provision/delete phases). The hub otherwise only
            # sees the 10s CS_TELEMETRY re-emit. Fire-and-forget.
            try:
                if self.control_plane is not None:
                    await self.control_plane.send_to_hub("CS_PROGRESS", d)
            except Exception as e:  # noqa: BLE001 — feed is best-effort
                logger.debug("CS_PROGRESS relay to hub failed: %s", e)
        return {"status": "SUCCESS", "hostname": hostname, "ingested": kind.lower()}

    async def _cmd_ingest_command_result(self, cmd, d):
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

    async def _cmd_store_proxmox_token(self, cmd, d):
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
        # Clear the debounce so a fresh token (which may re-enable API calls
        # used elsewhere) also triggers a sim-tag re-dispatch now. Tagging
        # itself no longer needs the token — it runs agent-side via qm/pct.
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
    async def _cmd_queue_command(self, cmd, d):
        # Bulk path: an ``items`` list enqueues MANY commands from ONE message
        # (e.g. a multi-VM delete/start/stop from the UI). Each item carries
        # its own target so VMs route to their own host (VMIDs collide across
        # hosts); we enqueue them all locally then live-push once per unique
        # host. This replaces the UI's old one-WS-message-per-VM burst (which
        # flooded the queue/WS) — one message, N local enqueues, no flood.
        _items = d.get("items")
        if isinstance(_items, list) and _items:
            results = []
            errors = []
            # Coalesce a multi-VM delete into ONE ``delete_vms`` per host: the
            # agent tears the whole list down in a single self-paced long-op
            # (per-VM progress + one terminal), instead of N ``delete_vm``
            # commands each racing the relay ACCEPT window on a saturated host
            # — the root of the mass-delete "only some got deleted" drop.
            # Non-delete items (or a delete missing its vmid) enqueue as-is.
            del_by_host: Dict[str, list] = {}
            vm_count = 0
            for it in _items:
                if not isinstance(it, dict):
                    continue
                itgt = str(it.get("target") or "proxmox").strip() or "proxmox"
                iact = str(it.get("action") or "").strip()
                if not iact:
                    errors.append("missing 'action'")
                    continue
                if iact == "delete_vm":
                    iargs = it.get("args") or {}
                    _vid = iargs.get("vmid", iargs.get("vm_id"))
                    if _vid is not None:
                        del_by_host.setdefault(itgt, []).append(_vid)
                        vm_count += 1
                        continue
                    # No vmid → fall through and enqueue as-is (the agent
                    # fails it explicitly rather than silently dropping).
                try:
                    res = await self.queue.enqueue(itgt, iact,
                                                   it.get("args") or {},
                                                   command_type=it.get("type"))
                    results.append(res)
                    vm_count += 1
                except ValueError as exc:
                    errors.append(str(exc))
            # One delete_vms per host carrying every requested vmid.
            for _htgt, _vids in del_by_host.items():
                try:
                    res = await self.queue.enqueue(_htgt, "delete_vms",
                                                   {"vmids": _vids})
                    results.append(res)
                except ValueError as exc:
                    errors.append(str(exc))
            for _tgt in {str(it.get("target") or "proxmox").strip() or "proxmox"
                         for it in _items if isinstance(it, dict)}:
                await client_api.push_pending(self, _tgt)
            return {"status": "SUCCESS",
                    "created": sum(1 for r in results if r.get("created")),
                    "commands": len(results),
                    "queued": vm_count, "errors": errors}
        target = str(d.get("target") or "proxmox").strip() or "proxmox"
        action = str(d.get("action") or "").strip()
        if not action:
            return {"status": "ERROR", "message": "missing 'action'"}
        # Fleet-broadcast actions must reach EVERY connected pxmx agent, not
        # just the first to poll. A single target="proxmox" command matches
        # any host but poll_agent_inbox marks it delivered on the first poll,
        # so only one server would act. Enqueue one copy per connected agent
        # hostname (target=hostname) so all servers run it in parallel.
        _FLEET = {"proxmox_reclone_all", "proxmox_reclone_stop",
                  "proxmox_reclone_clear"}
        targets = [target]
        if target == "proxmox" and action in _FLEET and self.control_plane:
            hns = [info.get("hostname") for info in
                   (self.control_plane.connected_agents or {}).values()
                   if info.get("hostname")]
            if hns:
                targets = sorted(set(hns))
        try:
            results = []
            for tgt in targets:
                res = await self.queue.enqueue(tgt, action,
                                               d.get("args") or {},
                                               command_type=d.get("type"))
                results.append(res)
                # Live-deliver to that host's connected WS agent (best-effort).
                await client_api.push_pending(self, tgt)
        except ValueError as exc:
            # Safeguard refusal (protected vmid / below sim floor).
            return {"status": "ERROR", "message": str(exc)}
        first = results[0] if results else {"command": None, "expired": 0, "purged": 0}
        return {"status": "SUCCESS", "command": first["command"],
                "created": sum(1 for r in results if r.get("created")),
                "queued_targets": len(results),
                "expired": first["expired"], "purged": first["purged"]}

    async def _cmd_poll_agent_inbox(self, cmd, d):
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        res = await self.queue.poll_agent_inbox(hostname)
        return {"status": "SUCCESS", **res}

    async def _cmd_ack_command(self, cmd, d):
        res = await self.queue.ack_command(d.get("id"), d.get("status"),
                                           d.get("message"), d.get("result"))
        if not res.get("ok"):
            return {"status": "ERROR", "message": res.get("message", "ack failed")}
        return {"status": "SUCCESS", **res}

    async def _cmd_touch_command(self, cmd, d):
        # Hub CSBridgePoller: a long op returned ACCEPTED. Refresh the
        # delivered command's updated_at + last_contact (in-memory) so the
        # STALE_DELIVERED reset + the delete-verify sweep don't re-send a
        # still-running op. See command_queue.touch_command. Surface the
        # COMMAND's status as queue_status so this handler's own SUCCESS
        # isn't clobbered by touch_command's status field.
        res = await self.queue.touch_command(d.get("id"), d.get("message"))
        return {"status": "SUCCESS", "id": res.get("id"),
                "queue_status": res.get("status"),
                "touched": res.get("touched")}

    async def _cmd_requeue_command(self, cmd, d):
        # Hub CSBridgePoller: a command's relay to the agent TIMED OUT (the
        # agent was too busy to ACCEPT within CS_RELAY_TIMEOUT_S). Re-queue
        # it for the next poll tick instead of marking it dead — bounded by
        # max_retries (default 5). The queue flips it to failed once
        # exhausted. See command_queue.requeue_command.
        try:
            max_retries = int(d.get("max_retries") or 5)
        except (TypeError, ValueError):
            max_retries = 5
        res = await self.queue.requeue_command(
            d.get("id"), max_retries, d.get("message"))
        if not res.get("ok"):
            return {"status": "ERROR", "message": res.get("message", "requeue failed")}
        # ``res`` carries its own ``status`` (pending|failed) describing the
        # COMMAND's new state; surface that as ``queue_status`` so this
        # handler's own ``status: SUCCESS`` (the request succeeded) isn't
        # clobbered by ``**res``. The bridge reads ``requeued``/``attempts``.
        return {"status": "SUCCESS", "id": res.get("id"),
                "queue_status": res.get("status"),
                "requeued": res.get("requeued"), "attempts": res.get("attempts"),
                "max_retries": res.get("max_retries")}

    async def _cmd_get_usb_config(self, cmd, d):
        hostname = str(d.get("hostname") or "").strip() or None
        cfg = await self.queue.get_usb_config(hostname)
        return {"status": "SUCCESS", "usb_config": cfg}

    async def _cmd_get_commands(self, cmd, d):
        return {"status": "SUCCESS",
                "commands": await self.queue.list_commands()}

    async def _cmd_clear_commands(self, cmd, d):
        # Cancel all non-terminal commands (optionally scoped to a target).
        # Sits BEFORE the NOT_IMPLEMENTED matcher below (whose set includes
        # "CLEAR") so the hub's Clear-queue + pre-teardown-expire routes work.
        target = str(d.get("target") or "").strip() or None
        res = await self.queue.clear_commands(target)
        return {"status": "SUCCESS", **res}

    async def _cmd_delete_command(self, cmd, d):
        # Per-row delete (any status). "DELETE" isn't in the matcher set, so
        # without this handler it would fall through to "Unknown command".
        res = await self.queue.delete_command(d.get("id"))
        if not res.get("ok"):
            return {"status": "ERROR",
                    "message": res.get("message", "command not found")}
        return {"status": "SUCCESS", **res}

    async def _cmd_update_settings(self, cmd, d):
        # cs UI edits a USB-provision / watchdog knob; persisted to data/cs_settings.json.
        patch = d.get("settings") or d.get("patch") or {}
        if not isinstance(patch, dict):
            return {"status": "ERROR", "message": "'settings' must be an object"}
        return {"status": "SUCCESS",
                "settings": self.settings.update(patch)}
