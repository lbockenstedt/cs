"""CommandQueue + CSSettings — the cs (Client-Simulation) spoke's command
queue and USB-config store.

Phase D2 port of the legacy ``cs/webui-spoke/server.py`` command queue
(``_make_command``/``_enqueue_command_locked``/``_peek_pending_agent_commands``
``_ack_command_internal``: lines 3509-4513, 13356-13463) and USB-config payload
(``_proxmox_usb_config_payload``: lines 4673-4724).

The cs spoke owns the queue; the LM hub's ``CSBridgePoller``
(``lm/core/src/gateway/cs_bridge.py``) polls it via ``CS_POLL_AGENT_INBOX``,
relays each pending command to the unified pxmx agent as ``CS_COMMAND`` (through
the pxmx spoke's ``SPOKE_RELAY``), and acks the terminal result back via
``CS_ACK_COMMAND``. USB config for the agent is read from a small
``CSSettings`` store via ``CS_GET_USB_CONFIG``; the hub bridge diffs it and
pushes changes down to the agent's ``client_simulation.usb_config`` through
``SET_AGENT_CONFIG`` → ``UPDATE_CONFIG``.

Command shape (mirrors legacy ``_make_command`` — 11 keys)::

    {id, target, action, args, type, status, created_at, updated_at,
     expires_at, purge_after, result, message}

Statuses: ``pending → delivered → completed/failed/expired``.

Semantics (ported from the legacy queue):
  - **idempotent enqueue**: a second enqueue with the same target+action+args
    signature while one is still ``pending``/``delivered`` returns the existing
    command (no duplicate).
  - **stale-delivered reset**: on poll, a ``delivered`` command older than
    ``STALE_DELIVERED_SECS`` (default 30s, env ``CS_STALE_DELIVERED_SECS``) with
    no ack is reset to ``pending`` so it is re-delivered (mirrors the legacy
    WS-reconnect reset).
  - **cleanup**: ``pending``/``delivered`` older than ``COMMAND_EXPIRE_SECS``
    (default 7200s = 2h, env ``CS_COMMAND_EXPIRE_SECS``) become ``expired``;
    terminal commands past ``purge_after`` are dropped (retention
    ``COMMAND_RESULT_RETENTION_SECS``, default 86400s, env
    ``CS_COMMAND_RESULT_RETENTION_SECS``). The 2h default covers a
    cloud-connected agent offline past the old 15-min window; the env knob
    lets a site tune the stale-command tradeoff (a delete queued before a
    long outage firing much later, possibly against rebuilt state).
  - **trim**: queue capped at ``COMMAND_MAX`` (default 100, env
    ``CS_COMMAND_MAX``); terminal commands dropped oldest-first.
  - **ack**: idempotent terminal update (``completed``/``failed``); sets
    ``message``/``updated_at``/``purge_after``; no prior-state check (a late ack
    for an already-terminal command re-records the result).

Safeguard (defense-in-depth on top of the agent's execution-layer
``cs_guard``): enqueue of a ``_VM_ACTIONS`` command on ``target=="proxmox"``
refuses any ``vmid < SIM_VMIN`` (90000) or in ``protected_vmids`` (default
``{1001}``; configurable per host from the hub). Sim VMs are 90001+, so the cs
UI only ever manages sim VMs; the hub/system containers stay untouchable.

Source of truth: ``cs/webui-spoke/server.py``
  - enqueue:        ``_enqueue_command_locked`` (lines 4382-4413)
  - make command:   ``_make_command`` (4414-4430)
  - cleanup/trim:   ``_cleanup_commands_locked`` (4340-4367), ``_trim_commands_locked`` (4323-4338)
  - duplicate find: ``_find_active_duplicate_command_locked`` (4370-4381)
  - poll:           ``_peek_pending_agent_commands_locked`` (4514+), ``_reset_delivered_commands_locked`` (4454-4475)
  - ack:            ``_ack_command_internal`` (13419-13440)
  - usb config:     ``_proxmox_usb_config_payload`` (4673-4724)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("CSCommandQueue")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except Exception:
        return default


# ── tunables (env-overridable so cloud / high-latency sites can stretch them) ─
COMMAND_MAX = _env_int("CS_COMMAND_MAX", 100, 10)        # keep last N commands in memory
# A pending/delivered command older than this from CREATION is marked expired.
# Default 2h — a cloud-connected agent can be offline (link flap / NAT idle /
# WAN brownout) well past the old 15-min window; expiring a delete_vm at 15 min
# meant the VM was never torn down when the agent finally reconnected. Bump via
# CS_COMMAND_EXPIRE_SECS for sites with longer outages; lower it for on-prem
# sites that want stale commands reaped faster (stale-command tradeoff: a
# delete queued before a long outage fires much later, possibly against
# rebuilt state — keep it bounded to what your outage window realistically is).
COMMAND_EXPIRE_SECS = _env_int("CS_COMMAND_EXPIRE_SECS", 7200, 60)
# Terminal rows (completed/failed/expired) kept for the queue view, then purged.
COMMAND_RESULT_RETENTION_SECS = _env_int("CS_COMMAND_RESULT_RETENTION_SECS", 86400, 60)
# A delivered command with no ack older than this is reset to pending so the
# next poll re-sends it. 30s is fine on a tight LAN; raise it on a high-latency
# cloud link where the agent's ack can legitimately take longer (avoids
# needless re-sends that double-execute an idempotent-but-noisy op).
STALE_DELIVERED_SECS = _env_int("CS_STALE_DELIVERED_SECS", 30, 5)

# Verified-report safety net for delete_vm / reclone_vm: a delivered long op
# that hasn't been TOUCHED (the bridge refreshes updated_at on every ACCEPTED
# re-ack while the agent is alive) or acked for this long is treated as LOST —
# the agent crashed, the WS dropped, or the long-op task died with no terminal.
# The verify-sweep requeues it (bounded) so it genuinely re-runs instead of
# spinning until the 2h COMMAND_EXPIRE_SECS (the "some VMs never get deleted
# on a bulk delete" symptom). 5 min/try × 2 tries ≈ 10 min max safety net; a
# healthy delete finishes in well under a minute so this only fires on real
# stalls. The bridge's touch keeps a slow-but-alive delete from being penalized.
DELETE_VERIFY_TIMEOUT_SECS = _env_int("CS_DELETE_VERIFY_TIMEOUT_S", 300, 30)
DELETE_VERIFY_MAX_RETRIES = _env_int("CS_DELETE_MAX_RETRIES", 2, 1)
# Actions covered by the verify-report net (the long teardown ops).
_VERIFY_ACTIONS = {"delete_vm", "reclone_vm"}

# Single-VM actions that take a ``vmid`` arg and must respect the sim range +
# protected-VMID guard at enqueue time (defense-in-depth; the agent's cs_guard
# enforces the same at execution).
_VM_ACTIONS = {"start_vm", "stop_vm", "reboot_vm", "snapshot_vm", "reclone_vm", "delete_vm"}

# Sim VMID floor (matches ``cs_guard.SIM_VMIN`` shipped in Phase B). VMs below
# this are not Client-Simulation VMs and must never be managed from the cs UI.
SIM_VMIN = 90000
DEFAULT_PROTECTED_VMIDS: Set[int] = {1001}


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_action(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalize_type(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().replace("-", "_")
    return normalized or None


def _args_signature(args: Optional[Dict[str, Any]]) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        safe = json.loads(json.dumps(args or {}, default=str))
        return json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_hostname(hostname: Any) -> str:
    return str(hostname or "").strip().rstrip(".").lower()


def _hostname_aliases(hostname: Any) -> Tuple[str, ...]:
    normalized = _normalize_hostname(hostname)
    if not normalized:
        return ()
    aliases = [normalized]
    short = normalized.split(".", 1)[0]
    if short and short not in aliases:
        aliases.append(short)
    return tuple(aliases)


def _hostnames_match(left: Any, right: Any) -> bool:
    la = set(_hostname_aliases(left))
    return bool(la and la.intersection(_hostname_aliases(right)))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── CommandQueue ─────────────────────────────────────────────────────────────

class CommandQueue:
    """Persisted command queue + ack surface for the cs spoke.

    One instance lives on the ``CSSpoke`` module; ``handle_command`` dispatches
    ``CS_QUEUE_COMMAND``/``CS_POLL_AGENT_INBOX``/``CS_ACK_COMMAND``/``CS_GET_USB_CONFIG``
    to it.
    """

    def __init__(self, data_dir: Path, settings: CSSettings,
                 protected_vmids: Optional[Set[int]] = None,
                 sim_vmin: int = SIM_VMIN) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "command_queue.json"
        self.settings = settings
        self.lock = asyncio.Lock()
        self.sim_vmin = sim_vmin
        # Protected set: explicit per-spoke config wins; else the settings store
        # (cs UI) wins; else the hardcoded default so the hub is never unprotected.
        if protected_vmids is not None:
            self.protected_vmids = set(protected_vmids)
        else:
            cfg_set = settings.protected_vmids() if settings else set()
            self.protected_vmids = cfg_set or set(DEFAULT_PROTECTED_VMIDS)
        self.commands: List[Dict[str, Any]] = []
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8") or "[]")
                if isinstance(loaded, list):
                    self.commands = [c for c in loaded if isinstance(c, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue load failed (%s): %s", self.path, exc)

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps(self.commands, default=str))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue save failed (%s): %s", self.path, exc)

    async def _asave(self) -> None:
        """Persist off the event loop. The command list is serialized HERE (on
        the loop, so it's a consistent snapshot while callers hold self.lock),
        but the slow atomic disk write is offloaded to a thread. This is the hot
        path — CS_POLL_AGENT_INBOX (~5s) and CS_ACK_COMMAND both save — and a
        synchronous write on a contended disk stalled the whole shared event
        loop (hub connection + uvicorn API), producing the hub's 5s/30s Request
        Timeouts. See logging/observability + cs-svr-02 starvation notes."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.commands, default=str)
            await asyncio.to_thread(_write_atomic, self.path, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Command queue save failed (%s): %s", self.path, exc)

    # ── shape helpers ──────────────────────────────────────────────────────

    def _make_command(self, target: str, action: str, args: Optional[dict],
                      command_type: Optional[str]) -> Dict[str, Any]:
        now = time.time()
        return {
            "id": str(uuid.uuid4()),
            "target": target,
            "action": _normalize_action(action),
            "args": dict(args or {}),
            "type": _normalize_type(command_type),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            # Last REAL agent contact for this command: refreshed by an ACCEPTED
            # touch (CS_TOUCH_COMMAND from the bridge), a progress frame, or the
            # terminal ack. NOT refreshed by the 30s stale re-probe or a requeue,
            # so it ages while the agent is silent and the delete-verify sweep
            # (DELETE_VERIFY_TIMEOUT_SECS) can bound a lost long op. Old persisted
            # commands without this key fall back to created_at via .get().
            "last_contact": now,
            "expires_at": now + COMMAND_EXPIRE_SECS,
            "purge_after": None,
            "result": None,
            "message": None,
            # How many times the hub's CSBridgePoller has re-queued this command
            # after a relay timeout (the agent was too busy to ACCEPT within
            # CS_RELAY_TIMEOUT_S). Re-queue retries up to ``max_retries`` before
            # the command is marked failed — see ``requeue_command``. A fresh
            # command has 0 attempts. Old persisted commands without this key
            # default to 0 via .get().
            "relay_attempts": 0,
        }

    @staticmethod
    def _serialize_for_agent(cmd: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": cmd["id"], "action": cmd["action"],
                "args": cmd.get("args", {}), "type": cmd.get("type")}

    def _find_active_duplicate(self, target: str, action: str,
                               args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sig = _args_signature(args)
        for cmd in self.commands:
            if cmd.get("target") != target or cmd.get("action") != action:
                continue
            if cmd.get("status") not in {"pending", "delivered"}:
                continue
            if _args_signature(cmd.get("args", {})) == sig:
                return cmd
        return None

    def _cleanup(self, now: Optional[float] = None) -> Tuple[int, int]:
        now = now if now is not None else time.time()
        expired = 0
        for cmd in self.commands:
            if cmd.get("status") in {"pending", "delivered"} and \
                    (now - float(cmd.get("created_at", now))) > COMMAND_EXPIRE_SECS:
                cmd["status"] = "expired"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                expired += 1
        before = len(self.commands)
        self.commands[:] = [
            cmd for cmd in self.commands
            if cmd.get("status") not in {"completed", "failed", "expired"}
            or now < float(cmd.get("purge_after") or
                           cmd.get("updated_at", cmd.get("created_at", now)) +
                           COMMAND_RESULT_RETENTION_SECS)
        ]
        purged = before - len(self.commands)
        self._trim()
        return expired, purged

    def _trim(self) -> None:
        if len(self.commands) <= COMMAND_MAX:
            return
        terminal = {"completed", "failed", "expired"}
        idx = 0
        while len(self.commands) > COMMAND_MAX and idx < len(self.commands):
            if self.commands[idx].get("status") in terminal:
                del self.commands[idx]
                continue
            idx += 1
        if len(self.commands) > COMMAND_MAX:
            del self.commands[:len(self.commands) - COMMAND_MAX]

    # ── safeguard ──────────────────────────────────────────────────────────

    def _refused(self, target: str, action: str, args: Dict[str, Any]) -> Optional[str]:
        """Return a refusal reason if the command must not be enqueued, else None."""
        if target != "proxmox" or action not in _VM_ACTIONS:
            return None
        vmid = args.get("vmid")
        try:
            v = int(vmid)
        except (TypeError, ValueError):
            return None  # agent validates; do not block ambiguous enqueue
        if v in self.protected_vmids:
            return f"VM {v} is protected (hub/system container)"
        if v < self.sim_vmin:
            return f"VM {v} outside Client-Simulation range (>= {self.sim_vmin})"
        return None

    # ── public API (called from CSSpoke.handle_command) ────────────────────

    async def enqueue(self, target: str, action: str,
                      args: Optional[Dict[str, Any]] = None,
                      command_type: Optional[str] = None) -> Dict[str, Any]:
        """Idempotently enqueue a command. Returns
        ``{command, created, expired, purged}``. Raises ``ValueError`` if the
        safeguard refuses the command (so the spoke surfaces a 403-style ERROR)."""
        async with self.lock:
            ntarget = _normalize_hostname(target) or str(target or "").strip()
            naction = _normalize_action(action)
            ntype = _normalize_type(command_type)
            nargs = dict(args or {})

            reason = self._refused("proxmox" if ntarget == "proxmox" else ntarget,
                                   naction, nargs)
            if reason:
                raise ValueError(reason)

            now = time.time()
            expired, purged = self._cleanup(now)
            existing = self._find_active_duplicate(ntarget, naction, nargs)
            if existing is not None:
                return {"command": existing, "created": False,
                        "expired": expired, "purged": purged}

            cmd = self._make_command(ntarget, naction, nargs, ntype)
            self.commands.append(cmd)
            self._trim()
            await self._asave()
            return {"command": cmd, "created": True,
                    "expired": expired, "purged": purged}

    def _command_matches_agent(self, cmd: Dict[str, Any], hostname: str) -> bool:
        if cmd.get("target") == hostname:
            return True
        if cmd.get("target") == "proxmox":
            return True
        return _hostnames_match(cmd.get("target", ""), hostname)

    async def poll_agent_inbox(self, hostname: str,
                                max_retries: int = 0) -> Dict[str, Any]:
        """Reset stale delivered→pending, cleanup, return serialized pending for
        this host, and mark them delivered. Returns
        ``{commands, expired, purged, reset, delivered, gave_up}``.

        ``max_retries`` caps the total re-send attempts for a command that's
        delivered but never acked (no CS_COMMAND_RESULT, no progress frames —
        the agent lost it or crashed mid-op). Each stale reset increments
        ``relay_attempts`` (shared with the relay-timeout requeue path); once
        it reaches ``max_retries`` the command is marked ``failed`` instead of
        re-sent — "re-send up to X times then give up", the same budget as a
        relay timeout. ``max_retries <= 0`` preserves the old unbounded
        re-send behavior (re-send every STALE_DELIVERED_SECS until the command
        expires). A running long op that streams CS_PROGRESS never hits this:
        ``touch_command`` refreshes ``updated_at`` on each progress frame."""
        hn = _normalize_hostname(hostname)
        async with self.lock:
            now = time.time()
            expired, purged = self._cleanup(now)

            # Reset stale delivered (>STALE_DELIVERED_SECS, no ack) → pending,
            # capped at max_retries (shared with the relay-timeout requeue
            # budget) — a command that's been re-sent max_retries times with
            # no ack is marked failed, not re-sent forever.
            reset = 0
            gave_up = 0
            for cmd in self.commands:
                if cmd.get("status") == "delivered" and \
                        (now - float(cmd.get("updated_at", now))) > STALE_DELIVERED_SECS and \
                        self._command_matches_agent(cmd, hn):
                    # delete_vm / reclone_vm: the 30s reset is a non-budgeted
                    # RE-PROBE ("is the agent back yet?"), not a give-up step.
                    # The few-minutes bound is owned by the verify-sweep below
                    # (which uses last_contact, not updated_at) so these periodic
                    # re-probes don't consume the retry budget or refresh the
                    # verify window. Other actions keep the old bounded behavior.
                    if cmd.get("action") in _VERIFY_ACTIONS:
                        cmd["status"] = "pending"
                        cmd["updated_at"] = now
                        reset += 1
                        continue
                    attempts = int(cmd.get("relay_attempts", 0)) + 1
                    cmd["relay_attempts"] = attempts
                    if max_retries > 0 and attempts >= max_retries:
                        # Exhausted the re-send budget — give up (terminal).
                        cmd["status"] = "failed"
                        cmd["message"] = (f"no ack after {attempts} "
                                          f"re-send attempt(s) — gave up")
                        cmd["updated_at"] = now
                        cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                        gave_up += 1
                    else:
                        cmd["status"] = "pending"
                        cmd["updated_at"] = now
                        reset += 1

            # Verified-report safety net for delete_vm / reclone_vm: a delivered
            # long op with no REAL agent contact (ACCEPTED touch / progress /
            # terminal → last_contact) for longer than DELETE_VERIFY_TIMEOUT_SECS
            # is LOST. Requeue it (bounded) so it genuinely re-runs — the agent
            # re-spawns a dead task (liveness-aware dedup) instead of dedup-
            # suppressing forever. last_contact is NOT refreshed by the 30s
            # re-probe above, so a silent agent ages toward this bound while a
            # slow-but-alive delete (re-acked every 30s → touched) does not.
            verify_requeued = 0
            verify_gave_up = 0
            for cmd in self.commands:
                if cmd.get("status") != "delivered" or \
                        cmd.get("action") not in _VERIFY_ACTIONS or \
                        not self._command_matches_agent(cmd, hn):
                    continue
                last_contact = float(cmd.get("last_contact",
                                             cmd.get("updated_at", now)))
                if (now - last_contact) <= DELETE_VERIFY_TIMEOUT_SECS:
                    continue
                attempts = int(cmd.get("relay_attempts", 0)) + 1
                cmd["relay_attempts"] = attempts
                if attempts < DELETE_VERIFY_MAX_RETRIES:
                    cmd["status"] = "pending"
                    cmd["message"] = (f"no verified report after "
                                      f"{DELETE_VERIFY_TIMEOUT_SECS}s — requeuing "
                                      f"(attempt {attempts}/{DELETE_VERIFY_MAX_RETRIES})")
                    cmd["updated_at"] = now
                    # Reset the verify window so the re-send gets a fresh
                    # DELETE_VERIFY_TIMEOUT_SECS to earn a touch/terminal before
                    # the next attempt — otherwise the sweep would fire on the
                    # very next poll (last_contact still old) and exhaust the
                    # budget in seconds, not minutes.
                    cmd["last_contact"] = now
                    verify_requeued += 1
                else:
                    cmd["status"] = "failed"
                    cmd["message"] = (f"no verified report after {attempts} "
                                      f"attempt(s) — gave up")
                    cmd["updated_at"] = now
                    cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                    verify_gave_up += 1

            pending = [c for c in self.commands
                       if c.get("status") == "pending" and self._command_matches_agent(c, hn)]
            delivered_ids = [c["id"] for c in pending]
            for cmd in self.commands:
                if cmd["id"] in set(delivered_ids) and cmd.get("status") == "pending":
                    cmd["status"] = "delivered"
                    cmd["updated_at"] = now

            if reset or delivered_ids or expired or purged or gave_up \
                    or verify_requeued or verify_gave_up:
                await self._asave()

            return {
                "commands": [self._serialize_for_agent(c) for c in pending],
                "expired": expired,
                "purged": purged,
                "reset": reset,
                "delivered": delivered_ids,
                "gave_up": gave_up,
                "verify_requeued": verify_requeued,
                "verify_gave_up": verify_gave_up,
            }

    async def ack_command(self, cmd_id: str, status: str,
                          message: Any = None, result: Any = None) -> Dict[str, Any]:
        """Idempotent terminal update. ``status`` must be ``completed`` or
        ``failed`` (long-op ``CS_COMMAND_RESULT`` in Phase E maps here)."""
        status = str(status or "").strip().lower()
        if status not in ("completed", "failed"):
            return {"ok": False, "message": "status must be 'completed' or 'failed'"}
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            self._cleanup()
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            cmd["status"] = status
            cmd["message"] = str(message) if message is not None else (cmd.get("message") or "")
            cmd["result"] = result if result is not None else cmd.get("result")
            cmd["updated_at"] = time.time()
            cmd["last_contact"] = cmd["updated_at"]
            cmd["purge_after"] = cmd["updated_at"] + COMMAND_RESULT_RETENTION_SECS
            await self._asave()
            return {"ok": True, "id": cmd_id, "status": status}

    async def requeue_command(self, cmd_id: str, max_retries: int = 5,
                              message: Optional[str] = None) -> Dict[str, Any]:
        """Re-queue a ``delivered`` command whose hub→agent relay TIMED OUT
        (the agent was too busy to ACCEPT within ``CS_RELAY_TIMEOUT_S``) instead
        of marking it dead. Bounded: increments ``relay_attempts`` and resets
        status → ``pending`` so the CSBridgePoller's next tick re-relays it,
        up to ``max_retries``. Once ``relay_attempts`` reaches ``max_retries``
        the command is marked ``failed`` (terminal) with the timeout message —
        this is the "retry 5 then give up" the operator asked for, instead of a
        single relay timeout killing a mass-delete on a busy agent.

        Idempotent-ish: a command that isn't currently ``delivered`` (already
        completed/failed/expired, or pending) is left alone (returns its
        current status) — the bridge may call this twice for the same timeout
        if a re-deliver races a requeue. ``max_retries <= 0`` means "never
        retry" → the first timeout fails the command (preserves the old
        behavior for a site that wants fail-fast)."""
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            self._cleanup()
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            cur = str(cmd.get("status", "")).lower()
            if cur in ("completed", "failed", "expired"):
                return {"ok": True, "id": cmd_id, "status": cur,
                        "requeued": False, "attempts": int(cmd.get("relay_attempts", 0))}
            attempts = int(cmd.get("relay_attempts", 0)) + 1
            cmd["relay_attempts"] = attempts
            now = time.time()
            if max_retries > 0 and attempts < max_retries:
                cmd["status"] = "pending"
                cmd["updated_at"] = now
                await self._asave()
                return {"ok": True, "id": cmd_id, "status": "pending",
                        "requeued": True, "attempts": attempts,
                        "max_retries": max_retries}
            # Exhausted retries (or max_retries<=0 fail-fast) → terminal failed.
            cmd["status"] = "failed"
            cmd["message"] = (message or "relay timed out — gave up after "
                              f"{attempts} attempt(s)")
            cmd["updated_at"] = now
            cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
            await self._asave()
            return {"ok": True, "id": cmd_id, "status": "failed",
                    "requeued": False, "attempts": attempts,
                    "max_retries": max_retries}

    async def touch_command(self, cmd_id: str,
                             message: Optional[str] = None) -> Dict[str, Any]:
        """Refresh a ``delivered`` command's ``updated_at`` on a CS_INGEST_PROGRESS
        tick so the ``STALE_DELIVERED_SECS`` reset doesn't re-send an in-flight
        long op (delete_vm / reclone_vm can take minutes; without this, a
        delivered command with no ack for 30s is reset to ``pending`` and
        re-relayed — duplicating the op while the first one is still running,
        the "some don't get deleted / queue grows" symptom on slow storage).

        Only refreshes a ``delivered`` command (a running long op); ``pending``
        (not yet relayed) and terminal (``completed``/``failed``/``expired``)
        are left alone. In-memory only — does NOT persist: a progress frame
        lands every second or so during a long op, and an atomic disk write per
        frame would be a write storm on the hot CS_POLL_AGENT_INBOX path. The
        next ``poll_agent_inbox`` (5s) saves, and the stale reset reads
        ``updated_at`` in-memory, which is exactly what this refreshes. A
        crash reverts to the last persisted ``updated_at`` — worst case a
        single re-send on restart, which the agent's ``_pending_delete_vmids``
        guard dedups."""
        cmd_id = str(cmd_id or "").strip()
        async with self.lock:
            cmd = next((c for c in self.commands if c.get("id") == cmd_id), None)
            if not cmd:
                return {"ok": False, "message": "Command not found"}
            if cmd.get("status") != "delivered":
                return {"ok": True, "id": cmd_id, "status": cmd.get("status"),
                        "touched": False}
            now = time.time()
            cmd["updated_at"] = now
            # Real agent contact (ACCEPTED / progress) → refresh last_contact,
            # the timestamp the delete-verify sweep watches.
            cmd["last_contact"] = now
            if message:
                cmd["message"] = str(message)
            return {"ok": True, "id": cmd_id, "status": "delivered",
                    "touched": True}

    async def list_commands(self) -> List[Dict[str, Any]]:
        async with self.lock:
            self._cleanup()
            # Hand back copies so callers can't mutate the live list.
            return [dict(c) for c in self.commands]

    async def clear_commands(self, target: Optional[str] = None) -> Dict[str, Any]:
        """Cancel (expire) all non-terminal commands, optionally scoped to a
        target. Mirrors the legacy ``DELETE /api/commands`` (cancel-all) and
        ``DELETE /api/commands/pending?target=`` (pre-teardown expiry so
        in-flight commands don't fire against a gone VM). Terminal commands
        (completed/failed/expired) are left for their retention window."""
        async with self.lock:
            now = time.time()
            ntarget = _normalize_hostname(target) if target else None
            cleared = 0
            for cmd in self.commands:
                if cmd.get("status") not in {"pending", "delivered"}:
                    continue
                if ntarget and not self._command_matches_agent(cmd, ntarget):
                    continue
                cmd["status"] = "expired"
                cmd["message"] = "cleared by operator"
                cmd["updated_at"] = now
                cmd["purge_after"] = now + COMMAND_RESULT_RETENTION_SECS
                cleared += 1
            if cleared:
                await self._asave()
            return {"cleared": cleared, "remaining": len(self.commands)}

    async def delete_command(self, cmd_id: str) -> Dict[str, Any]:
        """Remove a single command (any status). Mirrors the legacy
        ``DELETE /api/commands/{cmd_id}`` per-row delete."""
        cmd_id = str(cmd_id or "").strip()
        if not cmd_id:
            return {"ok": False, "message": "missing 'id'"}
        async with self.lock:
            before = len(self.commands)
            self.commands[:] = [c for c in self.commands if c.get("id") != cmd_id]
            removed = before - len(self.commands)
            if removed:
                await self._asave()
            return {"ok": bool(removed), "id": cmd_id, "removed": removed}

    async def get_usb_config(self, hostname: Optional[str] = None) -> Dict[str, Any]:
        return self.settings.usb_config_payload(hostname)


# ── small parsing helpers (ports of legacy setting coercions) ───────────────

def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _setting_int(value: Any, minimum: Optional[int] = None) -> int:
    try:
        n = int(str(value).strip())
    except Exception:
        n = 0
    if minimum is not None and n < minimum:
        n = minimum
    return n


def _setting_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _normalize_toggle(value: Any) -> str:
    return "on" if _setting_bool(value) else "off"


def _sanitize_vm_set_override(value: Any) -> int:
    try:
        n = int(str(value or "0").strip() or "0")
    except Exception:
        n = 0
    return max(0, n)


def _parse_int_ranges(raw: Any) -> Set[int]:
    """Parse ``"1001,1000-1002"`` into ``{1000,1001,1002}`` (legacy format)."""
    out: Set[int] = set()
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
                out.update(range(min(lo, hi), max(lo, hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(token))
            except ValueError:
                continue
    return out

# ── CSSettings re-export ─────────────────────────────────────────────────────
# CSSettings now lives in ``cs_settings.py`` (it couples to CommandQueue only via
# the constructor ``settings`` arg). It imports the shared normalizers defined
# above, so this re-export sits at the very bottom — by the time cs_settings is
# imported here, every name it pulls back from this module is already bound.
# Kept so the historical ``from command_queue import CommandQueue, CSSettings``
# import path continues to work unchanged.
from cs_settings import CSSettings  # noqa: E402,F401
