"""Pydantic models that define Hub persistence objects in the JSON store.

These schemas replace the old SQLAlchemy models from the legacy application.
Instances are serialized directly to JSON files under ``/data`` for users,
tenants, islands, command queues, and audit history, while still giving the
application validated types and default factories.
"""
# Postpone annotation evaluation so models can reference each other freely.
from __future__ import annotations

# datetime/timezone build UTC timestamps; typing hints describe optional/loose fields.
from datetime import datetime, timezone
from typing import Any, Optional
# uuid generates unique ids for new records.
import uuid

# Pydantic pieces: base class, model config, field options, aliases and a pre-validation hook.
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


def _now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime (default for timestamp fields)."""
    return datetime.now(timezone.utc)


def _uuid() -> str:
    """Return a new random UUID string (default for id fields)."""
    return str(uuid.uuid4())


class User(BaseModel):
    """A login account; tenant_roles lists {"tenant_id", "role"} pairs ("admin" or "viewer")."""
    # Unique id, login name and bcrypt password hash (empty for externally authenticated users).
    id: str = Field(default_factory=_uuid)
    username: str
    hashed_password: str
    # Superadmins can access every tenant and global settings.
    is_superadmin: bool = False
    # Per-tenant role assignments and creation time.
    tenant_roles: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    def get_role(self, tenant_id: str) -> Optional[str]:
        """Return this user's role in the given tenant, or None if not a member."""
        # Scan the role list for the requested tenant.
        for tr in self.tenant_roles:
            if tr["tenant_id"] == tenant_id:
                return tr["role"]
        return None

    def tenant_ids(self) -> list[str]:
        """Return the ids of every tenant this user belongs to."""
        return [tr["tenant_id"] for tr in self.tenant_roles]


class ProcessingMode(BaseModel):
    """Processing mode config — centralized or distributed per feature."""

    # Default mode for every feature; each field below can override it per feature (None = inherit).
    global_mode: str = "centralized"
    aruba_polling: Optional[str] = None
    teams_webhook: Optional[str] = None
    email: Optional[str] = None
    heartbeat: Optional[str] = None
    gkill: Optional[str] = None
    schedules: Optional[str] = None
    repo_sync: Optional[str] = None

    def resolve(self, feature: str) -> str:
        """Return the mode for ``feature``, falling back to global_mode when not set."""
        # Unknown feature names also fall back to the global mode.
        val = getattr(self, feature, None)
        return val if val else self.global_mode


class Tenant(BaseModel):
    """A customer/organisation; owns spokes and stores its (encrypted) integration settings."""
    # Unique id, display name and Aruba Central customer id.
    id: str = Field(default_factory=_uuid)
    name: str
    aruba_cid: Optional[str] = None
    # Encrypted JSON blobs for Aruba Central, notification and GitHub settings.
    aruba_config_enc: Optional[str] = None
    notification_config_enc: Optional[str] = None
    github_config_enc: Optional[str] = None
    # Default and per-integration processing modes (centralized on the hub vs distributed).
    default_processing_mode: ProcessingMode = Field(default_factory=ProcessingMode)
    processing_modes: dict[str, str] = Field(default_factory=lambda: {
        "central_api": "centralized",
        "teams": "centralized",
        "email": "centralized",
    })
    # Optional hub-managed configuration pushed to spokes when enabled.
    hub_config_enabled: bool = False
    hub_config: dict[str, Any] = Field(default_factory=dict)
    # Hub-managed conf overrides: INI text distributed to spokes via config_update.
    # Takes precedence over the GitHub-pulled version when the spoke is hub-connected.
    sim_conf_override: Optional[str] = None     # overrides configs/simulation.conf
    user_conf_override: Optional[str] = None    # overrides configs/user-overrides.conf
    # Per-client permanent simulation enables set by admin via the hub UI.
    # hostname → list of simulation names that should always appear active.
    # Merged into active_simulations on the hub; no GitHub key required.
    client_sim_overrides: dict[str, list[str]] = Field(default_factory=dict)
    # Aruba Central site settings used by the hub's Central browse/poll features.
    central_sites_config: dict[str, Any] = Field(default_factory=dict)
    central_browse_interval_minutes: int = 5  # How often the hub polls Central API (1–60 min)
    # USB device vendor/product ids allowed for pass-through on spokes.
    usb_vidpids: list[dict[str, Any]] = Field(default_factory=list)
    onboarding_psk_enc: str = ""  # Legacy single PSK — migrated to onboarding_psks_enc on first save
    onboarding_psks_enc: list[str] = Field(default_factory=list)  # List of encrypted PSKs
    # Audit metadata: when and by whom the tenant was created.
    created_at: datetime = Field(default_factory=_now)
    created_by: str = ""
    deleted_at: Optional[datetime] = None  # Soft delete timestamp — tenant is hidden but recoverable for 30 days


class Spoke(BaseModel):
    """A registered spoke (remote lab host) belonging to a tenant."""
    # Unique id, owning tenant and host identity/labels.
    id: str = Field(default_factory=_uuid)
    tenant_id: str
    hostname: str
    label: str = ""
    spoke_name: str = ""
    assigned_site: str = ""  # legacy single-site field, kept for backward-compat deserialization
    assigned_sites: list[str] = Field(default_factory=list)  # multi-site assignment

    # Pydantic runs this hook before field validation so old saved data still loads.
    @model_validator(mode="before")
    @classmethod
    def _migrate_assigned_site(cls, data: Any) -> Any:
        """Migrate legacy single assigned_site string to assigned_sites list."""
        # Only convert when the input is a dict that lacks the new list field.
        if isinstance(data, dict) and not data.get("assigned_sites"):
            # Read the legacy single-site value, treating None as empty.
            legacy = str(data.get("assigned_site") or "").strip()
            if legacy:
                # Copy before modifying so the caller's dict is not mutated.
                data = dict(data)
                data["assigned_sites"] = [legacy]
        return data
    # Lifecycle status ("pending" until approved) and the encrypted relay API key.
    status: str = "pending"
    api_key_enc: Optional[str] = None
    # Proxmox server address and encrypted API token used for VM control.
    proxmox_host: str = ""
    proxmox_token_enc: str = ""
    # Initial config supplied at registration, and the current desired config.
    seed_config: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    # Whether this spoke's work runs on the hub or on the spoke itself.
    processing_mode: ProcessingMode = Field(default_factory=ProcessingMode)
    # Config version tracking: desired vs applied version, when applied, and hash of last push.
    config_version: int = 0
    applied_config_version: int = 0
    last_config_applied_at: Optional[datetime] = None
    last_pushed_config_hash: Optional[str] = None
    # Last check-in time and the latest telemetry snapshot reported by the spoke.
    last_seen: Optional[datetime] = None
    telemetry: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class PendingSpoke(BaseModel):
    """Spoke registration before tenant assignment."""

    id: str = Field(default_factory=_uuid)
    # Unique id plus the identity the host registered with.
    hostname: str
    label: str = ""
    spoke_name: str = ""
    tenant_hint: str = ""  # Tenant ID the spoke pre-registered for (may be empty)
    # Initial configuration offered by the spoke, and registration / last contact times.
    seed_config: dict[str, Any] = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=_now)
    last_seen: Optional[datetime] = None


class Command(BaseModel):
    """A queued instruction for a spoke (e.g. reboot a VM) and its delivery/result tracking."""
    # Allow construction by field name as well as by alias.
    model_config = ConfigDict(populate_by_name=True)

    # Unique id; spoke_id also accepts the legacy "island_id" name but is always saved as spoke_id.
    id: str = Field(default_factory=_uuid)
    spoke_id: str = Field(validation_alias=AliasChoices("spoke_id", "island_id"), serialization_alias="spoke_id")
    # Owning tenant, who should run it, and the command type with its arguments.
    tenant_id: str
    target: str = "spoke"
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # Lifecycle: queued -> delivered -> executed; expires_at stops stale commands from running.
    status: str = "queued"
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime
    delivered_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    # Result data reported back by the spoke after execution.
    result: Optional[dict[str, Any]] = None


class AuditEntry(BaseModel):
    """One audit-log record describing a task run against a spoke."""
    # Allow construction by field name as well as by alias.
    model_config = ConfigDict(populate_by_name=True)

    # Unique id; spoke_id also accepts the legacy "island_id" input name.
    id: str = Field(default_factory=_uuid)
    spoke_id: str = Field(validation_alias=AliasChoices("spoke_id", "island_id"), serialization_alias="spoke_id")
    # Which tenant, what kind of task, and how it was executed (centralized/distributed).
    tenant_id: str
    task_type: str
    execution_mode: str
    # Outcome, free-text detail, who triggered it and any structured result.
    status: str
    detail: str = ""
    initiated_by: str = ""
    result: Optional[dict[str, Any]] = None
    # When the entry was recorded.
    timestamp: datetime = Field(default_factory=_now)


class SpokeBackupConfig(BaseModel):
    """Per-spoke backup selection: which Proxmox VM ids to back up."""
    vm_ids: list[int] = Field(default_factory=list)


class BackupConfig(BaseModel):
    """Tenant VM backup settings: per-spoke selections and the Azure Blob target."""
    # Map of spoke id -> VM selection, and how many backups to keep per VM.
    spokes: dict[str, SpokeBackupConfig] = Field(default_factory=dict)
    retention: int = 3
    # Azure storage account/container and the encrypted account key (Fernet token, not plaintext).
    azure_account: str = "csvmstorage"
    azure_container: str = "vms"
    azure_key_enc: str = "gAAAAABqBJh3Y-hvGAVJXju-Cp2qBDjR73zbyYoJ1ukCCd2JoWbYhemtXxlTVdHmYqOrKs9NjfAvNGnD1s-mC7MA8hDN8jE2-smgeh_SHeqCTXzIqi1GwWV82gf-kQNe_j6OgO7CeXMZeWHOjTgV1Q780Z0yRsQ5KCZAiaJdMy7doyqd8sQnR-ZGBeaxHXQJmXs2tlX-bkWo"

class HubAuthConfig(BaseModel):
    """Hub-wide login provider settings (local, LDAP, RADIUS or TACACS+); secrets are encrypted."""
    # Which provider is active ("local" uses the built-in user database).
    auth_provider: str = "local"
    # LDAP server, bind account, user search base/filter, and group-to-role mapping.
    auth_ldap_url: str = ""
    auth_ldap_bind_dn: str = ""
    auth_ldap_bind_password_enc: str = ""
    auth_ldap_user_base: str = ""
    auth_ldap_user_filter: str = "(&(objectClass=user)(sAMAccountName={username}))"
    auth_ldap_group_superadmin: str = ""
    auth_ldap_group_tenant_admin: str = ""
    auth_ldap_tenant_id: str = ""
    # RADIUS server (1812 is the standard port) and which reply attribute/value means superadmin.
    auth_radius_host: str = ""
    auth_radius_port: int = 1812
    auth_radius_secret_enc: str = ""
    auth_radius_role_attr: str = "Filter-Id"
    auth_radius_superadmin_val: str = "superadmin"
    # TACACS+ server (49 is the standard port); privilege level 15 maps to superadmin.
    auth_tacacs_host: str = ""
    auth_tacacs_port: int = 49
    auth_tacacs_secret_enc: str = ""
    auth_tacacs_superadmin_priv: int = 15
    # Role given to externally authenticated users that match no mapping.
    auth_default_role: str = "superadmin"


class MacProfileEntry(BaseModel):
    """One vendor entry in a mac_config.json profile."""

    # Vendor name, its MAC OUI prefix, and how many interfaces to generate with it.
    vendor: str
    oui: str      # e.g. "3c:15:c2"
    count: int = 1


class MacProfile(BaseModel):
    """Stored mac_config profile for a spoke's T3 devices."""

    # Which spoke/tenant the profile belongs to, and its vendor entries.
    spoke_id: str
    tenant_id: str
    entries: list[MacProfileEntry] = Field(default_factory=list)
    # When and by whom the profile was last changed.
    updated_at: datetime = Field(default_factory=_now)
    updated_by: str = ""

    @property
    def total_interfaces(self) -> int:
        """Total number of interfaces across all vendor entries."""
        return sum(e.count for e in self.entries)


class OuiPoolEntry(BaseModel):
    """Single row in the global OUI reference pool."""

    # Vendor, OUI prefix and an example device model for that OUI.
    vendor: str
    oui: str          # e.g. "3c:15:c2"
    device_type: str  # e.g. "iPhone 11 Pro"


class QAApiKey(BaseModel):
    """A tenant-scoped API key used exclusively by the QA runner.

    Only the SHA-256 hash of the raw key is persisted.  The plaintext key is
    returned exactly once at creation time and cannot be recovered afterwards.
    """

    # Unique id, owning tenant and a human description of the key's purpose.
    id: str = Field(default_factory=_uuid)
    tenant_id: str
    description: str = ""
    key_hash: str          # SHA-256 hex digest of the raw key
    created_by: str        # username of the superadmin who generated it
    # Creation time and the most recent successful use (for spotting stale keys).
    created_at: datetime = Field(default_factory=_now)
    last_used_at: Optional[datetime] = None
