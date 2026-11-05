"""Ingest command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_ingest`` looks the command up in
``_INGEST_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Standard-library logging for handler messages.
import logging
# asyncio is used to start background sim-tag syncs without blocking replies.
import asyncio

# Used to live-push newly queued commands to connected agents.
import client_api
# Type hints only.
from typing import Any, Dict, Optional

# Share the spoke's logger so these messages appear alongside the rest of the spoke.
logger = logging.getLogger("CSSpoke")


class IngestCommandsMixin:
    """Mixin handling telemetry ingestion, progress tracking, and command queueing."""

    # Map of command name -> handler method; several log-like events share one handler.
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
        # Look up which method handles this command.
        handler = self._INGEST_COMMANDS.get(cmd)
        # Not an ingest command: return None so the next domain mixin can try it.
        if handler is None:
            return None
        # Call the handler with the command name too (some handlers serve several commands).
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
        """Store a Proxmox host's telemetry frame and kick dependent updates."""
        # Read the reporting host and store its telemetry.
        hostname = (d.get("hostname") or "").strip()
        entry = self.deploy.ingest_telemetry(hostname, d)
        # The deploy helper returns nothing when the hostname is missing.
        if not entry:
            return {"status": "ERROR", "message": "missing hostname"}
        # Wake the conditional relay loop so any state change in this fresh
        # frame (VM deleting/recloning, client stop/start) relays to the hub
        # immediately instead of waiting out the idle interval.
        # Wake the hub relay loop if the control plane supports it.
        try:
            cp = getattr(self, "control_plane", None)
            if cp is not None and getattr(cp, "_relay_wake", None) is not None:
                cp._relay_wake.set()
        # Waking the relay is optional; ignore any failure.
        except Exception:  # noqa: BLE001 — relay wake is best-effort
            pass
        # Phase F: a fresh VM list is the trigger for sim-tag sync (legacy
        # drives it off client-status ingest; here the per-host VM list is
        # the source of truth). Best-effort background sweep — no-op until
        # the client registry lands (registry=None). Never blocks the ack.
        # Start a background sim-tag sync, then confirm with the host's VM count.
        asyncio.create_task(self._maybe_sync_sim_tags())
        return {"status": "SUCCESS", "hostname": hostname,
                "vm_count": entry.get("vm_count", 0)}

    async def _cmd_ingest_log(self, cmd, d):
        """Record a log/progress/watchdog/HW-reset event from a Proxmox agent."""
        # Read the reporting host and derive the event kind from the command name.
        hostname = (d.get("hostname") or "").strip()
        kind = cmd[len("CS_INGEST_"):]
        # Store the event in the per-host event buffer.
        self.deploy.ingest_event(hostname, kind.lower(), d)
        # A CS_PROGRESS frame means a long op is genuinely running on the
        # agent — refresh the command's updated_at so the
        # STALE_DELIVERED_SECS reset doesn't re-send it while it's still
        # working (delete_vm/reclone_vm can take minutes). The frame
        # carries the cs_cmd_id correlation key (same as
        # CS_COMMAND_RESULT). Best-effort: a missing/unknown id is fine.
        # Progress frames get extra handling (see explanation above).
        if cmd == "CS_INGEST_PROGRESS":
            # Correlation id of the long-running command, if supplied.
            cs_cmd_id = d.get("cs_cmd_id")
            if cs_cmd_id:
                # Mark the command as still alive so it is not re-sent.
                try:
                    await self.queue.touch_command(cs_cmd_id,
                                                  d.get("message"))
                # Unknown ids or queue errors are harmless here.
                except Exception as e:  # noqa: BLE001 — best-effort
                    logger.debug("CS_INGEST_PROGRESS touch %s failed: %s",
                                 cs_cmd_id, e)
            # Relay the per-phase progress frame UP to the hub so it can fan
            # out to the tenant's /sim/ws browsers for a realtime operations
            # feed (reclone/provision/delete phases). The hub otherwise only
            # sees the 10s CS_TELEMETRY re-emit. Fire-and-forget.
            # Forward the progress frame to the hub for the live operations feed.
            try:
                if self.control_plane is not None:
                    await self.control_plane.send_to_hub("CS_PROGRESS", d)
            # The feed is optional; log and continue on failure.
            except Exception as e:  # noqa: BLE001 — feed is best-effort
                logger.debug("CS_PROGRESS relay to hub failed: %s", e)
        # Confirm which kind of event was ingested.
        return {"status": "SUCCESS", "hostname": hostname, "ingested": kind.lower()}

    async def _cmd_ingest_command_result(self, cmd, d):
        """Record a long operation's final result and close its pending command."""
        # Terminal result of a long op. Record it for the per-host event
        # buffer AND close the deferred ack loop so the cs UI marks the
        # command completed/failed (Phase E). cs_cmd_id is the correlation
        # key the bridge deferred the ack on.
        # Read the reporting host and store the result event.
        hostname = (d.get("hostname") or "").strip()
        self.deploy.ingest_event(hostname, "command_result", d)
        # Correlation id and final status of the command.
        cs_cmd_id = d.get("cs_cmd_id")
        status = d.get("status")  # completed | failed
        # Only acknowledge when both are present.
        if cs_cmd_id and status:
            # Anything other than "completed" counts as failed.
            ack_status = "completed" if str(status).lower() == "completed" else "failed"
            # Close the command in the queue with its message and result.
            try:
                await self.queue.ack_command(cs_cmd_id, ack_status,
                                             d.get("message"), d.get("result"))
            # Ack failures are logged; the event itself was already recorded.
            except Exception as e:  # noqa: BLE001 — best-effort; the event is recorded
                logger.warning("CS_INGEST_COMMAND_RESULT ack failed for %s: %s",
                               cs_cmd_id, e)
        # Report whether an ack was attempted.
        return {"status": "SUCCESS", "hostname": hostname,
                "ingested": "command_result", "acked": bool(cs_cmd_id and status)}

    async def _cmd_store_proxmox_token(self, cmd, d):
        """Persist a Proxmox API token for a host and trigger a sim-tag sync."""
        # Phase F: persist the per-host Proxmox API token and kick sim-tag
        # sync. The token secret is stored to data/proxmox_tokens.json and
        # is NEVER logged (TokenStore.save logs only the hostname). Reply
        # carries no token — only {stored, hostname, token_set} so the hub
        # log can't leak it via request_response's result echo.
        # Read and validate the host and token.
        hostname = (d.get("hostname") or "").strip()
        token = d.get("token")
        if not hostname or not token:
            return {"status": "ERROR",
                    "message": "missing 'hostname' or 'token'"}
        # Save the token (never logged).
        self.tokens.save(hostname, token)
        # Clear the debounce so a fresh token (which may re-enable API calls
        # used elsewhere) also triggers a sim-tag re-dispatch now. Tagging
        # itself no longer needs the token — it runs agent-side via qm/pct.
        # Reset the debounce timer and start a sim-tag sync in the background.
        self._sim_tag_last_ts = 0.0
        asyncio.create_task(self._maybe_sync_sim_tags())
        # Reply without the token itself.
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
        """Queue one or many commands for Proxmox agents and push them out live."""
        # Bulk path: an ``items`` list enqueues MANY commands from ONE message
        # (e.g. a multi-VM delete/start/stop from the UI). Each item carries
        # its own target so VMs route to their own host (VMIDs collide across
        # hosts); we enqueue them all locally then live-push once per unique
        # host. This replaces the UI's old one-WS-message-per-VM burst (which
        # flooded the queue/WS) — one message, N local enqueues, no flood.
        # Bulk request: a non-empty "items" list.
        _items = d.get("items")
        if isinstance(_items, list) and _items:
            # Per-item results and error messages.
            results = []
            errors = []
            # Coalesce a multi-VM delete into ONE ``delete_vms`` per host: the
            # agent tears the whole list down in a single self-paced long-op
            # (per-VM progress + one terminal), instead of N ``delete_vm``
            # commands each racing the relay ACCEPT window on a saturated host
            # — the root of the mass-delete "only some got deleted" drop.
            # Non-delete items (or a delete missing its vmid) enqueue as-is.
            # Delete requests grouped per host, and how many VM actions were queued.
            del_by_host: Dict[str, list] = {}
            vm_count = 0
            # Process each requested item.
            for it in _items:
                # Ignore malformed items.
                if not isinstance(it, dict):
                    continue
                # Target host (defaults to "proxmox" = any host) and action name.
                itgt = str(it.get("target") or "proxmox").strip() or "proxmox"
                iact = str(it.get("action") or "").strip()
                # An item without an action is reported as an error.
                if not iact:
                    errors.append("missing 'action'")
                    continue
                # Deletes with a VM id are collected per host instead of queued one by one.
                if iact == "delete_vm":
                    # Accept either "vmid" or "vm_id".
                    iargs = it.get("args") or {}
                    _vid = iargs.get("vmid", iargs.get("vm_id"))
                    # Add to this host's delete list and move to the next item.
                    if _vid is not None:
                        del_by_host.setdefault(itgt, []).append(_vid)
                        vm_count += 1
                        continue
                    # No vmid → fall through and enqueue as-is (the agent
                    # fails it explicitly rather than silently dropping).
                # Queue the item as-is.
                try:
                    res = await self.queue.enqueue(itgt, iact,
                                                   it.get("args") or {},
                                                   command_type=it.get("type"))
                    # Record the result and count it.
                    results.append(res)
                    vm_count += 1
                # The queue refused it (e.g. protected VM): record the reason.
                except ValueError as exc:
                    errors.append(str(exc))
            # One delete_vms per host carrying every requested vmid.
            # Queue one combined delete per host.
            for _htgt, _vids in del_by_host.items():
                try:
                    # Queue it with every VM id for that host.
                    res = await self.queue.enqueue(_htgt, "delete_vms",
                                                   {"vmids": _vids})
                    results.append(res)
                # Record refusals.
                except ValueError as exc:
                    errors.append(str(exc))
            # Push pending commands once to each distinct target host.
            for _tgt in {str(it.get("target") or "proxmox").strip() or "proxmox"
                         for it in _items if isinstance(it, dict)}:
                await client_api.push_pending(self, _tgt)
            # Summarise how many commands were created and queued, plus any errors.
            return {"status": "SUCCESS",
                    "created": sum(1 for r in results if r.get("created")),
                    "commands": len(results),
                    "queued": vm_count, "errors": errors}
        # Single command: read target (default "proxmox") and action.
        target = str(d.get("target") or "proxmox").strip() or "proxmox"
        action = str(d.get("action") or "").strip()
        # An action is required.
        if not action:
            return {"status": "ERROR", "message": "missing 'action'"}
        # Fleet-broadcast actions must reach EVERY connected pxmx agent, not
        # just the first to poll. A single target="proxmox" command matches
        # any host but poll_agent_inbox marks it delivered on the first poll,
        # so only one server would act. Enqueue one copy per connected agent
        # hostname (target=hostname) so all servers run it in parallel.
        # Actions that every Proxmox host must run (see explanation above).
        _FLEET = {"proxmox_reclone_all", "proxmox_reclone_stop",
                  "proxmox_reclone_clear"}
        # By default queue for the single requested target.
        targets = [target]
        # For fleet actions, target every connected agent's hostname instead.
        if target == "proxmox" and action in _FLEET and self.control_plane:
            hns = [info.get("hostname") for info in
                   (self.control_plane.connected_agents or {}).values()
                   if info.get("hostname")]
            # Use the distinct sorted hostnames if any are connected.
            if hns:
                targets = sorted(set(hns))
        # Queue a copy per target; the queue may refuse with ValueError.
        try:
            results = []
            # Enqueue for each target host.
            for tgt in targets:
                res = await self.queue.enqueue(tgt, action,
                                               d.get("args") or {},
                                               command_type=d.get("type"))
                # Record the result.
                results.append(res)
                # Live-deliver to that host's connected WS agent (best-effort).
                await client_api.push_pending(self, tgt)
        # Refused by a safeguard.
        except ValueError as exc:
            # Safeguard refusal (protected vmid / below sim floor).
            return {"status": "ERROR", "message": str(exc)}
        # Use the first result for the reply (or a blank placeholder if none).
        first = results[0] if results else {"command": None, "expired": 0, "purged": 0}
        # Report the command, how many were created/queued, and housekeeping counts.
        return {"status": "SUCCESS", "command": first["command"],
                "created": sum(1 for r in results if r.get("created")),
                "queued_targets": len(results),
                "expired": first["expired"], "purged": first["purged"]}

    async def _cmd_poll_agent_inbox(self, cmd, d):
        """Return the pending commands for one Proxmox agent."""
        # Read and validate the agent's hostname.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Fetch (and mark delivered) the agent's pending commands.
        res = await self.queue.poll_agent_inbox(hostname)
        return {"status": "SUCCESS", **res}

    async def _cmd_ack_command(self, cmd, d):
        """Record the final status of a delivered command."""
        # Pass the id, status, message and result to the queue.
        res = await self.queue.ack_command(d.get("id"), d.get("status"),
                                           d.get("message"), d.get("result"))
        # Unknown id or invalid status.
        if not res.get("ok"):
            return {"status": "ERROR", "message": res.get("message", "ack failed")}
        # Echo the queue's response.
        return {"status": "SUCCESS", **res}

    async def _cmd_touch_command(self, cmd, d):
        """Mark a delivered command as still in progress so it is not re-sent."""
        # Hub CSBridgePoller: a long op returned ACCEPTED. Refresh the
        # delivered command's updated_at + last_contact (in-memory) so the
        # STALE_DELIVERED reset + the delete-verify sweep don't re-send a
        # still-running op. See command_queue.touch_command. Surface the
        # COMMAND's status as queue_status so this handler's own SUCCESS
        # isn't clobbered by touch_command's status field.
        # Refresh the command's timestamps.
        res = await self.queue.touch_command(d.get("id"), d.get("message"))
        # Report the command's own status as queue_status.
        return {"status": "SUCCESS", "id": res.get("id"),
                "queue_status": res.get("status"),
                "touched": res.get("touched")}

    async def _cmd_requeue_command(self, cmd, d):
        """Put a command whose relay timed out back in the queue, up to a retry limit."""
        # Hub CSBridgePoller: a command's relay to the agent TIMED OUT (the
        # agent was too busy to ACCEPT within CS_RELAY_TIMEOUT_S). Re-queue
        # it for the next poll tick instead of marking it dead — bounded by
        # max_retries (default 5). The queue flips it to failed once
        # exhausted. See command_queue.requeue_command.
        # Read the retry limit (default 5), tolerating bad values.
        try:
            max_retries = int(d.get("max_retries") or 5)
        # Non-numeric value: use the default.
        except (TypeError, ValueError):
            max_retries = 5
        # Ask the queue to requeue (or fail) the command.
        res = await self.queue.requeue_command(
            d.get("id"), max_retries, d.get("message"))
        # Unknown id or other refusal.
        if not res.get("ok"):
            return {"status": "ERROR", "message": res.get("message", "requeue failed")}
        # ``res`` carries its own ``status`` (pending|failed) describing the
        # COMMAND's new state; surface that as ``queue_status`` so this
        # handler's own ``status: SUCCESS`` (the request succeeded) isn't
        # clobbered by ``**res``. The bridge reads ``requeued``/``attempts``.
        # Report the command's new state and retry counters.
        return {"status": "SUCCESS", "id": res.get("id"),
                "queue_status": res.get("status"),
                "requeued": res.get("requeued"), "attempts": res.get("attempts"),
                "max_retries": res.get("max_retries")}

    async def _cmd_get_usb_config(self, cmd, d):
        """Return the USB provisioning config, optionally for one host."""
        # Optional host name ("" becomes None = global config).
        hostname = str(d.get("hostname") or "").strip() or None
        # Fetch the config and return it.
        cfg = await self.queue.get_usb_config(hostname)
        return {"status": "SUCCESS", "usb_config": cfg}

    async def _cmd_get_commands(self, cmd, d):
        """Return every command in the queue."""
        # List all commands in the reply.
        return {"status": "SUCCESS",
                "commands": await self.queue.list_commands()}

    async def _cmd_clear_commands(self, cmd, d):
        """Cancel all non-terminal commands, optionally for one target."""
        # Cancel all non-terminal commands (optionally scoped to a target).
        # Sits BEFORE the NOT_IMPLEMENTED matcher below (whose set includes
        # "CLEAR") so the hub's Clear-queue + pre-teardown-expire routes work.
        # Optional target ("" becomes None = all targets).
        target = str(d.get("target") or "").strip() or None
        # Cancel them and echo the result.
        res = await self.queue.clear_commands(target)
        return {"status": "SUCCESS", **res}

    async def _cmd_delete_command(self, cmd, d):
        """Delete a single command from the queue."""
        # Per-row delete (any status). "DELETE" isn't in the matcher set, so
        # without this handler it would fall through to "Unknown command".
        # Delete by id.
        res = await self.queue.delete_command(d.get("id"))
        # Unknown id.
        if not res.get("ok"):
            return {"status": "ERROR",
                    "message": res.get("message", "command not found")}
        # Echo the result.
        return {"status": "SUCCESS", **res}

    async def _cmd_update_settings(self, cmd, d):
        """Apply a settings patch to the spoke's persistent settings."""
        # cs UI edits a USB-provision / watchdog knob; persisted to data/cs_settings.json.
        # Accept the patch under "settings" or "patch".
        patch = d.get("settings") or d.get("patch") or {}
        # It must be a JSON object.
        if not isinstance(patch, dict):
            return {"status": "ERROR", "message": "'settings' must be an object"}
        # Save it and return the full updated settings.
        return {"status": "SUCCESS",
                "settings": self.settings.update(patch)}
