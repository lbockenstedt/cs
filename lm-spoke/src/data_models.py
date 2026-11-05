"""Pydantic models for the cs spoke — single-tenant (no tenant_id/user_id).

The webui-local (standalone hub) models carried tenant/user/auth fields; the cs spoke drops those
(the LM hub provides tenancy/users/approval). Phase 1 only exercises ``Client``;
``Command``/``AuditEntry``/``ProxmoxTarget`` are defined here so Phase 2/3 code
imports from one place.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Generic typing helpers used to describe each field's type.
from typing import Any, Dict, List, Optional

# Pydantic gives validated, JSON-serialisable models; Field supplies safe defaults.
from pydantic import BaseModel, Field


class Client(BaseModel):
    """One simulation client (VM) as tracked by the spoke registry."""

    # VM hostname; the unique key for a client in the registry.
    hostname: str
    # Which simulation profile/run the client belongs to ("" = none yet).
    simulation_id: str = ""
    # Client operating system family, used to pick platform-specific behaviour.
    platform: str = "linux"
    # When the client last sent a heartbeat (None = never).
    last_seen: Optional[float] = None  # epoch seconds
    # How many simulation iterations the client has completed.
    iteration: int = 0
    # Wi-Fi network the client is currently associated with ("" = not on Wi-Fi).
    connected_ssid: str = ""
    # The client's IPv4 address on the simulation interface.
    ip: str = ""  # IPv4 on the sim interface (default-route src); "" = no IP yet
    # Whether the client could reach its default gateway (None = not yet tested).
    gateway_reachable: Optional[bool] = None
    # Whether the client is connected to the VH (virtual hub) service.
    vh_connected: bool = False
    # Names of simulations currently running on the client.
    active_simulations: List[str] = Field(default_factory=list)
    # Recent error messages reported by the client.
    errors: List[str] = Field(default_factory=list)
    # Self-reported physical adapter inventory: [{name, mac, media_type, is_default_route}].
    # media_type is "wired" | "wireless" | "other". Empty until the client's first
    # heartbeat lands (or on old clients that predate this field).
    # Network adapters the client reported (see the comment above).
    adapters: List[Dict[str, Any]] = Field(default_factory=list)
    # Per-client configuration overrides pushed from the spoke.
    config: Dict[str, Any] = Field(default_factory=dict)
    # Approval state; new clients wait as "pending" until approved or rejected.
    status: str = "pending"  # pending | approved | rejected
    # Proxmox VM id backing this client, when known.
    vmid: Optional[int] = None


class Command(BaseModel):
    """A queued control command (target = client hostname | "proxmox" | "spoke")."""

    # Unique command id.
    id: str
    # Who should run the command (see the class docstring).
    target: str
    # Command kind; the comment lists the known values.
    type: str  # kill_switch | restart_sim | reclone | reboot | repo_sync | update_now | config_update ...
    # Extra parameters for the command.
    payload: Dict[str, Any] = Field(default_factory=dict)
    # Lifecycle state of the command, as listed in the trailing comment.
    status: str = "queued"  # queued | delivered | executed | expired | failed
    # When the command was queued (epoch seconds).
    created_at: float = 0.0
    # When the queued command stops being valid (epoch seconds).
    expires_at: float = 0.0  # 24h TTL
    # When the target picked up / finished the command (None until then).
    delivered_at: Optional[float] = None
    executed_at: Optional[float] = None
    # Whatever the target returned after running the command.
    result: Optional[Any] = None


class AuditEntry(BaseModel):
    """Append-only audit row for command/ops history (7-day rolling)."""

    # Unique audit row id.
    id: str
    # What kind of operation was recorded.
    task_type: str
    # Outcome of the operation.
    status: str
    # Free-text detail about the operation.
    detail: str = ""
    # Who started the operation.
    initiated_by: str = "local"  # hub user name, or "local"
    # When it happened (epoch seconds).
    timestamp: float = 0.0


class ProxmoxTarget(BaseModel):
    """Connection parameters for a Proxmox deploy target (Phase 3)."""

    # Proxmox API host name or address.
    host: str
    # API token id and secret used to authenticate to Proxmox.
    token_id: str = ""
    token_secret: str = ""
    # Proxmox node name where VMs are created.
    node: str = ""
    # Optional resource pool new VMs are placed in.
    pool: str = ""
    # Network bridge that cloned VMs attach to (Proxmox default is vmbr0).
    bridge: str = "vmbr0"
    # VLAN tag for the VM NIC (0 = untagged).
    vlan: int = 0
    # VM id of the template that new clients are cloned from.
    template_vmid: int = 0
    # Prefix for generated VM names (e.g. client-01).
    name_prefix: str = "client"
    # Allowed VM id range for new clones, as [first, last].
    vmid_range: List[int] = Field(default_factory=list)