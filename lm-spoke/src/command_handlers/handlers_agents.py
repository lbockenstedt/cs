"""Agents command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_agents`` scans only its own command group and
returns the result dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
import asyncio
from typing import Any, Dict, Optional

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


class AgentCommandsMixin:
    async def _dispatch_agents(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:

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
            logger.info(
                "[cs-spoke] SPOKE_RELAY received: command=%r target_agent_id=%r "
                "control_plane=%s", command, target, bool(self.control_plane))
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

        if cmd == "PXMX_RETAG_TENANT":
            # Cross-tenant migration, split topology: THIS cs spoke hosts the
            # pxmx agents, so re-tag (old_tag -> new_tag) by broadcasting to each
            # connected agent (mirrors _install_cert_relay's fan-out).
            if not self.control_plane:
                return {"status": "ERROR", "message": "not connected to a control plane"}
            connected = dict(self.control_plane.connected_agents or {})
            if not connected:
                return {"status": "DEFERRED", "count": 0,
                        "message": "no managed pxmx agents connected — deferred, retries on reconnect"}

            async def _retag_one(aid: str) -> Dict[str, Any]:
                try:
                    r = await self.control_plane.send_to_agent(
                        "PXMX_RETAG_TENANT", d, agent_id=aid, timeout=120.0)
                except Exception as exc:  # noqa: BLE001 - one node must not abort the rest
                    return {"agent_id": aid, "status": "ERROR", "count": 0, "message": str(exc)}
                rret = (r.get("payload", {}).get("data", r) if isinstance(r, dict) else {})
                if not isinstance(rret, dict):
                    rret = {}
                return {"agent_id": aid, "status": rret.get("status", "ERROR"),
                        "count": int(rret.get("count", 0) or 0), "message": rret.get("message", "")}

            nodes = list(await asyncio.gather(*[_retag_one(a) for a in connected]))
            total = sum(n["count"] for n in nodes)
            any_err = any(n["status"] not in ("SUCCESS", None) for n in nodes)
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
        if cmd == "SHELL_START":
            if not hasattr(self, "shell_sessions"):
                self.shell_sessions = {}
            session_id = d.get("session_id") or ""
            agent_id = d.get("agent_id") or d.get("target_agent_id")
            if not agent_id and session_id:
                agent_id = self.shell_sessions.get(session_id)
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
                return {"status": "ERROR", "message": "No agent resolved for SHELL_START"}
            if session_id:
                self.shell_sessions[session_id] = agent_id
                # Phase 2: register the relay token so a co-located edge proxy
                # can attach a /ws/console-relay leg for this shell session
                # (mirrors ProxmoxSpoke.SHELL_START).
                relay_token = d.get("relay_token")
                if relay_token and hasattr(self.control_plane, "register_console_relay"):
                    try:
                        self.control_plane.register_console_relay(
                            session_id, relay_token, agent_id, "shell")
                    except Exception:  # noqa: BLE001
                        pass
            return await self.control_plane.send_to_agent(
                "SHELL_START", d, agent_id=agent_id, timeout=20.0)

        if cmd in ("SHELL_IN", "SHELL_RESIZE"):
            aid = getattr(self, "shell_sessions", {}).get(d.get("session_id") or "")
            if aid and self.control_plane:
                await self.control_plane.send_raw_to_agent(aid, cmd, d)
            return {"status": "OK"}

        if cmd == "SHELL_DISCONNECT":
            aid = getattr(self, "shell_sessions", {}).pop(d.get("session_id") or "", None)
            if self.control_plane and hasattr(self.control_plane, "unregister_console_relay"):
                self.control_plane.unregister_console_relay(d.get("session_id") or "")
            if aid and self.control_plane:
                await self.control_plane.send_raw_to_agent(aid, "SHELL_DISCONNECT", d)
            return {"status": "OK"}
        return None
