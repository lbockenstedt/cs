"""Helpers for encrypting Hub secrets before writing them to JSON.

Two independent Fernet keys are used: the hub key (WEBUI_SECRET_KEY / hub.key) for
encrypt_str/encrypt_dict, and ENCRYPTION_KEY for encrypt_value/decrypt_value. The module
also signs Azure Blob SAS URLs and creates random API keys.
"""
# Allow modern type-hint syntax such as ``Fernet | None`` on older Python versions.
from __future__ import annotations

# base64 encodes keys/signatures; hashlib+hmac compute the SAS HMAC-SHA256 signature.
import base64
import hashlib
import hmac
# json serialises dicts before encryption; logging reports key-loading problems.
import json
import logging
# os reads env vars and random bytes; urllib.parse builds the SAS query string.
import os
import urllib.parse
# Date/time helpers for SAS validity windows; Path locates the key file.
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fernet is symmetric authenticated encryption; InvalidToken signals wrong key/corrupt data.
from cryptography.fernet import Fernet, InvalidToken

# Settings provide WEBUI_SECRET_KEY when no key file exists.
from .config import get_settings

# Lazily created cipher objects, cached so the key is resolved only once per process.
_fernet: Fernet | None = None
_value_fernet: Fernet | None = None

# Module logger for key-management messages.
_logger = logging.getLogger(__name__)

# If this file exists on the Azure Files mount, its contents are used as
# WEBUI_SECRET_KEY instead of the env var.  This allows the key to be
# provisioned once on the persistent share and rotated without redeploying.
_KEY_FILE_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "hub.key"


def _persist_key_to_file(path: Path, key: str) -> None:
    """Write key to hub.key so future restarts use the same key regardless of env var."""
    # Create the folder if needed and write the key; failures are logged but not fatal.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key)
        _logger.info("WEBUI_SECRET_KEY persisted to key file: %s", path)
    # A read-only or missing share should not stop the app; it just keeps using the in-memory key.
    except OSError as exc:
        _logger.warning("Could not persist key to %s: %s", path, exc)


def _load_key_from_file(path: Path) -> str | None:
    """Return the trimmed key string from the key file, or None if absent/unreadable."""
    # Read the file only if it exists, and use it only if it is non-empty.
    try:
        if path.exists():
            key = path.read_text().strip()
            if key:
                _logger.info("WEBUI_SECRET_KEY loaded from key file: %s", path)
                return key
    # Unreadable file: log and let the caller fall back to the environment variable.
    except OSError as exc:
        _logger.warning("Could not read key file %s: %s — falling back to env var", path, exc)
    return None


def _get_fernet() -> Fernet:
    """Return the cached hub Fernet cipher, resolving the key on first use.

    Key priority: hub.key file, then WEBUI_SECRET_KEY (persisted to hub.key), then a
    freshly generated throwaway key for dev.
    """
    # Tell Python we are assigning the module-level cache, not a local variable.
    global _fernet
    # Only resolve the key the first time; later calls reuse the cached cipher.
    if _fernet is None:
        # Prefer key file on Azure Files share; fall back to env var
        key = _load_key_from_file(_KEY_FILE_PATH)
        # No key file: try the WEBUI_SECRET_KEY setting instead.
        if not key:
            settings = get_settings()
            key = settings.webui_secret_key.strip()
            if key:
                # Persist the env-var key to hub.key so future restarts use it
                # even if WEBUI_SECRET_KEY is later changed or rotated.
                _persist_key_to_file(_KEY_FILE_PATH, key)
        # Still no key (dev without config): generate one so the app works, and warn loudly.
        if not key:
            key = Fernet.generate_key().decode()
            _logger.warning(
                "WEBUI_SECRET_KEY not set in dev — generated ephemeral key. "
                "Secrets will not survive restart. Set WEBUI_SECRET_KEY in .env"
            )
            _persist_key_to_file(_KEY_FILE_PATH, key)
        # Build the cipher; a malformed key raises, which we turn into a clear configuration error.
        try:
            _fernet = Fernet(key.encode())
        except Exception as exc:
            raise ValueError(
                "Invalid WEBUI_SECRET_KEY: must be a 32-byte URL-safe base64 Fernet key"
            ) from exc
    return _fernet


def _get_value_fernet() -> Fernet:
    """Return the cached Fernet cipher keyed by the ENCRYPTION_KEY env var.

    Raises RuntimeError if the variable is missing or not a valid Fernet key.
    """
    # Assign the module-level cache rather than a local variable.
    global _value_fernet
    # Resolve the key only once per process.
    if _value_fernet is None:
        key = os.environ.get("ENCRYPTION_KEY", "").strip()
        # Unlike the hub key there is no fallback: values encrypted with it must stay decryptable.
        if not key:
            raise RuntimeError("ENCRYPTION_KEY env var is not set")
        # Turn a malformed key into an explicit configuration error.
        try:
            _value_fernet = Fernet(key.encode())
        except Exception as exc:
            raise RuntimeError(
                "Invalid ENCRYPTION_KEY: must be a 32-byte URL-safe base64 Fernet key"
            ) from exc
    return _value_fernet


def encrypt_str(value: str) -> str:
    """Encrypt a plaintext string and return the Fernet ciphertext as text."""
    # Encode to bytes, encrypt, then decode the token back to text so it fits in JSON.
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_str(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string back to its original plaintext value."""
    # Reverse of encrypt_str: bytes in, decrypt (verifying integrity), text out.
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def encrypt_dict(data: dict) -> str:
    """Encrypt a dict as JSON. Returns ciphertext string."""
    # Serialise to JSON then encrypt the resulting string.
    return encrypt_str(json.dumps(data))


def decrypt_dict(ciphertext: str) -> dict:
    """Decrypt a ciphertext string back to dict."""
    # Decrypt to the JSON string then parse it back into a dict.
    return json.loads(decrypt_str(ciphertext))


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a Fernet token safe to store."""
    # Encrypt with the ENCRYPTION_KEY cipher and return the token as text.
    return _get_value_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string. Raises ValueError on failure."""
    # Wrong key or tampered data raises InvalidToken; convert it to a ValueError callers expect.
    try:
        return _get_value_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt value: invalid ENCRYPTION_KEY or corrupted data") from exc


def generate_blob_container_sas(
    account_name: str,
    account_key: str,
    container: str,
    permissions: str = "rl",
    hours: int = 1,
) -> str:
    """Generate an Azure Blob Service SAS URL for a container."""
    # Current UTC time anchors the SAS validity window.
    now = datetime.now(timezone.utc)
    # Start 5 minutes in the past to tolerate small clock differences between us and Azure.
    start = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Expire after the requested number of hours.
    expiry = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Azure Storage service version whose SAS signing format we follow.
    sv = "2023-11-03"

    # Azure requires these exact fields, in this order, one per line; empty strings are unused
    # fields
    # (identifier, IP range, resource type "c" = container, response header overrides).
    string_to_sign = "\n".join([
        permissions,
        start,
        expiry,
        f"/blob/{account_name}/{container}",
        "",
        "",
        "https",
        sv,
        "c",
        "",
        "",
        "",
        "",
        "",
        "",
    ])

    # The storage account key is base64; decode it to raw bytes for HMAC.
    key_bytes = base64.b64decode(account_key)
    # Sign the string with HMAC-SHA256 and base64-encode the result, as Azure expects.
    sig = base64.b64encode(
        hmac.new(key_bytes, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    # Query parameters that make up the SAS token (version, resource, perms, times, protocol, sig).
    params = {
        "sv": sv,
        "sr": "c",
        "sp": permissions,
        "st": start,
        "se": expiry,
        "spr": "https",
        "sig": sig,
    }
    # URL of the container; the SAS token is appended as the query string.
    base = f"https://{account_name}.blob.core.windows.net/{container}"
    return f"{base}?{urllib.parse.urlencode(params)}"


def generate_api_key() -> str:
    """Generate a URL-safe random spoke relay API key for one-time approval flows."""
    # 32 random bytes, URL-safe base64 without padding, so the key can go in URLs and headers.
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
