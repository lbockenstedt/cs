"""Ingest command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_ingest`` scans only its own command group and
returns the result dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
import asyncio

import client_api
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


class IngestCommandsMixin:
    async def _dispatch_ingest(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:

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
                for it in _items:
                    if not isinstance(it, dict):
                        continue
                    itgt = str(it.get("target") or "proxmox").strip() or "proxmox"
                    iact = str(it.get("action") or "").strip()
                    if not iact:
                        errors.append("missing 'action'")
                        continue
                    try:
                        res = await self.queue.enqueue(itgt, iact,
                                                       it.get("args") or {},
                                                       command_type=it.get("type"))
                        results.append(res)
                    except ValueError as exc:
                        errors.append(str(exc))
                for _tgt in {str(it.get("target") or "proxmox").strip() or "proxmox"
                             for it in _items if isinstance(it, dict)}:
                    await client_api.push_pending(self, _tgt)
                return {"status": "SUCCESS",
                        "created": sum(1 for r in results if r.get("created")),
                        "queued": len(results), "errors": errors}
            target = str(d.get("target") or "proxmox").strip() or "proxmox"
            action = str(d.get("action") or "").strip()
            if not action:
                return {"status": "ERROR", "message": "missing 'action'"}
            # Fleet-broadcast actions must reach EVERY connected pxmx agent, not
            # just the first to poll. A single target="proxmox" command matches
            # any host but poll_agent_inbox marks it delivered on the first poll,
            # so only one server would act. Enqueue one copy per connected agent
            # hostname (target=hostname) so all servers run it in parallel.
            _FLEET = {"proxmox_reclone_all", "proxmox_reclone_stop"}
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

        if cmd == "CS_TOUCH_COMMAND":
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

        if cmd == "CS_REQUEUE_COMMAND":
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
        return None
