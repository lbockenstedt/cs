"""Pydantic models for the cs spoke — single-tenant (no tenant_id/user_id).

The webui-local (standalone hub) models carried tenant/user/auth fields; the cs spoke drops those
(the LM hub provides tenancy/users/approval). Phase 1 only exercises ``Client``;
``Command``/``AuditEntry``/``ProxmoxTarget`` are defined here so Phase 2/3 code
imports from one place.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Client(BaseModel):
    """One simulation client (VM) as tracked by the spoke registry."""

    hostname: str
    simulation_id: str = ""
    platform: str = "linux"
    last_seen: Optional[float] = None  # epoch seconds
    iteration: int = 0
    connected_ssid: str = ""
    ip: str = ""  # IPv4 on the sim interface (default-route src); "" = no IP yet
    gateway_reachable: Optional[bool] = None
    vh_connected: bool = False
    active_simulations: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    # Self-reported physical adapter inventory: [{name, mac, media_type, is_default_route}].
    # media_type is "wired" | "wireless" | "other". Empty until the client's first
    # heartbeat lands (or on old clients that predate this field).
    adapters: List[Dict[str, Any]] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # pending | approved | rejected
    vmid: Optional[int] = None


class Command(BaseModel):
    """A queued control command (target = client hostname | "proxmox" | "spoke")."""

    id: str
    target: str
    type: str  # kill_switch | restart_sim | reclone | reboot | repo_sync | update_now | config_update ...
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: str = "queued"  # queued | delivered | executed | expired | failed
    created_at: float = 0.0
    expires_at: float = 0.0  # 24h TTL
    delivered_at: Optional[float] = None
    executed_at: Optional[float] = None
    result: Optional[Any] = None


class AuditEntry(BaseModel):
    """Append-only audit row for command/ops history (7-day rolling)."""

    id: str
    task_type: str
    status: str
    detail: str = ""
    initiated_by: str = "local"  # hub user name, or "local"
    timestamp: float = 0.0


class ProxmoxTarget(BaseModel):
    """Connection parameters for a Proxmox deploy target (Phase 3)."""

    host: str
    token_id: str = ""
    token_secret: str = ""
    node: str = ""
    pool: str = ""
    bridge: str = "vmbr0"
    vlan: int = 0
    template_vmid: int = 0
    name_prefix: str = "client"
    vmid_range: List[int] = Field(default_factory=list)