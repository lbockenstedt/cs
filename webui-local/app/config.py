"""Application settings loaded from environment variables and an optional .env file.

Use get_settings() to obtain the single cached Settings instance.
"""
# lru_cache makes get_settings() build the settings object only once per process.
from functools import lru_cache
# Path type returned by Settings.data_path().
from pathlib import Path

# Field/AliasChoices let one setting be read from one or more differently named env variables.
from pydantic import AliasChoices, Field
# BaseSettings reads values from the environment; SettingsConfigDict configures that loading.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configurable values for the web UI; each attribute can be overridden by an env variable.
    """
    # Core
    # Deployment environment name ("dev" relaxes the production safety checks below).
    env: str = Field(default="dev", validation_alias=AliasChoices("ENV"))
    # Key used to sign JWT login tokens; the default is a deliberately obvious placeholder.
    secret_key: str = "change-me-in-production"
    webui_secret_key: str = ""  # Fernet master key for secrets at rest (required outside dev)
    # JWT signing algorithm (HMAC-SHA256 with the shared secret_key).
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 120  # 2 hours; was 8 hours — tokens are silently refreshed by the frontend

    # Admin bootstrap
    # Username for the admin account created on first start-up.
    first_admin_username: str = "admin"
    # Password for that first admin; accepts either ADMIN_PASSWORD or FIRST_ADMIN_PASSWORD.
    first_admin_password: str = Field(
        default="changeme",
        validation_alias=AliasChoices("ADMIN_PASSWORD", "FIRST_ADMIN_PASSWORD"),
    )

    # Data directory
    # Folder where the app keeps its database, certificates and other persistent files.
    data_dir: str = "./data"

    # TLS
    tls_cert_path: str = ""   # override path; auto-generated if empty
    tls_key_path: str = ""    # override path; auto-generated if empty

    # Auth provider stubs (inactive for now)
    # OpenID Connect login settings (unused until the OIDC provider is implemented).
    oidc_enabled: bool = False
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""

    # LDAP login settings (unused until the LDAP provider is implemented).
    ldap_enabled: bool = False
    ldap_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_search_base: str = ""

    # RADIUS login settings; 1812 is the standard RADIUS authentication port.
    radius_enabled: bool = False
    radius_host: str = ""
    radius_port: int = 1812
    radius_secret: str = ""

    # Read a .env file in the working directory as UTF-8 and match env names case-insensitively.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    def data_path(self) -> Path:
        """Return data_dir as a Path object so callers can join file names onto it."""
        return Path(self.data_dir)


@lru_cache
def get_settings() -> Settings:
    """Build, validate and cache the Settings object.

    Raises RuntimeError outside dev when required secrets are missing or left at defaults.
    """
    # Load all values from the environment / .env file.
    settings = Settings()
    # Treat only an explicit "dev" environment (case-insensitive) as development.
    is_dev = str(settings.env or "").strip().lower() == "dev"
    # Outside dev, secrets at rest cannot be encrypted without a master key, so refuse to start.
    if not settings.webui_secret_key and not is_dev:
        raise RuntimeError("WEBUI_SECRET_KEY must be set in production")
    # Outside dev, a default JWT secret would let anyone forge logins, so refuse to start.
    if settings.secret_key == "change-me-in-production" and not is_dev:
        raise RuntimeError("SECRET_KEY must be set in production (used for JWT signing)")
    return settings
