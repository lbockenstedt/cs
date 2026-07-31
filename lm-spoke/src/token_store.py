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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("TokenStore")

# A client seen within this window keeps its sim- tags. This MUST track the
# quota engine's OFFLINE_TTL_S (3600s), NOT the 300s "online" window used by
# client_rows / QT_ONLINE_S.
#
# The engine keeps an offline-but-alive runner ASSIGNED and PRODUCING for a full
# hour — its VM is still running the sim; only the agent's heartbeat lapsed. At
# 300s this map declared such a client offline and handed back an EMPTY tag set,
# so apply_sim_tags STRIPPED the sim- tags off a VM that was still running the
# simulation and still counted in the ledger. Result: a client quiet for 5-60
# minutes showed `<no-tags>` in Proxmox while the Engine State page listed it as
# a producing runner — the two views disagreed and the tags looked broken.
#
# Borrowing QT_ONLINE_S was the original mistake: that window governs DONGLE
# QUARANTINE (is this dongle answering right now), a different question from
# "is this client still running its assigned sim".
_ONLINE_WINDOW_S = 3600.0

_SIM_TAG_PREFIX = "sim-"


def norm_hostname(h: Any) -> str:
    """Normalize a host identity for JOINING two independently-reported names.

    Telemetry (``proxmox_states`` keys) and the agent registration
    (``connected_agents[aid]["hostname"]``) are reported separately, so one can
    be an FQDN or differently-cased. Lowercase + strip the DNS domain so
    ``PXMX-CS-SVR-03.example.com`` and ``pxmx-cs-svr-03`` join. Used only as a
    FALLBACK after an exact match, so this can never merge two genuinely
    different short names."""
    return str(h or "").strip().lower().split(".")[0]


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
    tags). Returns ``{}`` when no registry is wired.

    ClientRegistry exposes ``get_all()`` (a ``{hostname: entry}`` dict) and has
    NO ``online`` field — so derive liveness from ``last_seen`` freshness here,
    using the ENGINE's offline TTL (see _ONLINE_WINDOW_S) so a still-assigned
    runner keeps its tags. (The prior code called a nonexistent
    ``registry.get_clients()`` behind a ``hasattr`` guard, so it always fell to
    ``[]`` and this map was permanently empty — sim-tag sync never fired.)"""
    if registry is None:
        return {}
    try:
        clients = registry.get_all() if hasattr(registry, "get_all") else {}
    except Exception as e:  # noqa: BLE001
        logger.debug("sim-tag map: registry.get_all failed: %s", e)
        return {}
    now = time.time()
    out: Dict[str, List[str]] = {}
    for c in (clients or {}).values():
        try:
            host = str((c or {}).get("hostname") or (c or {}).get("name") or "").strip().lower()
            if not host:
                continue
            last_seen = (c or {}).get("last_seen")
            online = bool(isinstance(last_seen, (int, float))
                          and (now - last_seen) < _ONLINE_WINDOW_S)
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