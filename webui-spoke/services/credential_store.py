"""Fernet credential encryption for sensitive settings.json fields (moved verbatim from server.py)."""
# Postpone annotation evaluation so modern type hints are never evaluated at runtime.
from __future__ import annotations

# copy makes deep copies so the caller's settings dict is never changed in place.
import copy
# logging reports missing keys and decrypt failures; Path locates the key file.
import logging
from pathlib import Path

# Same logger name as server.py so messages land in the same log.
logger = logging.getLogger("client_sim_dashboard")
# The webui-spoke directory (one level above services/), where .secret_key lives.
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Credential encryption ─────────────────────────────────────────────────────
# Fernet symmetric encryption for sensitive fields in settings.json.
# Key is generated once at install time and stored in .secret_key (chmod 600).
# Falls back to plaintext if key file or cryptography package is unavailable.
# Marks a value as encrypted, so plaintext from older installs can still be read.
_ENC_PREFIX = "enc:"
# Which fields are secret, grouped by where they sit in settings.json.
_SENSITIVE_CFG_KEYS = {"access_token", "refresh_token", "client_secret"}
_SENSITIVE_CLASSIC_API_KEYS = {"password"}
_SENSITIVE_CENTRAL_API_KEYS = {"client_secret"}
_SENSITIVE_TOP_KEYS = {"relay_api_key", "github_token", "client_api_key", "admin_ws_token", "admin_password", "auth_ldap_bind_password", "auth_radius_secret", "auth_tacacs_secret"}
_SENSITIVE_TOP_DICT_KEYS = {"proxmox_approved_agents"}
_SENSITIVE_NOTIF_KEYS = {"smtp_password", "teams_webhook_url"}

# Load the key if the cryptography package and the key file are both there.
try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _key_file = BASE_DIR / ".secret_key"
    if _key_file.exists():
        _fernet = _Fernet(_key_file.read_bytes().strip())
    else:
        # No key file: secrets are saved as plaintext.
        _fernet = None
        logger.warning("No .secret_key found — credentials stored as plaintext")
except Exception:
    # Package missing or key unreadable: secrets are saved as plaintext.
    _fernet = None
    logger.warning("cryptography unavailable or key error — credentials stored as plaintext")


def _encrypt_secret(value: str) -> str:
    """Encrypt one value and add the "enc:" prefix (unchanged if no key or empty)."""
    # Without a key, or for an empty value, there is nothing to encrypt.
    if not _fernet or not value:
        return value
    # Encrypt and add the prefix so it can be recognised later.
    return _ENC_PREFIX + _fernet.encrypt(value.encode()).decode()


def _decrypt_secret(value: str) -> str:
    """Decrypt one "enc:" value; plaintext passes through and a bad value becomes an empty string.
    """
    if not value or not value.startswith(_ENC_PREFIX):
        return value  # plaintext or empty — return as-is (legacy compat)
    if not _fernet:
        return value  # no key — return ciphertext unchanged
    # Strip the prefix and decrypt.
    try:
        return _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        # Wrong key or damaged data: log it and drop the value rather than crash.
        logger.warning("Failed to decrypt a secret field — may be corrupted or from a different key")
        return ""


def _encrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields encrypted for disk storage."""
    # Work on a copy so the in-memory settings stay plaintext.
    out = copy.deepcopy(raw)
    # Top-level secret strings.
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _encrypt_secret(out[key])
    # Top-level dicts whose values are all secrets (e.g. agent keys).
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _encrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    # Secrets inside central_config.
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _encrypt_secret(out["central_config"][key])
    # Classic Central API password.
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _encrypt_secret(out["central_api"]["classic"][key])
    # New Central API client secret.
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _encrypt_secret(out["central_api"]["central"][key])
    # Notification secrets.
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _encrypt_secret(out["notifications"][key])
    return out


def _decrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields decrypted into memory."""
    # Work on a copy so the stored data is not changed.
    out = copy.deepcopy(raw)
    # Top-level secret strings.
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _decrypt_secret(out[key])
    # Top-level dicts whose values are all secrets (e.g. agent keys).
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _decrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    # Secrets inside central_config.
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _decrypt_secret(out["central_config"][key])
    # Classic Central API password.
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _decrypt_secret(out["central_api"]["classic"][key])
    # New Central API client secret.
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _decrypt_secret(out["central_api"]["central"][key])
    # Notification secrets.
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _decrypt_secret(out["notifications"][key])
    return out
