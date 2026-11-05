"""Clients command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_clients`` looks the command up in
``_CLIENT_COMMANDS`` (one ``_cmd_*`` method per command) and returns the result
dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Standard-library logging for handler messages.
import logging
# Type hints only.
from typing import Any, Dict, Optional

# Helpers that hide secret override values in replies and drop masked placeholders on save.
from sim_config import mask_secret_overrides, strip_masked_secrets

# Share the spoke's logger so these messages appear alongside the rest of the spoke.
logger = logging.getLogger("CSSpoke")


class ClientCommandsMixin:
    """Mixin handling client configuration overrides and purge operations."""

    # Map of command name -> handler method name for this domain.
    _CLIENT_COMMANDS = {
        "CS_DOT1X_CREDENTIAL": "_cmd_dot1x_credential",
        "CS_GET_CLIENT_OVERRIDES": "_cmd_get_client_overrides",
        "CS_SET_CLIENT_OVERRIDES": "_cmd_set_client_overrides",
        "CS_CLEAR_CLIENT_OVERRIDES": "_cmd_clear_client_overrides",
        "CS_SET_ALL_CLIENT_OVERRIDES": "_cmd_set_all_client_overrides",
        "CS_CLEAR_ALL_CLIENT_OVERRIDES": "_cmd_clear_all_client_overrides",
        "CS_PURGE_CLIENTS": "_cmd_purge_clients",
        "CS_DELETE_CLIENT": "_cmd_delete_client",
        "CS_PURGE_HOST": "_cmd_purge_host",
        "CS_GET_HOST_USB_OVERRIDES": "_cmd_get_host_usb_overrides",
        "CS_SET_HOST_USB_OVERRIDE": "_cmd_set_host_usb_override",
        "CS_CLEAR_HOST_USB_OVERRIDE": "_cmd_clear_host_usb_override",
    }

    async def _dispatch_clients(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the handler for ``cmd`` from ``_CLIENT_COMMANDS``; None if it is not one of ours."""
        # Look up which method handles this command.
        handler = self._CLIENT_COMMANDS.get(cmd)
        # Not a client command: return None so the next domain mixin can try it.
        if handler is None:
            return None
        # Call the handler method on the spoke and return its reply.
        return await getattr(self, handler)(d)

    # ── per-client override control panel (hub/UI → registry overrides) ──
    # The legacy cs webui-spoke exposed a per-client "Control Panel" with
    # live sim-flag toggles (kill_switch/dns_fail/iperf/download/www_traffic/
    # ping_test/ssidpw_fail/auth_fail/dhcp_fail/port_flap/assoc_fail) +
    # Apply / Clear / Apply-to-ALL. The hub forwards each action here as a
    # CS_* command; these handlers wrap ClientRegistry.set_overrides /
    # clear_overrides (the SAME persisted store the /api/config delivery
    # reads, unlike the ephemeral demo flags). GET + CLEAR sit before the
    # NOT_IMPLEMENTED matcher (both second-segments are in its set); SET is
    # not in the set but would fall through to "Unknown command", so it gets
    # an explicit handler too.
    # ── JIT 802.1X credential (hub broker → engine-owned dot1x_password) ──
    async def _cmd_dot1x_credential(self, d):
        """Return a just-in-time 802.1X credential from the sim quota engine."""
        # The quota engine owns 802.1X passwords; it may not be running.
        eng = getattr(self, "sim_quota_engine", None)
        if eng is None:
            return {"status": "ERROR", "message": "sim quota engine not running"}
        # Let the engine issue/return the credential for this request.
        return await eng.dot1x_credential(d)

    async def _cmd_get_client_overrides(self, d):
        """Return one client's persisted sim overrides (secrets masked)."""
        # Read and validate the target client name.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Fetch the client's registry entry (empty if unknown).
        entry = self.registry.get(hostname) or {}
        # Return its overrides with secret values masked.
        return {"status": "SUCCESS", "hostname": hostname,
                "overrides": mask_secret_overrides(entry.get("overrides", {}))}

    async def _cmd_set_client_overrides(self, d):
        """Replace one client's persisted sim overrides."""
        # Read the target client and the requested overrides.
        hostname = str(d.get("hostname") or "").strip()
        overrides = d.get("overrides") or {}
        # Validate both inputs.
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        if not isinstance(overrides, dict):
            return {"status": "ERROR", "message": "'overrides' must be an object"}
        # Save the overrides, ignoring masked secret placeholders so real secrets are kept.
        entry = await self.registry.set_overrides(hostname, strip_masked_secrets(overrides))
        # Notify the client so it re-fetches its config.
        await self._on_client_override_changed(hostname)
        # Return the stored overrides with secrets masked.
        return {"status": "SUCCESS", "hostname": hostname,
                "overrides": mask_secret_overrides(entry.get("overrides", {}))}

    async def _cmd_clear_client_overrides(self, d):
        """Clear one client's persisted sim overrides."""
        # Read and validate the target client name.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Remove all of this client's persisted overrides.
        await self.registry.clear_overrides(hostname)
        # A cleared JIT dot1x_password is re-delivered on the next sweep.
        # Ask the quota engine (if it supports it) to re-send the 802.1X password.
        eng = getattr(self, "sim_quota_engine", None)
        if eng is not None and hasattr(eng, "dot1x_mark_undelivered"):
            eng.dot1x_mark_undelivered(hostname)
        # Notify the client so it re-fetches its config.
        await self._on_client_override_changed(hostname)
        return {"status": "SUCCESS", "hostname": hostname, "cleared": True}

    async def _cmd_set_all_client_overrides(self, d):
        """Apply the same overrides to every registered client."""
        # Read and validate the overrides to apply everywhere.
        overrides = d.get("overrides") or {}
        if not isinstance(overrides, dict):
            return {"status": "ERROR", "message": "'overrides' must be an object"}
        # Count clients updated; strip masked secret placeholders once up front.
        applied = 0
        overrides = strip_masked_secrets(overrides)
        # Apply a separate copy of the overrides to every registered client.
        for hostname in list(self.registry.get_all().keys()):
            await self.registry.set_overrides(hostname, dict(overrides))
            applied += 1
        # Every registered client's served [username] changed → re-fetch all.
        # Tell every client to re-fetch its config.
        await self._push_config_refresh_to_clients()
        # Report the count and the applied overrides with secrets masked.
        return {"status": "SUCCESS", "applied": applied,
                "overrides": mask_secret_overrides(overrides)}

    async def _cmd_clear_all_client_overrides(self, d):
        """Clear registry overrides and demo scenarios for every client."""
        # Bulk-clear EVERY override layer /api/config bakes into the served
        # [username] section, not just the registry:
        #   * the legacy per-client REGISTRY override layer (client_api.py
        #     :304-313) — stale flags persisted in clients.json, invisible
        #     in the User Overrides card (which reads user-overrides.conf);
        #   * the ephemeral DEMO scenario layer (client_api.py :243-245) — a
        #     demo triggered from the Clients tab "Demo" column injects the
        #     FAILURE_FLAGS (dns_fail/ssidpw_fail/…) the user was seeing and
        #     survives a registry-only clear (the 120-min TTL otherwise).
        # Then enqueue update_now to every client so each re-fetches the
        # now-clean config: update.sh runs ONLY on update_now / a VERSION
        # bump (the 1-min watchdog runs sys_mon, not update.sh), so without
        # this the client keeps its stale local simulation.conf with the old
        # [username] section — the "still there after Clear All" symptom.
        # Count of registry entries cleared; the quota engine may be absent.
        cleared = 0
        eng = getattr(self, "sim_quota_engine", None)
        # Clear every client's persisted overrides.
        for hostname in list(self.registry.get_all().keys()):
            await self.registry.clear_overrides(hostname)
            # Ask the engine to re-send each client's 802.1X password.
            if eng is not None and hasattr(eng, "dot1x_mark_undelivered"):
                eng.dot1x_mark_undelivered(hostname)
            cleared += 1
        # Now clear any active demo scenarios, counting them first for the reply.
        demos_cleared = 0
        demo = getattr(self, "demo", None)
        # Only if the demo manager exists; a failure here is logged but not fatal.
        if demo is not None:
            try:
                demos_cleared = len(await demo.active_summary())
                demo.clear_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("CS_CLEAR_ALL_CLIENT_OVERRIDES: demo clear failed: %s", exc)
        # Record what was cleared.
        logger.info("CS_CLEAR_ALL_CLIENT_OVERRIDES: cleared %d registry override(s) + %d demo(s)",
                     cleared, demos_cleared)
        # Tell every client to re-fetch its now-clean config.
        await self._push_config_refresh_to_clients()
        # Report both counts.
        return {"status": "SUCCESS", "cleared": cleared,
                "demos_cleared": demos_cleared}

    async def _cmd_purge_clients(self, d):
        """Remove every registered client (irreversible)."""
        # The "Purge Clients" button (original cs-webui
        # DELETE /api/clients/history): drop every registered client from
        # memory + delete clients.json on disk — irreversible. The hub/UI
        # forwards here via DELETE /sim/api/{tenant}/clients. Returns the
        # count removed so the UI can confirm. Sits before the
        # NOT_IMPLEMENTED matcher below (second-segment "PURGE" isn't in
        # its set, but without this handler it would fall through to
        # "Unknown command").
        # Delete every client record and return what the registry reports.
        res = await self.registry.purge()
        return {"status": "SUCCESS", **res}

    async def _cmd_delete_client(self, d):
        """Remove one client's registry record."""
        # Surgical counterpart to CS_PURGE_CLIENTS — removes ONE client's
        # registry record (a renamed/recloned VM's stale old hostname, a
        # one-off test client, etc.) instead of wiping the entire fleet's
        # history. Hub forwards here via
        # DELETE /sim/api/{tenant}/clients/{hostname}.
        # Read and validate the client name.
        hostname = (d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing hostname"}
        # Remove that one record and report whether it existed.
        removed = await self.registry.delete_one(hostname)
        return {"status": "SUCCESS", "hostname": hostname, "removed": removed}

    async def _cmd_purge_host(self, d):
        """Forget a Proxmox host and its site assignment."""
        # Operator deleted a VM Server row for an intentionally shut-down
        # host (hub DELETE /sim/api/proxmox/host/{hostname} forwards here).
        # Drop it from proxmox_states so it stops being relayed.
        # Read and validate the Proxmox host name.
        hostname = (d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing hostname"}
        # Stop tracking the host's telemetry.
        removed = self.deploy.remove_host(hostname)
        # Also forget its Config → Sites server-to-site assignment, so a
        # server that left this tenant (moved to another tenant's spoke)
        # stops showing as an offline row there.
        # Track whether a site assignment was removed.
        unmapped = False
        # Remove the host from the server-to-site map if it is there and save the map.
        try:
            _m = self.local_store.get_pxmx_site_map()
            if hostname in _m:
                _m.pop(hostname, None)
                self.local_store.set_pxmx_site_map(_m)
                unmapped = True
        # Site-map cleanup is best effort; the purge still succeeds.
        except Exception as exc:  # noqa: BLE001 — purge must still succeed
            logger.warning("CS_PURGE_HOST: site-map cleanup failed for %s: %s", hostname, exc)
        # Report what was removed.
        return {"status": "SUCCESS", "hostname": hostname,
                "removed": removed, "unmapped": unmapped}

    # ── Per-host USB VMID overrides ──────────────────────────────────────
    # Optional per-host vmid_start/vmid_end/vm_set_override that override the
    # global range for one proxmox host (the pxmx agent honors a non-default
    # range over its own hostname-suffix batch derivation). Persisted by
    # CSSettings in cs_settings.json under ``host_usb_overrides``.
    async def _cmd_get_host_usb_overrides(self, d):
        """Return all per-host USB VMID overrides."""
        # Return every per-host USB override from settings.
        return {"status": "SUCCESS",
                "overrides": self.settings.all_host_usb_overrides()}

    async def _cmd_set_host_usb_override(self, d):
        """Set per-host USB VMID override settings for one Proxmox host."""
        # Read and validate the Proxmox host name.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Accept the settings under "knobs" or the older "overrides" key.
        knobs = d.get("knobs") or d.get("overrides") or {}
        # The settings must be a JSON object.
        if not isinstance(knobs, dict):
            return {"status": "ERROR", "message": "'knobs' must be an object"}
        # Merge them into the stored overrides and return the result.
        merged = self.settings.set_host_usb_override(hostname, knobs)
        return {"status": "SUCCESS", "hostname": hostname, "knobs": merged}

    async def _cmd_clear_host_usb_override(self, d):
        """Remove the USB VMID overrides for one Proxmox host."""
        # Read and validate the Proxmox host name.
        hostname = str(d.get("hostname") or "").strip()
        if not hostname:
            return {"status": "ERROR", "message": "missing 'hostname'"}
        # Remove that host's overrides and report whether any existed.
        cleared = self.settings.clear_host_usb_override(hostname)
        return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}
