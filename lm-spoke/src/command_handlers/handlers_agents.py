"""Agents command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_agents`` looks the command up in
``_AGENT_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Standard-library logging for relay messages.
import logging
# asyncio runs per-agent requests concurrently (retag fan-out).
import asyncio
# Type hints only.
from typing import Any, Dict, Optional

# Share the spoke's logger so these messages appear alongside the rest of the spoke.
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
    # If either side is not a dict, the incoming value simply replaces the old one.
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        return incoming
    # Start from a copy of the base so the caller's dict is not modified.
    out = dict(base)
    # Merge each incoming key.
    for k, v in incoming.items():
        # Both sides are dicts: merge them recursively.
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge_cfg(out[k], v)
        # Otherwise the incoming value wins (lists are replaced whole).
        else:
            out[k] = v
    # Return the merged copy.
    return out


class AgentCommandsMixin:
    """Mixin handling agent relay, host configuration, VNC, and shell commands."""

    # Map of command name -> handler method; some handlers serve two commands.
    _AGENT_COMMANDS = {
        "GET_AGENTS": "_cmd_get_agents",
        "SET_AGENT_CONFIG": "_cmd_set_agent_config",
        "SPOKE_RELAY": "_cmd_spoke_relay",
        "INSTALL_CERT": "_cmd_install_cert",
        "PXMX_RETAG_TENANT": "_cmd_pxmx_retag_tenant",
        "VNC_START": "_cmd_vnc_start",
        "VNC_FRAME_DOWN": "_cmd_vnc_frame_down",
        "VNC_DISCONNECT": "_cmd_vnc_disconnect",
        "SHELL_START": "_cmd_shell_start",
        "SHELL_IN": "_cmd_shell_in",
        "SHELL_RESIZE": "_cmd_shell_in",
        "SHELL_DISCONNECT": "_cmd_shell_disconnect",
        "PXMX_DRIVE_HEALTH": "_cmd_pxmx_drive_health",
        "PXMX_INSTALL_SSACLI": "_cmd_pxmx_drive_health",
    }

    async def _dispatch_agents(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the handler for ``cmd`` from ``_AGENT_COMMANDS``; None if it is not one of ours."""
        # Look up which method handles this command.
        handler = self._AGENT_COMMANDS.get(cmd)
        # Not an agent command: return None so the next domain mixin can try it.
        if handler is None:
            return None
        # Call the handler with the command name too (some handlers serve several commands).
        return await getattr(self, handler)(cmd, d)

    # ── agent hosting (cs-dialed pxmx agents, split topology) ───────────
    # These mirror ProxmoxSpoke so the LM hub can list/approve/relay-to a
    # pxmx agent that dials THIS cs spoke (wss://<cs>:443/ws/agent) instead
    # of the pxmx spoke. The cs WebUI's cs-agents panel + the hub's
    # CSBridgePoller drive them. Only active when CSControlPlane has its
    # agent listener enabled (LM_CS_AGENT_LISTENER=1); otherwise
    # control_plane.connected_agents is empty and these return empty/error.
    async def _cmd_get_agents(self, cmd, d):
        """Return the list of Proxmox agents connected to this spoke."""
        # Delegate to the spoke's agent listing.
        return self._get_agents()

    async def _cmd_set_agent_config(self, cmd, d):
        """Store and push a new configuration to one connected agent."""
        # Read the target agent and its new config.
        agent_id = d.get("agent_id")
        cfg = d.get("config", {})
        # The agent id is required.
        if not agent_id:
            return {"status": "ERROR", "message": "Missing agent_id"}
        # Only possible when the agent listener (control plane) exists.
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
            # Merge into the per-agent config cache, if the control plane keeps one.
            cache = getattr(self.control_plane, "_agent_config_cache", None)
            if cache is not None:
                cfg = _deep_merge_cfg(cache.get(agent_id) or {}, cfg)
                cache[agent_id] = cfg
            # Send the full (merged) config to the agent and return its reply.
            return await self.control_plane.send_to_agent(
                "UPDATE_CONFIG", cfg, agent_id=agent_id)
        # No control plane: the agent cannot be reached.
        return {"status": "ERROR", "message": "Agent not connected"}

    async def _cmd_spoke_relay(self, cmd, d):
        """Approve, revoke, or forward a command to a specific connected agent."""
        # Which agent and which command the hub wants.
        target = d.get("target_agent_id")
        command = d.get("command")
        # Log every relay request for troubleshooting.
        logger.info(
            "[cs-spoke] SPOKE_RELAY received: command=%r target_agent_id=%r "
            "control_plane=%s", command, target, bool(self.control_plane))
        # Hub approved a pending agent: let it in.
        if command == "APPROVAL_SUCCESS" and target and self.control_plane:
            await self.control_plane.approve_pending_agent(target)
            return {"status": "SUCCESS", "message": f"Agent {target} approved"}
        # Hub revoked an agent: disconnect it.
        if command == "REVOKE_AGENT" and target and self.control_plane:
            await self.control_plane.revoke_agent(target)
            return {"status": "SUCCESS", "message": f"Agent {target} disconnected"}
        # Generic forward: relay an arbitrary command (e.g. CS_COMMAND from
        # the hub's CSBridgePoller) to a specific cs-dialed agent. The
        # agent's AGENT_RESPONSE data is returned to the hub.
        # Any other command is forwarded as-is to the target agent.
        if command and target and self.control_plane:
            # The command's payload.
            inner = d.get("data") or {}
            # Long ops (delete/reclone/snapshot/clone/provision) ack ACCEPTED
            # immediately and stream the real result via CS_COMMAND_RESULT,
            # but that ACCEPTED can slip past the default 15s window when the
            # agent is busy (e.g. auto-prov cloning clients during a bulk
            # delete) — producing a false "Agent response timeout" and a
            # FAILED queue entry even though the op runs. Give long ops a
            # wider sync window; fast commands keep the 15s fail-fast. Match
            # on the relay command OR the inner action (path-agnostic).
            # Long-running actions that need a wider reply window (see explanation above).
            _LONG = {"delete_vm", "reclone_vm", "snapshot_vm", "clone_lxc",
                     "provision_unassigned", "reclone_all", "proxmox_reclone_all"}
            # Decide which action this is: the relay command itself, or the inner "action" field.
            _act = command if command in _LONG else (
                inner.get("action") if isinstance(inner, dict) else None)
            # Timeouts are hub-configurable (Setup → General, pushed via
            # CS_CONFIG_UPDATE) so WAN / busy-agent sites can tune them.
            # Pick the long or fast timeout (60 s / 15 s defaults, hub-configurable).
            _to = (getattr(self, "_relay_timeout_long", 60.0) if _act in _LONG
                   else getattr(self, "_relay_timeout_fast", 15.0))
            # Forward to the agent and return its reply.
            return await self.control_plane.send_to_agent(command, inner, agent_id=target, timeout=_to)
        # Missing command/target or no control plane.
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
    async def _cmd_install_cert(self, cmd, d):
        """Deploy a certificate to managed agents and, unless it is hypervisor-only, this spoke's
        UI.
        """
        # Which kind of module the cert is for.
        mt = (d.get("module_type") or "").lower()
        # First relay the cert to every managed pxmx agent.
        relay = await self._install_cert_relay(d)
        # Hypervisor certs are only for the Proxmox nodes: stop here.
        if mt == "hypervisor":
            return relay
        # Default local-UI result if there is no control plane.
        webui = {"status": "ERROR", "message": "no control plane"}
        cp = self.control_plane
        # Install the cert on this spoke's own dashboard when supported.
        if cp is not None and hasattr(cp, "_apply_local_cert"):
            try:
                webui = await cp._apply_local_cert(
                    d.get("fullchain", ""), d.get("privkey", ""))
            # Report a local install failure without hiding the relay result.
            except Exception as exc:  # noqa: BLE001 - webui apply must not mask the relay result
                webui = {"status": "ERROR",
                         "message": f"{type(exc).__name__}: {exc}"}
        # Attach the local-UI message to the relay result.
        relay["webui"] = webui.get("message", "")
        # The node relay owns the target status: a cert deployed to the
        # nodes is SUCCESS even when the local-dashboard HTTPS rebind fails
        # (that failure is surfaced in the message, not dropped — but it
        # must not flip a successful node deploy red, which was the
        # operator's complaint: a deployed cert showing failed). A relay
        # ERROR stays ERROR regardless of the webui outcome.
        # Append the local-UI status to the message without changing the overall status.
        relay["message"] = (relay.get("message", "")
                            + f"; webui {webui.get('status', 'ERROR').lower()}"
                            + (f": {webui.get('message', '')}" if webui.get("message") else ""))
        # Return the relay result.
        return relay

    async def _cmd_pxmx_retag_tenant(self, cmd, d):
        """Ask every connected agent to re-tag its VMs from an old tenant tag to a new one."""
        # Cross-tenant migration, split topology: THIS cs spoke hosts the
        # pxmx agents, so re-tag (old_tag -> new_tag) by broadcasting to each
        # connected agent (mirrors _install_cert_relay's fan-out).
        # No control plane: no agents can be reached.
        if not self.control_plane:
            return {"status": "ERROR", "message": "not connected to a control plane"}
        # Snapshot the connected agents.
        connected = dict(self.control_plane.connected_agents or {})
        # None connected: report that the retag is deferred until they reconnect.
        if not connected:
            return {"status": "DEFERRED", "count": 0,
                    "message": "no managed pxmx agents connected — deferred, retries on reconnect"}

        async def _retag_one(aid: str) -> Dict[str, Any]:
            """Send the retag request to one agent and normalise its reply."""
            # Ask the agent to retag (allow up to 2 minutes).
            try:
                r = await self.control_plane.send_to_agent(
                    "PXMX_RETAG_TENANT", d, agent_id=aid, timeout=120.0)
            # One failing node must not abort the others: report it as an error row.
            except Exception as exc:  # noqa: BLE001 - one node must not abort the rest
                return {"agent_id": aid, "status": "ERROR", "count": 0, "message": str(exc)}
            # The useful data may be wrapped in payload.data; non-dict replies give {}.
            rret = (r.get("payload", {}).get("data", r) if isinstance(r, dict) else {})
            # Guard against non-dict data.
            if not isinstance(rret, dict):
                rret = {}
            # Return a uniform per-node result row.
            return {"agent_id": aid, "status": rret.get("status", "ERROR"),
                    "count": int(rret.get("count", 0) or 0), "message": rret.get("message", "")}

        # Retag all agents concurrently.
        nodes = list(await asyncio.gather(*[_retag_one(a) for a in connected]))
        # Total VMs retagged, and whether any node failed.
        total = sum(n["count"] for n in nodes)
        any_err = any(n["status"] not in ("SUCCESS", None) for n in nodes)
        # PARTIAL when any node failed, otherwise SUCCESS.
        return {"status": "PARTIAL" if any_err else "SUCCESS",
                "count": total, "retagged": total, "nodes": nodes,
                "message": f"re-tagged {total} VM(s) across {len(nodes)} node(s)"}

    # ── VNC console relay (agent-terminates-WSS) ────────────────────────
    # PORT of ProxmoxSpoke.VNC_START/FRAME_DOWN/DISCONNECT. In the all-cs-
    # hosted topology the pxmx agents dial THIS cs spoke, so the cs spoke —
    # not a pxmx spoke — must relay VNC to them. Without these handlers a
    # request_response(cs_spoke, "VNC_START") hit no branch and fell through
    # to a generic error, which the hub surfaced as "agent refused
    # VNC_START". VNC_START is sync (returns the Proxmox ticket = the RFB
    # password noVNC must present); frames/disconnect are fire-and-forget.
    async def _cmd_vnc_start(self, cmd, d):
        """Start a VNC console session on the agent that owns the requested VM."""
        # Lazily create the session_id -> agent_id map.
        if not hasattr(self, "vnc_sessions"):
            self.vnc_sessions = {}
        # Session id and an explicitly requested agent, if any.
        session_id = d.get("session_id") or ""
        agent_id = d.get("agent_id") or d.get("target_agent_id")
        # Reuse the agent already bound to this session.
        if not agent_id and session_id:
            agent_id = self.vnc_sessions.get(session_id)
        # Not known yet and a control plane exists: try to resolve the agent from the node.
        if not agent_id and self.control_plane:
            # Otherwise resolve by Proxmox node name or by the cluster prefix of unique_id
            # ("cluster/...").
            node = str(d.get("node") or "")
            uid = str(d.get("unique_id") or "")
            cluster = uid.split("/")[0] if "/" in uid else ""
            # Find a connected agent whose hostname or cluster name matches.
            for aid, info in self.control_plane.connected_agents.items():
                hosts = {info.get("hostname"), info.get("cluster_name")}
                # Use the first match.
                if (node and node in hosts) or (cluster and cluster in hosts):
                    agent_id = aid
                    break
            # With exactly one agent connected, it must be the one.
            if not agent_id and len(self.control_plane.connected_agents) == 1:
                agent_id = next(iter(self.control_plane.connected_agents))
        # No agent could be resolved.
        if not agent_id:
            return {"status": "ERROR", "message": "No agent resolved for VNC_START"}
        # Remember which agent serves this session for later frames.
        if session_id:
            self.vnc_sessions[session_id] = agent_id
        # Ask the agent to start VNC (45 s limit) and return its reply (includes the ticket).
        return await self.control_plane.send_to_agent(
            "VNC_START", d, agent_id=agent_id, timeout=45.0)

    async def _cmd_vnc_frame_down(self, cmd, d):
        """Forward a VNC data frame to the agent serving the session."""
        # Look up the agent for this session.
        aid = getattr(self, "vnc_sessions", {}).get(d.get("session_id") or "")
        # Forward the frame without waiting for a reply.
        if aid and self.control_plane:
            await self.control_plane.send_raw_to_agent(aid, "VNC_FRAME_DOWN", d)
        # Always acknowledge.
        return {"status": "OK"}

    async def _cmd_vnc_disconnect(self, cmd, d):
        """End a VNC session and tell its agent."""
        # Forget the session and get its agent.
        aid = getattr(self, "vnc_sessions", {}).pop(d.get("session_id") or "", None)
        # Tell the agent to close it.
        if aid and self.control_plane:
            await self.control_plane.send_raw_to_agent(aid, "VNC_DISCONNECT", d)
        # Always acknowledge.
        return {"status": "OK"}

    # ── Host-shell (xterm terminal) relay — same routing as VNC ─────────
    # PORT of ProxmoxSpoke.SHELL_START/IN/RESIZE/DISCONNECT. In the all-cs-
    # hosted topology the pxmx agents dial THIS cs spoke, so — exactly like
    # VNC above — the cs spoke must relay the host shell to them. Without
    # this branch a request_response(cs_spoke, "SHELL_START") fell through to
    # a generic error, surfaced by the hub as "agent refused SHELL_START",
    # so VM Server → Terminal never opened. SHELL_START is sync (spawns the
    # PTY); SHELL_IN/RESIZE/DISCONNECT are fire-and-forget (high-volume
    # keystrokes must not block the dispatch loop). The agent emits
    # SHELL_OUT/READY/ERROR/DISCONNECT up via AGENT_RELAY_UP.
    async def _cmd_shell_start(self, cmd, d):
        """Start a host shell (terminal) session on the agent for the requested node."""
        # Lazily create the session_id -> agent_id map.
        if not hasattr(self, "shell_sessions"):
            self.shell_sessions = {}
        # Session id and an explicitly requested agent, if any.
        session_id = d.get("session_id") or ""
        agent_id = d.get("agent_id") or d.get("target_agent_id")
        # Reuse the agent already bound to this session.
        if not agent_id and session_id:
            agent_id = self.shell_sessions.get(session_id)
        # Not known yet and a control plane exists: try to resolve the agent from the node.
        if not agent_id and self.control_plane:
            # Otherwise resolve by Proxmox node name or by the cluster prefix of unique_id
            # ("cluster/...").
            node = str(d.get("node") or "")
            uid = str(d.get("unique_id") or "")
            cluster = uid.split("/")[0] if "/" in uid else ""
            # Find a connected agent whose hostname or cluster name matches.
            for aid, info in self.control_plane.connected_agents.items():
                hosts = {info.get("hostname"), info.get("cluster_name")}
                # Use the first match.
                if (node and node in hosts) or (cluster and cluster in hosts):
                    agent_id = aid
                    break
            # With exactly one agent connected, it must be the one.
            if not agent_id and len(self.control_plane.connected_agents) == 1:
                agent_id = next(iter(self.control_plane.connected_agents))
        # No agent could be resolved.
        if not agent_id:
            return {"status": "ERROR", "message": "No agent resolved for SHELL_START"}
        # Remember which agent serves this session.
        if session_id:
            self.shell_sessions[session_id] = agent_id
            # Phase 2: register the relay token so a co-located edge proxy
            # can attach a /ws/console-relay leg for this shell session
            # (mirrors ProxmoxSpoke.SHELL_START).
            # Register the relay token if the control plane supports console relays.
            relay_token = d.get("relay_token")
            if relay_token and hasattr(self.control_plane, "register_console_relay"):
                try:
                    self.control_plane.register_console_relay(
                        session_id, relay_token, agent_id, "shell")
                # Relay registration is optional; ignore failures.
                except Exception:  # noqa: BLE001
                    pass
        # Ask the agent to start the shell (20 s limit) and return its reply.
        return await self.control_plane.send_to_agent(
            "SHELL_START", d, agent_id=agent_id, timeout=20.0)

    async def _cmd_shell_in(self, cmd, d):
        """Forward terminal input or a resize to the agent serving the session."""
        # Look up the agent for this session.
        aid = getattr(self, "shell_sessions", {}).get(d.get("session_id") or "")
        # Forward the original command (SHELL_IN or SHELL_RESIZE) without waiting.
        if aid and self.control_plane:
            await self.control_plane.send_raw_to_agent(aid, cmd, d)
        # Always acknowledge.
        return {"status": "OK"}

    async def _cmd_shell_disconnect(self, cmd, d):
        """End a shell session and tell its agent."""
        # Forget the session and get its agent.
        aid = getattr(self, "shell_sessions", {}).pop(d.get("session_id") or "", None)
        # Remove any console relay registered for this session.
        if self.control_plane and hasattr(self.control_plane, "unregister_console_relay"):
            self.control_plane.unregister_console_relay(d.get("session_id") or "")
        # Tell the agent to close the shell.
        if aid and self.control_plane:
            await self.control_plane.send_raw_to_agent(aid, "SHELL_DISCONNECT", d)
        # Always acknowledge.
        return {"status": "OK"}

    # ── Drive health / ssacli install relay (split topology) ────────────
    # PORT of ProxmoxSpoke's PXMX_DRIVE_HEALTH / PXMX_INSTALL_SSACLI
    # branches (proxmox_spoke.py:340-374). In the all-cs-hosted topology
    # the pxmx host agents dial THIS cs spoke, so the hub's drive-health
    # route (which already treats "simulation" spokes as valid hypervisor
    # targets — see hub_spoke_registry.get_hypervisor_spokes_for_tenant)
    # sends the raw command here. Without this branch it fell through to
    # the generic "Unknown command: PXMX_DRIVE_HEALTH" error, so the CS-
    # hosted nodes' Drive Health panel showed "Agent vunknown" / 0 drives
    # even though the SAME pxmx agent binary (with drive-health support)
    # is running on those boxes. Node resolution mirrors VNC_START/
    # SHELL_START above: explicit agent_id first, else match "node" (or
    # unique_id's cluster prefix) against a connected agent's
    # hostname/cluster_name, else fall back to the sole connected agent.
    async def _cmd_pxmx_drive_health(self, cmd, d):
        """Relay a drive-health query or ssacli install to the right Proxmox agent."""
        # No control plane: no agents can be reached.
        if not self.control_plane:
            return {"status": "ERROR", "message": "not connected to a control plane"}
        # An explicitly requested agent, if any.
        agent_id = d.get("agent_id") or d.get("target_agent_id")
        # Otherwise resolve it from the node name (same rules as VNC/shell).
        if not agent_id:
            # Otherwise resolve by Proxmox node name or by the cluster prefix of unique_id
            # ("cluster/...").
            node = str(d.get("node") or "")
            uid = str(d.get("unique_id") or "")
            cluster = uid.split("/")[0] if "/" in uid else ""
            # Find a connected agent whose hostname or cluster name matches.
            for aid, info in self.control_plane.connected_agents.items():
                hosts = {info.get("hostname"), info.get("cluster_name")}
                # Use the first match.
                if (node and node in hosts) or (cluster and cluster in hosts):
                    agent_id = aid
                    break
            # With exactly one agent connected, it must be the one.
            if not agent_id and len(self.control_plane.connected_agents) == 1:
                agent_id = next(iter(self.control_plane.connected_agents))
        # No agent could be resolved.
        if not agent_id:
            return {"status": "ERROR", "message": "No agent resolved for node"}
        # Name to show for this node (cluster name, else the agent id).
        cluster_name = (self.control_plane.connected_agents or {}).get(
            agent_id, {}).get("cluster_name", agent_id)
        # Empty result shape used if the agent call fails.
        fallback = ({"drives": []} if cmd == "PXMX_DRIVE_HEALTH"
                    else {"installed": False})
        # Drive health is quick (30 s); installing ssacli can take longer (120 s).
        timeout = 30.0 if cmd == "PXMX_DRIVE_HEALTH" else 120.0
        try:
            # Send the command to the agent.
            r = await self.control_plane.send_to_agent(
                cmd, {}, agent_id=agent_id, timeout=timeout)
            # Unwrap payload.data when present.
            result = (r.get("payload", {}).get("data", r)
                      if isinstance(r, dict) else r)
            # Tag the result with the cluster name.
            if isinstance(result, dict):
                result["cluster"] = cluster_name
            return result
        # Log the failure and return an error plus the empty fallback data.
        except Exception as exc:  # noqa: BLE001 - one agent's failure must not raise
            logger.debug("%s agent %s failed: %s", cmd, agent_id, exc)
            return {"status": "ERROR", "message": str(exc),
                    "cluster": cluster_name, **fallback}
