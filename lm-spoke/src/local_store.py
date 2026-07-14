"""JSON-backed local config store for a standalone/hub-connected cs spoke's
own Setup-tab knobs: hub_config (auto-provisioning thresholds/templates/VMID
range — the settings the LM hub would otherwise own per-tenant) and
central_config/central_sites_config (Aruba Central API credentials + the
sites to poll).

Adapted from lm/core/src/simulations/store.py's SimulationsStore — same
defaults, same get/set/reset semantics — but single-tenant: this spoke IS
the tenant, so every method drops the tenant_id parameter the hub version
threads through per-tenant. Re-sync _DEFAULT_HUB_CONFIG from there if the
hub's knob set changes.

Persisted to ``data/local_store.json`` next to client_registry.py's
``data/clients.json`` (same runtime-state directory, gitignored).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("LocalStore")

# Setup/Proxmox hub-config list fields. The local Setup UI (sim-views.js)
# collects these as comma/space-delimited text (no raw JSON); the spoke
# normalizes a delimited string into a list here, at the storage boundary, so
# downstream _parse_json_list / _apply_hub_config read a real list. Already-list
# or already-JSON values pass through. usb_vidpids is a list of {vidpid,type,
# label}; type/label are preserved from the currently-stored entry when the same
# vidpid already exists, so editing the field as a plain vidpid list does NOT
# discard metadata set via another UI. Mirrors normalize_hub_config_lists in
# lm/core/src/simulations/routes.py — keep the two in sync.
_HUB_CONFIG_LIST_KEYS = (
    "usb_ignored_vidpids",   # list of "vid:pid"
    "t1_pci_vidpids",        # list of "vid:pid"
    "t3_pci_vidpids",        # list of "vid:pid"
    "ignored_hostnames",     # list of str
)
_HUB_CONFIG_VIDPID_OBJ_KEY = "usb_vidpids"  # list of {vidpid,type,label}
_USB_VIDPID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")


def _split_delim(s: str) -> list:
    return [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]


def _coerce_to_list(raw: Any) -> list:
    """Best-effort raw → list: an already-parsed list, a JSON array string, or a
    delimited string. Returns [] for empty/None."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            pass
    return _split_delim(s)


def _hub_config_list_value(key: str, raw: Any, stored_raw: Any = None) -> list:
    """Normalize one Setup/Proxmox list field for storage. vidpid string-list
    keys → deduped list of lowercased ``vid:pid`` (invalid tokens dropped);
    ``ignored_hostnames`` → deduped list of non-empty strings (order kept);
    ``usb_vidpids`` → deduped list of ``{vidpid,type,label}`` dicts, reusing the
    stored entry's type/label for an existing vidpid (else type ``"wireless"``,
    label = vidpid) — or the input object's own type/label when it's already a
    dict."""
    if key == _HUB_CONFIG_VIDPID_OBJ_KEY:
        items = _coerce_to_list(raw)
        prev: Dict[str, Dict[str, str]] = {}
        for it in _coerce_to_list(stored_raw):
            if isinstance(it, dict) and it.get("vidpid"):
                vp = str(it["vidpid"]).strip().lower()
                if _USB_VIDPID_RE.match(vp):
                    prev[vp] = {"type": str(it.get("type") or "wireless"),
                                "label": str(it.get("label") or vp)}
        out, seen = [], set()
        for it in items:
            vp = str(it.get("vidpid", "") if isinstance(it, dict) else it).strip().lower()
            if not _USB_VIDPID_RE.match(vp) or vp in seen:
                continue
            seen.add(vp)
            if isinstance(it, dict):
                out.append({"vidpid": vp,
                            "type": str(it.get("type") or "wireless"),
                            "label": str(it.get("label") or vp)})
            else:
                p = prev.get(vp)
                out.append({"vidpid": vp,
                            "type": p["type"] if p else "wireless",
                            "label": p["label"] if p else vp})
        return out
    items = _coerce_to_list(raw)
    if key == "ignored_hostnames":
        out, seen = [], set()
        for it in items:
            s = str(it).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    out, seen = [], set()
    for it in items:
        vp = str(it.get("vidpid", "") if isinstance(it, dict) else it).strip().lower()
        if _USB_VIDPID_RE.match(vp) and vp not in seen:
            seen.add(vp)
            out.append(vp)
    return out


def normalize_hub_config_lists(hc: Any, stored_hc: Any = None) -> dict:
    """Return a copy of ``hc`` with the Setup/Proxmox list fields normalized to
    lists. Fields not present are left untouched. Called by
    ``LocalStore.set_hub_config`` so the local Setup UI may send comma/space-
    delimited strings instead of raw JSON."""
    if not isinstance(hc, dict):
        return hc
    out = dict(hc)
    stored_hc = stored_hc or {}
    for k in _HUB_CONFIG_LIST_KEYS:
        if k in out:
            out[k] = _hub_config_list_value(k, out[k], stored_hc.get(k))
    if _HUB_CONFIG_VIDPID_OBJ_KEY in out:
        out[_HUB_CONFIG_VIDPID_OBJ_KEY] = _hub_config_list_value(
            _HUB_CONFIG_VIDPID_OBJ_KEY, out[_HUB_CONFIG_VIDPID_OBJ_KEY],
            stored_hc.get(_HUB_CONFIG_VIDPID_OBJ_KEY))
    return out

# Mirror of lm/core/src/simulations/store.py's _DEFAULT_HUB_CONFIG. Keep in
# sync if the hub's knob set changes — this is what seeds a fresh standalone
# deployment's Setup/Proxmox card instead of showing a blank grid.
_DEFAULT_HUB_CONFIG: Dict[str, Any] = {
    # Provisioning Behavior
    "usb_auto_provision": "off",
    "usb_missing_timeout": 60,            # minutes (cs spoke ×60 → seconds)
    "usb_max_slots": 24,
    # Resource Thresholds (% — 1-hour average)
    "cpu_provision_threshold": 80,
    "cpu_delete_threshold": 90,
    "mem_provision_threshold": 80,
    "mem_delete_threshold": 90,
    # Tier classification by PCI passthrough (T1/T3 are PCI, T2 is USB). A VM
    # whose hostpciN device matches one of these VID:PIDs is that tier. Defaults
    # match the solutions-hpe originals; edited in the Hub Config card. NOT in
    # preserve-on-reset: reset restores these canonical tier IDs.
    "t1_pci_vidpids": ["1912:0015"],
    "t3_pci_vidpids": ["168c:0034"],
    # VM Templates (clone-source VMIDs + image1 mix)
    "vm_image_1_template_id": 100,
    "vm_image_2_template_id": 200,
    "vm_image_1_pct": 50,
    # Parallel Provisioning
    "reclone_concurrency": 1,
    # VMID allocation range for new sim VMs (templates excluded by the agent)
    "vmid_start": 90000,
    "vmid_end": 99999,
    # Remaining hub-owned knobs (the Hub Config card)
    "use_all_dongles": "off",
    "vm_silent_timeout": 24,
    "l1_vlan_start": 100,
    "l1_vlan_end": 199,
    "reclone_schedule_enabled": "off",
    # Guest-agent watchdog group
    "guest_agent_watchdog_enabled": "on",
    "guest_agent_grace_minutes": 20,
    "guest_agent_check_interval_minutes": 10,
    "guest_agent_reboot_after_minutes": 10,
    "guest_agent_reclone_after_minutes": 30,
    "watchdog_reboot_enabled": "on",
}

# JSON-list fields holding real certified/ignored data — preserved across a
# "reset to default" (resetting the knobs must not de-certify dongles).
_HUB_CONFIG_PRESERVE_ON_RESET = (
    "usb_vidpids", "usb_ignored_vidpids", "ignored_hostnames",
)


class LocalStore:
    """Single-tenant JSON config store for the cs spoke's own Setup knobs
    (``data/local_store.json``). See the module docstring for the shape."""

    def __init__(self, data_dir: os.PathLike | str) -> None:
        self._path = Path(data_dir) / "local_store.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f) or {}
        except FileNotFoundError:
            self._data = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("LocalStore: load failed (%s): %s — starting empty",
                           self._path, exc)
            self._data = {}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LocalStore: save failed (%s): %s", self._path, exc)

    # ── hub-config (usb provisioning / vm images / reclone knobs) ──────────

    def get_hub_config(self) -> Dict[str, Any]:
        stored = self._data.get("hub_config") or {}
        merged = dict(_DEFAULT_HUB_CONFIG)
        merged.update(stored)
        return {"hub_config_enabled": bool(self._data.get("hub_config_enabled", False)),
                "hub_config": merged}

    def set_hub_config(self, enabled: bool, hub_config: Dict[str, Any]) -> None:
        """REPLACES the stored hub_config wholesale (not a merge).

        Callers that edit a subset (``csSaveHubConfig`` and
        ``csSaveAutoProvConfig`` in ``sim-views.js``) must GET-merge-PUT or
        the two Setup cards wipe each other's keys. ``enabled`` toggles
        ``hub_config_enabled`` independently of the knob dict.

        List fields (``usb_vidpids`` / ``usb_ignored_vidpids`` /
        ``t1_pci_vidpids`` / ``t3_pci_vidpids`` / ``ignored_hostnames``) are
        normalized from comma/space-delimited text into lists (see
        ``normalize_hub_config_lists``) — the Setup UI no longer requires raw
        JSON. ``usb_vidpids`` type/label are preserved from the prior stored
        entry for an existing vidpid."""
        with self._lock:
            stored_hc = self._data.get("hub_config") or {}
            self._data["hub_config_enabled"] = bool(enabled)
            self._data["hub_config"] = normalize_hub_config_lists(hub_config or {}, stored_hc)
            self._save()

    def reset_hub_config(self) -> Dict[str, Any]:
        with self._lock:
            stored = self._data.get("hub_config") or {}
            preserved = {k: stored.get(k, "[]") for k in _HUB_CONFIG_PRESERVE_ON_RESET}
            new_cfg = dict(_DEFAULT_HUB_CONFIG)
            new_cfg.update({"protected_vmids": "", "repo_branch": "",
                           "reclone_schedule_cron": ""})
            new_cfg.update(preserved)
            self._data["hub_config"] = new_cfg
            self._save()
            return {"hub_config_enabled": bool(self._data.get("hub_config_enabled", False)),
                    "hub_config": dict(new_cfg)}

    # ── central API config (mode + cluster creds) ──────────────────────────

    def get_central_config(self) -> Dict[str, Any]:
        return dict(self._data.get("central_config") or {})

    def set_central_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._data["central_config"] = cfg or {}
            self._save()

    # ── central sites config (which sites to poll + monitored checks) ──────

    def get_central_sites_config(self) -> Dict[str, Any]:
        return dict(self._data.get("central_sites_config") or {})

    def set_central_sites_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._data["central_sites_config"] = cfg or {}
            self._save()

    # ── pxmx server → site map (engine site resolver) ──────────────────────────
    # An operator assigns each connected pxmx server (the agent host = its short
    # hostname, the same key as connected_agents[agent_id]["hostname"] and
    # proxmox_states) to a site (e.g. "MIA"). The SimQuotaEngine resolves a
    # client's effective site via its hosting pxmx server's entry here (after a
    # per-client wsite override, before the bucket-default wsite fallback), so a
    # site-specific quota ("10 DNS-fail clients in MIA") is filled from clients
    # whose hosting server is in MIA. Persisted so a spoke restart keeps the
    # mapping until re-edited. Shape: {pxmx_host: site}.
    def get_pxmx_site_map(self) -> Dict[str, str]:
        return dict(self._data.get("pxmx_site_map") or {})

    def set_pxmx_site_map(self, mapping: Dict[str, Any]) -> Dict[str, str]:
        with self._lock:
            clean: Dict[str, str] = {}
            for host, site in (mapping or {}).items():
                h = str(host).strip()
                s = str(site or "").strip()
                if h and s:
                    clean[h] = s
            self._data["pxmx_site_map"] = clean
            self._save()
            return clean

    # ── effective sim quotas (hub-pushed, engine input) ──────────────────────
    # The hub merges platform-wide defaults + this tenant's overrides (enabled
    # only) and pushes the result as effective_sim_quotas; the spoke's
    # SimQuotaEngine reconciles client assignments against it. Persisted so a
    # spoke restart re-runs the engine against the last-pushed set until the hub
    # re-delivers (push_cs_hub_config on reconnect).
    def get_effective_sim_quotas(self) -> list:
        return list(self._data.get("effective_sim_quotas") or [])

    def set_effective_sim_quotas(self, quotas: list) -> None:
        with self._lock:
            self._data["effective_sim_quotas"] = list(quotas or [])
            self._save()

    # ── per-sim shareable/stackable overrides (hub-pushed, authoritative) ─────
    # {sim_id: bool}. A sim set False can NEVER be stacked by the SimQuotaEngine,
    # overriding the hardcoded SIM_META multi_capable default.
    def get_sim_shareable(self) -> dict:
        v = self._data.get("sim_shareable")
        return dict(v) if isinstance(v, dict) else {}

    def set_sim_shareable(self, mapping: dict) -> None:
        with self._lock:
            self._data["sim_shareable"] = dict(mapping) if isinstance(mapping, dict) else {}
            self._save()

    # ── pool / SSID config (hub-pushed) — see docs/simulation-pool-and-quota-design.md ──
    # site_source: "pxmx" (site from the hosting PXMX server — RF chamber, the
    # common case) or "assigned" (weighted logical assignment + site-based SSID).
    def get_site_source(self) -> str:
        v = str(self._data.get("site_source") or "pxmx").strip().lower()
        return v if v in ("pxmx", "assigned") else "pxmx"

    def set_site_source(self, mode: str) -> None:
        with self._lock:
            m = str(mode or "pxmx").strip().lower()
            self._data["site_source"] = m if m in ("pxmx", "assigned") else "pxmx"
            self._save()

    # randomizable_sims: the sim_ids the ambient pool may randomly run (traffic
    # sims). Failure/alert sims are harvest-only and NEVER appear here.
    def get_randomizable_sims(self) -> list:
        v = self._data.get("randomizable_sims")
        return list(v) if isinstance(v, list) else []

    def set_randomizable_sims(self, sims: list) -> None:
        with self._lock:
            self._data["randomizable_sims"] = [str(s) for s in sims] if isinstance(sims, list) else []
            self._save()

    # random_pool: {site: bool} — whether ambient clients at a site randomize.
    def get_random_pool(self) -> dict:
        v = self._data.get("random_pool")
        return dict(v) if isinstance(v, dict) else {}

    def set_random_pool(self, mapping: dict) -> None:
        with self._lock:
            self._data["random_pool"] = {str(k): bool(v) for k, v in mapping.items()} \
                if isinstance(mapping, dict) else {}
            self._save()

    # ambient_pct: in HUB mode (web_server=on) each randomizable sim flips on with
    # this probability (0-100) — the direct roll that replaces the s0-s9 bucket
    # combos. Default 50.
    def get_ambient_pct(self) -> int:
        try:
            v = int(self._data.get("ambient_pct", 50))
        except (TypeError, ValueError):
            v = 50
        return max(0, min(100, v))

    def set_ambient_pct(self, pct) -> None:
        with self._lock:
            try:
                self._data["ambient_pct"] = max(0, min(100, int(pct)))
            except (TypeError, ValueError):
                self._data["ambient_pct"] = 50
            self._save()

    # ambient_control: off (default) = automatic, every randomizable sim uses the
    # uniform ambient_pct. on = the operator is steering distribution and per-sim
    # weights in ambient_weights take over. Toggle is surfaced in the Quota UI.
    def get_ambient_control(self) -> bool:
        return bool(self._data.get("ambient_control", False))

    def set_ambient_control(self, on) -> None:
        with self._lock:
            self._data["ambient_control"] = bool(on)
            self._save()

    # ambient_weights: {sim_id: relative int, default 1}. Among the ambient-active
    # clients (see ambient_pct = the level), each runs ONE randomizable sim chosen
    # by weighted random pick — a sim weighted 3 runs on 3x the clients of one
    # weighted 1. Only consulted when ambient_control is on; a sim not listed
    # defaults to weight 1 (even split). Relative, so no upper clamp beyond sanity.
    def get_ambient_weights(self) -> dict:
        v = self._data.get("ambient_weights")
        if not isinstance(v, dict):
            return {}
        out = {}
        for k, w in v.items():
            try:
                out[str(k)] = max(0, min(100000, int(w)))
            except (TypeError, ValueError):
                continue
        return out

    def set_ambient_weights(self, mapping: dict) -> None:
        with self._lock:
            clean = {}
            if isinstance(mapping, dict):
                for k, w in mapping.items():
                    try:
                        clean[str(k)] = max(0, min(100000, int(w)))
                    except (TypeError, ValueError):
                        continue
            self._data["ambient_weights"] = clean
            self._save()

    # ambient_site_weights: {site: relative int, default 1} — a per-site load
    # weight. A site's ambient LEVEL is base_level × site_weight, so a site
    # weighted 3 has 3x the ambient-active clients of a site weighted 1 (default).
    # Only used when ambient_control is on; a site not listed stays weight 1. The
    # multiplier is folded into the served ambient_pct (level) at request time
    # (client_api), so the client never sees the site weight directly.
    def get_ambient_site_weights(self) -> dict:
        v = self._data.get("ambient_site_weights")
        if not isinstance(v, dict):
            return {}
        out = {}
        for k, w in v.items():
            try:
                out[str(k)] = max(0, int(w))
            except (TypeError, ValueError):
                continue
        return out

    def set_ambient_site_weights(self, mapping: dict) -> None:
        with self._lock:
            clean = {}
            if isinstance(mapping, dict):
                for k, w in mapping.items():
                    try:
                        clean[str(k)] = max(0, int(w))
                    except (TypeError, ValueError):
                        continue
            self._data["ambient_site_weights"] = clean
            self._save()

    # ssid_matrix: the defined cells [{site, auth, ssid, ssidpw, enabled, weight}].
    def get_ssid_matrix(self) -> list:
        v = self._data.get("ssid_matrix")
        return [dict(c) for c in v if isinstance(c, dict)] if isinstance(v, list) else []

    def set_ssid_matrix(self, cells: list) -> None:
        with self._lock:
            self._data["ssid_matrix"] = [dict(c) for c in cells if isinstance(c, dict)] \
                if isinstance(cells, list) else []
            self._save()

    # ssid_placement: per-site SSID hold targets + remainder policy, e.g.
    # {"MIA": {"targets": {"MIA-PSK": 20, "MIA-1X": 50}, "remainder": "MIA-1X"}}.
    def get_ssid_placement(self) -> dict:
        v = self._data.get("ssid_placement")
        return dict(v) if isinstance(v, dict) else {}

    def set_ssid_placement(self, mapping: dict) -> None:
        with self._lock:
            self._data["ssid_placement"] = dict(mapping) if isinstance(mapping, dict) else {}
            self._save()

    # ssid_weights: weighted random-spread rules for the SPARE (unaccounted)
    # pool — a list of {site, ssid, weight, all}. weight 0 = that cell takes
    # none; ``all`` = the cell soaks the balance. Replaces the deprecated
    # ssid_placement hold-N/remainder model; the engine's _reconcile_weighted
    # consumes it.
    def get_ssid_weights(self) -> list:
        v = self._data.get("ssid_weights")
        return [dict(r) for r in v if isinstance(r, dict)] if isinstance(v, list) else []

    def set_ssid_weights(self, rules: list) -> None:
        with self._lock:
            self._data["ssid_weights"] = [dict(r) for r in rules if isinstance(r, dict)] \
                if isinstance(rules, list) else []
            self._save()

    # ignored_hostnames: clients excluded from the quota engine (pool, ledger,
    # counts). Pushed via _pool_config so it reaches the spoke regardless of the
    # hub-source-of-truth toggle (the Hub Config card's copy is dropped by
    # _apply_hub_config). The engine unions this with any hub_config copy.
    def get_ignored_hostnames(self) -> list:
        v = self._data.get("ignored_hostnames")
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    def set_ignored_hostnames(self, hosts: list) -> None:
        with self._lock:
            self._data["ignored_hostnames"] = [str(x).strip() for x in hosts if str(x).strip()] \
                if isinstance(hosts, list) else []
            self._save()
