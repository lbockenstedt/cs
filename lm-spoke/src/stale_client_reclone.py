"""
stale_client_reclone.py — auto-reclones a sim VM that Proxmox reports as
RUNNING but whose simulation client has stopped reporting to the API.

This is a distinct failure mode from everything else already watched:

  - guest_watchdog (pxmx agent) pings the QEMU GUEST AGENT (`qm agent ping`)
    — a signal about the GUEST OS, not the sim client PROCESS running inside
    it. A hung/crashed client process with a perfectly responsive OS/QGA
    passes every ping forever; guest_watchdog never sees it, by design (it
    exists specifically for "OS wedged", a different fault).
  - the dongle-health ladder (pxmx agent, T2/USB-dongle VMs only) watches
    per-USB-bus L2/L3 signals, and DELIBERATELY never escalates a
    "no_gateway" state (associated, but no route to the gateway) — treated
    as an infra fault, not a dongle fault, so reboot/reclone is explicitly
    withheld there. It also has no equivalent at all for T1/T3 (PCI
    passthrough) VMs.
  - CS_GET_SIM_TAG_HEALTH (this spoke, handlers_config.py) already computes
    exactly this same "VM running, client hasn't reported recently" fact —
    but only for the diagnostic panel (desired=[] / "client_stale_tags_cleared").
    Nothing ever ACTED on it.

Reboot first: a flagged VM is rebooted, and only recloned if its client still
hasn't reported 15 min after that reboot (_REBOOT_WAIT_S).

This closes that gap: applies to every tier (whether the client can reach
the API doesn't depend on how its NIC is attached), and reuses the SAME
`reclone_vm` command the manual per-VM Reclone button already sends — no new
pxmx-agent-side code, no new command handler.

Debounce: a freshly (re)cloned VM is expected to be silent for its own
settle window (auto-prov's post-clone settle+reboot contract can run 15+
minutes on its own) — re-triggering on the SAME symptom immediately after a
reclone would just loop forever, destroying and rebuilding the same VM
every sweep. Per-vmid trigger timestamps are persisted to disk (survives a
spoke restart, unlike an in-memory dict) so this can't reset to "act
immediately" right after a restart mid-settle-window.

Only acts on a client that HAS reported before and then went silent, AND on a
VM whose client has NEVER reported once it has been running-yet-silent past the
(longer) grace window — both are "the sim isn't running in this VM, rebuild it".
The never-reported path is correlation-gated (it only fires when some OTHER VM's
client IS reporting, proving the VM-name↔hostname match works) and rate-capped,
so a fleet-wide telemetry hiccup or a global name-mismatch can't mass-reclone.
"""
# asyncio runs the periodic sweep loop.
import asyncio
# Per-VM timestamps are persisted as JSON files.
import json
# Standard-library logging for recovery actions.
import logging
# Current time for silence and cooldown checks.
import time
# Generates unique command ids for reclone requests.
import uuid
# Filesystem path handling for the state files.
from pathlib import Path
# Type hints only.
from typing import Any, Dict

# Dedicated logger for this watchdog.
logger = logging.getLogger("StaleClientReclone")

# How often the sweep runs (seconds).
_SCAN_INTERVAL_S = 300.0          # 5 min — cheap (local-cache reads only, no
                                  # new agent calls until something is flagged)
# How long a reporting client may be silent before recovery starts (seconds).
_STALE_RECLONE_S = 25 * 60.0      # 25 min of client silence while the VM is
                                  # confirmed running (20-30 min window)
# Minimum time between reclones of the same VM (seconds).
_RECLONE_COOLDOWN_S = 40 * 60.0   # after triggering, don't re-trigger the SAME
                                  # vmid for this long — covers auto-prov's own
                                  # post-clone settle+reboot window plus margin
                                  # for the fresh client's first heartbeat
# A running sim VM whose client has NEVER checked into the API (no registry row,
# or a row that never got a last_seen) is a DEAD clone — the sim never came up.
# We reclone it too, but only after it has been observed running-yet-silent for
# this GRACE window (longer than the silent-client threshold: a fresh clone gets
# its full auto-prov settle+reboot window AND first-heartbeat margin before we
# assume it's dead). Persisted per-vmid so a spoke restart can't reset the clock
# to "act immediately". Gated on correlation working (some OTHER VM's client IS
# reporting) so a fleet-wide telemetry/name-mismatch glitch can't mass-reclone.
# How long a never-reported VM must stay running-and-silent before recovery (seconds).
_NEVER_REPORTED_GRACE_S = 45 * 60.0
# Safety cap: at most this many NEVER-reported VMs recloned per sweep, so even if
# the correlation gate is somehow satisfied during a partial outage we bleed off
# dead VMs slowly instead of destroying the whole pool in one tick.
# Maximum never-reported VMs recovered per sweep.
_NEVER_REPORTED_MAX_PER_SWEEP = 3
# A flagged VM is rebooted first; reclone only if its client hasn't reported
# this long after the reboot.
# Time to wait after a reboot before falling back to a reclone (seconds).
_REBOOT_WAIT_S = 15 * 60.0
# Only VM ids above this are simulation clients; lower ids are never touched.
_VMID_FLOOR = 90000
# State file names: last reclone time, first time seen missing, and last reboot time for each VM.
_STATE_FILE = "stale_client_reclone.json"
_REBOOT_STATE_FILE = "stale_client_reboot.json"
_MISSING_STATE_FILE = "stale_client_reclone_missing.json"
# The pxmx agent starts a reclone in the background and answers ACCEPTED.
# Agent reply statuses that mean the reclone was accepted.
_ACCEPTED_STATUSES = {"ACCEPTED", "SUCCESS"}


def reclone_command(vmid: int) -> Dict[str, Any]:
    """The CS_COMMAND payload for a single-VM reclone.

    The pxmx agent only runs cs actions that arrive as a ``CS_COMMAND`` message
    carrying ``action`` (agent.py -> cs_commands.handle_cs_command); a bare
    ``reclone_vm`` message type is answered with "Unknown command" and nothing
    happens. ``cs_cmd_id`` is the agent's de-dup key for the long-running op.
    """
    # Build the command with a fresh unique id.
    return {"action": "reclone_vm", "vmid": vmid, "cs_cmd_id": uuid.uuid4().hex}


def reboot_command(vmid: int) -> Dict[str, Any]:
    """The CS_COMMAND payload for a single-VM reboot (fast, synchronous)."""
    # Build the reboot command.
    return {"action": "reboot_vm", "vmid": vmid}


def reply_status(reply: Any) -> str:
    """The status of an agent reply, whether or not it is wrapped in an
    AGENT_RESPONSE envelope (``{"payload": {"data": {...}}}``)."""
    # Non-dict replies have no status.
    if not isinstance(reply, dict):
        return ""
    # Prefer the status inside the AGENT_RESPONSE envelope when present.
    inner = (reply.get("payload") or {}).get("data")
    if isinstance(inner, dict) and inner.get("status"):
        return str(inner["status"]).upper()
    # Otherwise use the top-level status (upper-cased, "" if missing).
    return str(reply.get("status") or "").upper()


class StaleClientReclone:
    """Started by CSControlPlane.run(), same pattern as ClientRegistry.start()
    / DemoManager.start() / CentralPoller.start(). No-op start() if there's no
    running event loop (e.g. a spoke constructed standalone in a unit test)."""

    def __init__(self, spoke, data_dir: Path):
        """Load the persisted per-VM timestamps and remember the owning spoke."""
        # The owning spoke; provides the control plane, registry and Proxmox state.
        self.spoke = spoke
        # Paths of the reclone-time and missing-since state files.
        self._path = Path(data_dir) / _STATE_FILE
        self._missing_path = Path(data_dir) / _MISSING_STATE_FILE
        # Last reclone time per VM id, restored from disk.
        self._triggered: Dict[str, float] = self._load(self._path)
        # {vmid: epoch we FIRST saw this running sim VM with no reporting client}
        # Restore the missing-since times from disk.
        self._missing_since: Dict[str, float] = self._load(self._missing_path)
        # {vmid: epoch we rebooted it as the first recovery step}
        # Path and restored contents of the reboot-time state.
        self._reboot_path = Path(data_dir) / _REBOOT_STATE_FILE
        self._rebooted: Dict[str, float] = self._load(self._reboot_path)
        # Background loop task (started by start()).
        self._task = None

    def start(self) -> None:
        """Start the background sweep loop if it is not already running."""
        # Already running: do nothing.
        if self._task is not None and not self._task.done():
            return
        # Start the loop on the current event loop.
        try:
            self._task = asyncio.create_task(self._loop())
        # No running loop (e.g. tests): skip.
        except RuntimeError:
            pass  # no current event loop

    def _load(self, path: Path) -> Dict[str, float]:
        """Read a ``{vmid: epoch}`` JSON file, returning {} if missing or invalid."""
        try:
            # Only read files that exist.
            if path.exists():
                d = json.loads(path.read_text())
                # Accept only a JSON object, converting keys to str and values to float.
                if isinstance(d, dict):
                    return {str(k): float(v) for k, v in d.items()}
        # Corrupt or unreadable state is ignored (start fresh).
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save(self) -> None:
        """Persist the per-VM reclone times."""
        try:
            # Write the reclone-time map as JSON; failures are only logged.
            self._path.write_text(json.dumps(self._triggered))
        except Exception as e:  # noqa: BLE001
            logger.debug("stale-client-reclone: state save failed: %s", e)

    def _save_missing(self) -> None:
        """Persist the per-VM missing-since times."""
        try:
            # Write the missing-since map as JSON; failures are only logged.
            self._missing_path.write_text(json.dumps(self._missing_since))
        except Exception as e:  # noqa: BLE001
            logger.debug("stale-client-reclone: missing-state save failed: %s", e)

    def _save_rebooted(self) -> None:
        """Persist the per-VM reboot times."""
        try:
            # Write the reboot-time map as JSON; failures are only logged.
            self._reboot_path.write_text(json.dumps(self._rebooted))
        except Exception as e:  # noqa: BLE001
            logger.debug("stale-client-reclone: reboot-state save failed: %s", e)

    async def _loop(self) -> None:
        """Run a sweep every _SCAN_INTERVAL_S seconds until cancelled."""
        # Repeat forever.
        while True:
            try:
                # Wait, then sweep.
                await asyncio.sleep(_SCAN_INTERVAL_S)
                await self._sweep()
            # Shutdown: exit quietly.
            except asyncio.CancelledError:
                return
            # Log any other error and keep looping.
            except Exception as e:  # noqa: BLE001 — a sweep must not kill the loop
                logger.warning("stale-client-reclone loop error: %s", e)

    async def _sweep(self) -> None:
        """Find running sim VMs whose clients are silent and reboot/reclone them."""
        # Get the control plane (agent links), client registry and Proxmox state.
        spoke = self.spoke
        cp = getattr(spoke, "control_plane", None)
        reg = getattr(spoke, "registry", None)
        deploy = getattr(spoke, "deploy", None)
        # All three are needed; otherwise do nothing this time.
        if cp is None or reg is None or deploy is None:
            return
        # Latest per-host VM lists and the connected agents.
        states = getattr(deploy, "proxmox_states", {}) or {}
        agents = getattr(cp, "connected_agents", {}) or {}
        # hostname -> agent_id, CONNECTED agents only — a reclone dispatched to
        # a disconnected agent just fails/times out; skip and let the next
        # sweep retry once it reconnects, rather than queuing/retry-storming.
        # Map hostname -> agent id for connected agents.
        by_host: Dict[str, str] = {}
        # Walk every connected agent.
        for aid, info in agents.items():
            hn = str((info or {}).get("hostname") or "").strip()
            # Skip agents without a hostname.
            if hn:
                by_host[hn] = aid
        # Build a lower-cased hostname -> client record lookup from the registry.
        clients: Dict[str, Any] = {}
        try:
            # Walk every client record.
            for c in (reg.get_all() or {}).values():
                h = str((c or {}).get("hostname") or "").strip().lower()
                # Skip records without a hostname.
                if h:
                    clients[h] = c
        # A registry error leaves the lookup partial or empty.
        except Exception:  # noqa: BLE001
            pass
        # One "now" for the whole sweep, and flags marking which state files need saving.
        now = time.time()
        dirty = False
        missing_dirty = False
        reboot_dirty = False

        # ── Pass 1: collect the running sim VMs on CONNECTED hosts. ──────────
        # (vid, name, hn, agent_id, last_seen) per VM; also count how many have a
        # client that HAS reported (ls>0) — proof the VM-name↔client-hostname
        # correlation is working, which gates the never-reported reclone below.
        # Running sim VMs to evaluate, and how many have a reporting client.
        candidates = []
        reporting_count = 0
        # Walk each Proxmox host's VM list.
        for hn, st in states.items():
            # Skip hosts whose agent is not connected.
            agent_id = by_host.get(hn)
            if not agent_id:
                continue
            # Walk the host's VMs.
            for vm in (st.get("vms") or []):
                # Templates and containers are never sim clients.
                if vm.get("is_template") or vm.get("type") == "lxc":
                    continue
                # Parse the VM id; skip malformed entries.
                try:
                    vid = int(vm.get("vmid") or 0)
                except (TypeError, ValueError):
                    continue
                # Skip infrastructure VMs below the sim floor.
                if vid <= _VMID_FLOOR:
                    continue
                # Only running VMs matter.
                if str(vm.get("status") or "").lower() != "running":
                    continue
                # Find the client whose hostname matches the VM name.
                name = str(vm.get("name") or "").strip().lower()
                cl = clients.get(name)
                # Last time that client reported (0 if never or invalid).
                try:
                    ls = float((cl or {}).get("last_seen") or 0)
                except (TypeError, ValueError):
                    ls = 0.0
                # Count VMs whose client has reported at least once.
                if ls > 0:
                    reporting_count += 1
                # Keep this VM as a candidate.
                candidates.append((vid, name, hn, agent_id, ls))

        # VM ids seen running this sweep (used to prune stale state below).
        seen_vids = {str(v[0]) for v in candidates}

        async def _reclone(vid, name, hn, agent_id, reason):
            """Mark the VM as recloned and ask its agent to reclone it."""
            # We update the shared "reclone state changed" flag.
            nonlocal dirty
            # The cooldown starts even if the dispatch fails, so a host that
            # keeps rejecting it is retried every cooldown, not every sweep.
            # Record the reclone time first (see note above).
            self._triggered[str(vid)] = now
            dirty = True
            # Log the reason.
            logger.warning("stale-client-reclone: VM %s (%s) on %s %s — reclone",
                           vid, name, hn, reason)
            # Send the reclone command (60 s limit).
            try:
                reply = await cp.send_to_agent(
                    "CS_COMMAND", reclone_command(vid),
                    agent_id=agent_id, timeout=60.0)
            # A failed dispatch is logged; the cooldown still applies.
            except Exception as e:  # noqa: BLE001 — one failed dispatch must not
                                     # stop the sweep covering other VMs
                logger.warning("stale-client-reclone: reclone dispatch for "
                               "VM %s failed: %s", vid, e)
                return
            # Log an error if the agent did not accept the request.
            status = reply_status(reply)
            if status not in _ACCEPTED_STATUSES:
                logger.error("stale-client-reclone: agent %s rejected reclone of "
                             "VM %s (%s): %s", agent_id, vid, status or "no status",
                             reply)

        async def _reboot(vid, name, hn, agent_id, reason):
            """Mark the VM as rebooted and ask its agent to reboot it."""
            # We update the shared "reboot state changed" flag.
            nonlocal reboot_dirty
            # Record the reboot time.
            self._rebooted[str(vid)] = now
            reboot_dirty = True
            # Log the reason and that a reclone follows if it stays silent.
            logger.warning("stale-client-reclone: VM %s (%s) on %s %s — rebooting "
                           "first (reclone if still silent in %.0fs)",
                           vid, name, hn, reason, _REBOOT_WAIT_S)
            # Send the reboot command (60 s limit).
            try:
                reply = await cp.send_to_agent(
                    "CS_COMMAND", reboot_command(vid),
                    agent_id=agent_id, timeout=60.0)
            # A failed dispatch is logged and we move on.
            except Exception as e:  # noqa: BLE001
                logger.warning("stale-client-reclone: reboot dispatch for "
                               "VM %s failed: %s", vid, e)
                return
            # Log an error if the reboot did not succeed.
            status = reply_status(reply)
            if status != "SUCCESS":
                logger.error("stale-client-reclone: agent %s failed to reboot "
                             "VM %s (%s): %s", agent_id, vid, status or "no status",
                             reply)

        async def _recover(vid, name, hn, agent_id, reason):
            """Reboot first; reclone only if the client is still silent
            _REBOOT_WAIT_S after the reboot."""
            # We update the shared "reboot state changed" flag.
            nonlocal reboot_dirty
            # When (if ever) this VM was rebooted by us.
            rebooted_at = self._rebooted.get(str(vid))
            # Not rebooted yet: reboot it now and stop.
            if rebooted_at is None:
                await _reboot(vid, name, hn, agent_id, reason)
                return
            # Rebooted recently: wait for the reboot window to pass.
            if now - rebooted_at < _REBOOT_WAIT_S:
                return                             # give the reboot its window
            # The reboot did not help: clear its mark and reclone.
            self._rebooted.pop(str(vid), None)
            reboot_dirty = True
            await _reclone(vid, name, hn, agent_id,
                           reason + " and still silent %.0fs after a reboot"
                           % (now - rebooted_at))

        # ── Pass 2a: clients that REPORTED then went silent (existing path). ─
        # VMs whose client has never reported; handled in pass 2b.
        never_reported = []
        # Check each candidate VM.
        for vid, name, hn, agent_id, ls in candidates:
            # State maps use string VM ids.
            key = str(vid)
            # Never reported: defer to pass 2b.
            if ls <= 0:
                never_reported.append((vid, name, hn, agent_id))
                continue
            # Reported at least once → not a "never checked in" case; clear any
            # missing-clock it may have carried before its first heartbeat.
            # Clear any missing-since time from before it first reported.
            if key in self._missing_since:
                self._missing_since.pop(key, None)
                missing_dirty = True
            # How long the client has been silent.
            age = now - ls
            # Recently heard from: healthy.
            if age < _STALE_RECLONE_S:
                # If it had been rebooted, the reboot worked; clear the mark.
                if self._rebooted.pop(key, None) is not None:   # recovered
                    reboot_dirty = True
                continue
            # Skip VMs still in their reclone cooldown.
            if (now - self._triggered.get(key, 0.0)) < _RECLONE_COOLDOWN_S:
                continue
            # Silent too long: start recovery (reboot first).
            await _recover(vid, name, hn, agent_id,
                           "running but client silent %.0fs (> %.0fs)"
                           % (age, _STALE_RECLONE_S))

        # ── Pass 2b: VMs whose client NEVER checked in (the VM/client gap). ──
        # Correlation gate: only act when SOME VM's client is reporting, so a
        # fleet-wide telemetry glitch (every match fails at once) can't wipe the
        # pool. Each dead VM must persist running-yet-silent for the grace window
        # (tracked per-vmid, survives restart) before we assume it's dead.
        # Only if there are never-reported VMs and at least one other client is reporting.
        if never_reported and reporting_count > 0:
            # Number recovered this sweep.
            fired = 0
            # Check each never-reported VM.
            for vid, name, hn, agent_id in never_reported:
                # State maps use string VM ids.
                key = str(vid)
                # When we first saw it running and silent.
                first = self._missing_since.get(key)
                # First sighting: start its clock and act later.
                if first is None:
                    self._missing_since[key] = now
                    missing_dirty = True
                    continue                       # start the clock; act later
                # Still within the grace window.
                if (now - first) < _NEVER_REPORTED_GRACE_S:
                    continue
                # Still within the reclone cooldown.
                if (now - self._triggered.get(key, 0.0)) < _RECLONE_COOLDOWN_S:
                    continue
                # Still waiting out a reboot.
                if now - self._rebooted.get(key, 0.0) < _REBOOT_WAIT_S:
                    continue                       # waiting out its reboot
                # Reached the per-sweep cap.
                if fired >= _NEVER_REPORTED_MAX_PER_SWEEP:
                    break                          # bleed off slowly, never storm
                # Count it and start recovery.
                fired += 1
                await _recover(vid, name, hn, agent_id,
                               "running but client NEVER checked in for %.0fs (> %.0fs)"
                               % (now - first, _NEVER_REPORTED_GRACE_S))

        # Prune missing-clocks for VMs no longer present (deleted/recloned/stopped)
        # so the map can't grow without bound.
        # Remove missing-since entries for VMs no longer running.
        for gone in [k for k in self._missing_since if k not in seen_vids]:
            self._missing_since.pop(gone, None)
            missing_dirty = True

        # Drop reboot marks for VMs no longer present, once their wait is over
        # (a VM mid-reboot can briefly drop out of the running list).
        # Remove reboot marks for VMs no longer running whose wait has passed.
        for gone in [k for k, ts in self._rebooted.items()
                     if k not in seen_vids and now - ts > _REBOOT_WAIT_S]:
            self._rebooted.pop(gone, None)
            reboot_dirty = True

        # Save whichever state files changed.
        if dirty:
            self._save()
        if missing_dirty:
            self._save_missing()
        if reboot_dirty:
            self._save_rebooted()
