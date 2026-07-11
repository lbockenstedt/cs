"""TokenStore — per-host Proxmox API token persistence + sim-tag sync (Phase F).

Ports ``cs/webui-spoke/server.py``:
  - per-host token store:        ``_get/_save_proxmox_token_for_host`` (3663-3737)
  - token provisioning reply:    ``_handle_provision_proxmox_token`` (6350-6467)
  - sim-tag sanitize/merge/apply: ``_sanitize_proxmox_tag`` (3648-3652),
                                  ``_merge_sim_tags`` (3655-3660),
                                  ``_apply_sim_tags_for_vm`` (3740-3764)
  - sim-tag sweep:               ``_sync_all_vm_sim_tags`` (3794-3819)

The unified pxmx agent creates the ``root@pam!cs-hub`` token locally (it has
pvesh; the cs spoke does not) and emits ``CS_TOKEN_RESULT`` up → the hub relays
it here as ``CS_STORE_PROXMOX_TOKEN{hostname, token}``. This module persists the
token per host and (when a client registry is available) drives sim-tag sync so
each VM whose Proxmox ``name`` matches a sim client carries that client's
``sim-`` tags. The token secret is persisted to ``data/proxmox_tokens.json``
and is NEVER logged.

Sim-tag sync is driven from ``CS_INGEST_TELEMETRY`` (the per-host VM list is
there). It requires a client→sim-tags map; until the Phase 2/3 client registry
lands (``CSSpoke.registry``) the map is empty and the sweep is a no-op — the
machinery is in place and ready.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Proxmox API port for sim-tag PUTs. Default 8006 (Proxmox VE stock), overridable
# per-deployment via CS_PROXMOX_API_PORT (e.g. a proxied/non-stock port). A wrong
# port just means the PUTs fail → the sweep reports failures and the cs spoke
# backs off (see cs_spoke._maybe_sync_sim_tags), never re-storming per frame.
_PROXMOX_API_PORT = (os.environ.get("CS_PROXMOX_API_PORT") or "8006").strip() or "8006"

# httpx is only needed for sim-tag sync (apply_sim_tags_for_vm), which is a
# no-op until the client registry lands (CSSpoke.registry is None in Phase 2/3).
# Import it lazily + guarded so this module — and therefore cs_spoke — loads
# cleanly even if httpx isn't installed in the spoke venv (mirrors
# sim_primitives.py's guarded import). A missing httpx only surfaces as a
# debug log when sim-tag sync actually runs.
_HAS_HTTPX = True
try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    _HAS_HTTPX = False

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


def merge_sim_tags(current_tags_str: str, desired_sim_tags: List[str]) -> str:
    """Replace only ``sim-``-prefixed tags; preserve all manual Proxmox tags
    (legacy 3655-3660)."""
    existing = [t.strip() for t in (current_tags_str or "").split(";") if t.strip()]
    non_sim = [t for t in existing if not t.lower().startswith(_SIM_TAG_PREFIX)]
    merged = non_sim + sorted({t for t in desired_sim_tags if t})
    return ";".join(merged)


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


async def apply_sim_tags_for_vm(hostname: str, node: str, vmid: int,
                                  desired_sim_tags: List[str],
                                  current_tags: str, token: str, *,
                                  applied_cache: Dict[tuple, set],
                                  client: "httpx.AsyncClient") -> bool:
    """PUT ``tags=`` to the VM config via the Proxmox API (legacy 3752-3764).
    ``node`` is the Proxmox node name (short host label) owning the VM; the
    PUT path is ``/nodes/{node}/qemu/{vmid}/config`` and requires the real node
    name (a ``_`` placeholder would 404). Skips when the desired set is
    unchanged from the last apply. Uses the caller's SHARED ``client`` (one per
    sweep — not one per VM, which recreated an SSL context every call and burned
    CPU on the event loop). Returns True on success (incl. a no-op cache hit),
    False on a real PUT failure. Best-effort: never raises."""
    if not token:
        return False
    cache_key = (hostname, int(vmid))
    desired_set = set(desired_sim_tags)
    if applied_cache.get(cache_key) == desired_set:
        return True
    merged = merge_sim_tags(current_tags, desired_sim_tags)
    url = f"https://{hostname}:{_PROXMOX_API_PORT}/api2/json/nodes/{node}/qemu/{int(vmid)}/config"
    headers = {"Authorization": f"PVEAPIToken={token}"}
    try:
        resp = await client.put(url, headers=headers, data={"tags": merged})
        if resp.status_code == 200:
            applied_cache[cache_key] = desired_set
            logger.debug("VM %s tags updated on %s/%s", vmid, hostname, node)
            return True
        logger.debug("VM %s tag update on %s/%s:%s failed: HTTP %s",
                     vmid, hostname, node, _PROXMOX_API_PORT, resp.status_code)
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("VM %s tag update on %s/%s:%s error: %s",
                     vmid, hostname, node, _PROXMOX_API_PORT, e)
        return False


async def sync_all_sim_tags(deploy: Any, token_store: TokenStore,
                              registry: Any, *,
                              applied_cache: Optional[Dict[tuple, set]] = None
                              ) -> int:
    """Sweep every non-template, non-LXC VM across all known Proxmox hosts and
    apply the matching client's ``sim-`` tags (legacy ``_sync_all_vm_sim_tags``
    3794-3819). VMs whose ``name`` matches no client are left untouched. Returns
    ``(updated, failures)`` so the caller can back off when a target is
    unreachable/misconfigured. No-op when no registry / no httpx."""
    client_map = _client_sim_map(registry)
    if not client_map:
        return 0, 0  # nothing to sync until the client registry lands
    if not _HAS_HTTPX:
        logger.debug("sim-tag sweep skipped: httpx not installed")
        return 0, 0
    cache = applied_cache if applied_cache is not None else {}
    updated = failures = 0
    states = getattr(deploy, "proxmox_states", {}) or {}
    # ONE client for the whole sweep (was one per VM — each rebuilt an SSL
    # context, synchronous CPU on the event loop, once per VM per telemetry frame).
    async with httpx.AsyncClient(verify=False, timeout=8) as client:
        for agent_hn, st in states.items():
            token = token_store.get(agent_hn)
            if not token:
                continue
            node_hostname = str((st.get("node") or {}).get("hostname") or agent_hn)
            node = node_hostname.split(".")[0]
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
                        continue  # not a managed client VM — don't touch tags
                    ok = await apply_sim_tags_for_vm(
                        agent_hn, node, int(vmid), desired, str(vm.get("tags") or ""),
                        token, applied_cache=cache, client=client)
                    if ok:
                        updated += 1
                    else:
                        failures += 1
                except Exception:  # noqa: BLE001
                    failures += 1
                    continue
    return updated, failures