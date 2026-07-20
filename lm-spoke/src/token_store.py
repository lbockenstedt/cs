"""TokenStore — per-host Proxmox API token persistence + sim-tag computation.

Ports ``cs/webui-spoke/server.py``:
  - per-host token store:        ``_get/_save_proxmox_token_for_host`` (3663-3737)
  - token provisioning reply:    ``_handle_provision_proxmox_token`` (6350-6467)
  - sim-tag sanitize:            ``_sanitize_proxmox_tag`` (3648-3652)
  - sim-tag sweep (source data): ``_sync_all_vm_sim_tags`` (3794-3819)

The unified pxmx agent creates the ``root@pam!cs-hub`` token locally (it has
pvesh; the cs spoke does not) and emits ``CS_TOKEN_RESULT`` up → the hub relays
it here as ``CS_STORE_PROXMOX_TOKEN{hostname, token}``. This module persists the
token per host.

Sim-tag application MOVED to the pxmx AGENT: the cs spoke is off the Proxmox host
and cannot run ``qm``; PUTting ``tags=`` to the Proxmox API per VM from here
storms the telemetry hot path (CS_INGEST_TELEMETRY Request Timeouts). Instead
``compute_sim_tag_map`` derives the desired ``sim-`` tags per VM (from the
``CS_INGEST_TELEMETRY`` VM list + the client registry) with NO I/O, and
``CSSpoke`` dispatches each host's map to that host's agent
(``PXMX_APPLY_SIM_TAGS``) which applies them with local ``qm``/``pct``. Until
the client registry lands (``CSSpoke.registry``) the map is empty (no-op).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("TokenStore")

_SIM_TAG_PREFIX = "sim-"


def sanitize_tag(name: str) -> str:
    """Normalize a simulation name to a Proxmox-safe ``sim-`` tag (legacy
    3648-3652): lowercase, non-alphanumeric → ``-``, strip ends, ensure the
    ``sim-`` prefix, cap at 64 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not s:
        return ""
    tag = s if s.startswith(_SIM_TAG_PREFIX) else f"{_SIM_TAG_PREFIX}{s}"
    return tag[:64]

# NOTE: the current+desired tag MERGE (preserve manual tags, replace only
# ``sim-`` tags) now runs on the pxmx AGENT (pve_cmds.apply_sim_tags), reading
# each VM's live tags — the spoke only supplies the desired ``sim-`` set.


class TokenStore:
    """Per-host Proxmox API token persistence (legacy 3663-3737)."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "proxmox_tokens.json"
        self._tokens: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > 0:
                with open(self.path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._tokens = {str(k): str(v) for k, v in data.items() if v}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("token_store load failed: %s", e)
            self._tokens = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._tokens, f)
        except OSError as e:
            logger.warning("token_store save failed: %s", e)

    def get(self, hostname: Optional[str]) -> str:
        """Per-host token, falling back to a legacy global token key (legacy
        3665-3673). The unified world stores per-host only."""
        if hostname:
            t = self._tokens.get(hostname, "")
            if t:
                return t
        return self._tokens.get("__global__", "")

    def save(self, hostname: str, token: str) -> None:
        """Persist the token for a host. The token is never logged."""
        if not hostname or not token:
            return
        self._tokens[hostname] = str(token)
        self._save()
        logger.info("Proxmox token stored for host %s (value not logged)", hostname)

    def has_any(self) -> bool:
        return any(self._tokens.values())

    def all_hosts(self) -> Dict[str, str]:
        return dict(self._tokens)


# ── sim-tag apply ───────────────────────────────────────────────────────────


def _client_sim_map(registry: Any) -> Dict[str, List[str]]:
    """Build ``{normalized_client_hostname: [sim- tags]}`` from the client
    registry, mirroring legacy ``_sync_all_vm_sim_tags`` (3794-3819). Online
    clients get their ``active_simulations`` sanitized to ``sim-`` tags; offline
    clients get an empty list (clears their ``sim-`` tags but preserves manual
    tags). Returns ``{}`` when no registry is wired (Phase 2/3 not landed)."""
    if registry is None:
        return {}
    out: Dict[str, List[str]] = {}
    try:
        clients = registry.get_clients() if hasattr(registry, "get_clients") else []
    except Exception as e:  # noqa: BLE001
        logger.debug("sim-tag map: registry.get_clients failed: %s", e)
        return {}
    for c in clients or []:
        try:
            host = str((c or {}).get("hostname") or (c or {}).get("name") or "").strip().lower()
            if not host:
                continue
            online = bool((c or {}).get("online", False))
            sims = (c or {}).get("active_simulations") or []
            tags = [sanitize_tag(s) for s in sims] if online else []
            tags = [t for t in tags if t]
            out[host] = tags
        except Exception:  # noqa: BLE001
            continue
    return out


def compute_sim_tag_map(deploy: Any, registry: Any) -> Dict[str, Dict[str, List[str]]]:
    """Compute the desired ``sim-`` tags per VM, grouped by agent hostname —
    PURE, no Proxmox API, no I/O. Returns ``{agent_hostname: {vmid: [sim- tags]}}``.

    Tagging moved OFF this (off-host) cs spoke: it used to PUT ``tags=`` to the
    Proxmox API per VM, which storms the telemetry hot path (CS_INGEST_TELEMETRY
    Request Timeouts → stale VM Server / quota engine fleet-wide). Now the spoke
    only *computes* the desired tags (from the client registry: client hostname →
    ``active_simulations``) and hands each host's sub-map to that host's pxmx
    AGENT via ``PXMX_APPLY_SIM_TAGS``; the agent applies them with local
    ``qm``/``pct`` and does the read-merge-write (preserving manual tags).

    Only VMs whose ``name`` matches a registered client are included — unmatched
    VMs are omitted so the agent never touches their tags. Templates and LXCs are
    skipped. Returns ``{}`` until the client registry is wired."""
    client_map = _client_sim_map(registry)
    if not client_map:
        return {}  # nothing to sync until the client registry lands
    out: Dict[str, Dict[str, List[str]]] = {}
    states = getattr(deploy, "proxmox_states", {}) or {}
    for agent_hn, st in states.items():
        host_map: Dict[str, List[str]] = {}
        for vm in (st.get("vms") or []):
            try:
                vmid = vm.get("vmid")
                vm_name = str(vm.get("name") or "").strip().lower()
                if not vmid or not vm_name:
                    continue
                if vm.get("is_template") or vm.get("type") == "lxc":
                    continue
                desired = client_map.get(vm_name)
                if desired is None:
                    continue  # not a managed client VM — don't touch its tags
                host_map[str(vmid)] = list(desired)
            except Exception:  # noqa: BLE001
                continue
        if host_map:
            out[agent_hn] = host_map
    return out