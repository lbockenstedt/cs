"""Clients command handlers for the cs spoke.

Extracted verbatim from ``cs_spoke.py``'s ~900-line ``handle_command`` if-chain
(pure structural move, no behavior change). ``CSSpoke`` inherits this mixin, so
every handler runs against the real spoke ``self`` and the CS_* dispatch
contract is unchanged. ``_dispatch_clients`` scans only its own command group and
returns the result dict, or ``None`` when the command is not one of its own
(``handle_command`` then tries the next domain — command sets are disjoint).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("CSSpoke")


class ClientCommandsMixin:
    async def _dispatch_clients(self, cmd: str, d: Dict[str, Any]) -> Optional[Dict[str, Any]]:

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
        if cmd in ("CS_GET_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            entry = self.registry.get(hostname) or {}
            return {"status": "SUCCESS", "hostname": hostname,
                    "overrides": entry.get("overrides", {})}

        if cmd in ("CS_SET_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            overrides = d.get("overrides") or {}
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            if not isinstance(overrides, dict):
                return {"status": "ERROR", "message": "'overrides' must be an object"}
            entry = await self.registry.set_overrides(hostname, overrides)
            await self._on_client_override_changed(hostname)
            return {"status": "SUCCESS", "hostname": hostname,
                    "overrides": entry.get("overrides", {})}

        if cmd in ("CS_CLEAR_CLIENT_OVERRIDES",):
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            await self.registry.clear_overrides(hostname)
            await self._on_client_override_changed(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": True}

        if cmd in ("CS_SET_ALL_CLIENT_OVERRIDES",):
            overrides = d.get("overrides") or {}
            if not isinstance(overrides, dict):
                return {"status": "ERROR", "message": "'overrides' must be an object"}
            applied = 0
            for hostname in list(self.registry.get_all().keys()):
                await self.registry.set_overrides(hostname, dict(overrides))
                applied += 1
            # Every registered client's served [username] changed → re-fetch all.
            await self._push_config_refresh_to_clients()
            return {"status": "SUCCESS", "applied": applied,
                    "overrides": dict(overrides)}

        if cmd in ("CS_CLEAR_ALL_CLIENT_OVERRIDES",):
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
            cleared = 0
            for hostname in list(self.registry.get_all().keys()):
                await self.registry.clear_overrides(hostname)
                cleared += 1
            demos_cleared = 0
            demo = getattr(self, "demo", None)
            if demo is not None:
                try:
                    demos_cleared = len(await demo.active_summary())
                    demo.clear_all()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CS_CLEAR_ALL_CLIENT_OVERRIDES: demo clear failed: %s", exc)
            logger.info("CS_CLEAR_ALL_CLIENT_OVERRIDES: cleared %d registry override(s) + %d demo(s)",
                         cleared, demos_cleared)
            await self._push_config_refresh_to_clients()
            return {"status": "SUCCESS", "cleared": cleared,
                    "demos_cleared": demos_cleared}

        if cmd in ("CS_PURGE_CLIENTS",):
            # The "Purge Clients" button (original cs-webui
            # DELETE /api/clients/history): drop every registered client from
            # memory + delete clients.json on disk — irreversible. The hub/UI
            # forwards here via DELETE /sim/api/{tenant}/clients. Returns the
            # count removed so the UI can confirm. Sits before the
            # NOT_IMPLEMENTED matcher below (second-segment "PURGE" isn't in
            # its set, but without this handler it would fall through to
            # "Unknown command").
            res = await self.registry.purge()
            return {"status": "SUCCESS", **res}

        if cmd == "CS_PURGE_HOST":
            # Operator deleted a VM Server row for an intentionally shut-down
            # host (hub DELETE /sim/api/proxmox/host/{hostname} forwards here).
            # Drop it from proxmox_states so it stops being relayed.
            hostname = (d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing hostname"}
            removed = self.deploy.remove_host(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "removed": removed}

        # ── Per-host USB VMID overrides ──────────────────────────────────────
        # Optional per-host vmid_start/vmid_end/vm_set_override that override the
        # global range for one proxmox host (the pxmx agent honors a non-default
        # range over its own hostname-suffix batch derivation). Persisted by
        # CSSettings in cs_settings.json under ``host_usb_overrides``.
        if cmd == "CS_GET_HOST_USB_OVERRIDES":
            return {"status": "SUCCESS",
                    "overrides": self.settings.all_host_usb_overrides()}

        if cmd == "CS_SET_HOST_USB_OVERRIDE":
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            knobs = d.get("knobs") or d.get("overrides") or {}
            if not isinstance(knobs, dict):
                return {"status": "ERROR", "message": "'knobs' must be an object"}
            merged = self.settings.set_host_usb_override(hostname, knobs)
            return {"status": "SUCCESS", "hostname": hostname, "knobs": merged}

        if cmd == "CS_CLEAR_HOST_USB_OVERRIDE":
            hostname = str(d.get("hostname") or "").strip()
            if not hostname:
                return {"status": "ERROR", "message": "missing 'hostname'"}
            cleared = self.settings.clear_host_usb_override(hostname)
            return {"status": "SUCCESS", "hostname": hostname, "cleared": cleared}
        return None
