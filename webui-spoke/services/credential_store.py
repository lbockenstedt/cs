"""Fernet credential encryption for sensitive settings.json fields (moved verbatim from server.py)."""
from __future__ import annotations

import copy
import logging
from pathlib import Path

logger = logging.getLogger("client_sim_dashboard")
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Credential encryption ─────────────────────────────────────────────────────
# Fernet symmetric encryption for sensitive fields in settings.json.
# Key is generated once at install time and stored in .secret_key (chmod 600).
# Falls back to plaintext if key file or cryptography package is unavailable.
_ENC_PREFIX = "enc:"
_SENSITIVE_CFG_KEYS = {"access_token", "refresh_token", "client_secret"}
_SENSITIVE_CLASSIC_API_KEYS = {"password"}
_SENSITIVE_CENTRAL_API_KEYS = {"client_secret"}
_SENSITIVE_TOP_KEYS = {"relay_api_key", "github_token", "client_api_key", "admin_ws_token", "admin_password", "auth_ldap_bind_password", "auth_radius_secret", "auth_tacacs_secret"}
_SENSITIVE_TOP_DICT_KEYS = {"proxmox_approved_agents"}
_SENSITIVE_NOTIF_KEYS = {"smtp_password", "teams_webhook_url"}

try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _key_file = BASE_DIR / ".secret_key"
    if _key_file.exists():
        _fernet = _Fernet(_key_file.read_bytes().strip())
    else:
        _fernet = None
        logger.warning("No .secret_key found — credentials stored as plaintext")
except Exception:
    _fernet = None
    logger.warning("cryptography unavailable or key error — credentials stored as plaintext")


def _encrypt_secret(value: str) -> str:
    if not _fernet or not value:
        return value
    return _ENC_PREFIX + _fernet.encrypt(value.encode()).decode()


def _decrypt_secret(value: str) -> str:
    if not value or not value.startswith(_ENC_PREFIX):
        return value  # plaintext or empty — return as-is (legacy compat)
    if not _fernet:
        return value  # no key — return ciphertext unchanged
    try:
        return _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt a secret field — may be corrupted or from a different key")
        return ""


def _encrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields encrypted for disk storage."""
    out = copy.deepcopy(raw)
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _encrypt_secret(out[key])
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _encrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _encrypt_secret(out["central_config"][key])
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _encrypt_secret(out["central_api"]["classic"][key])
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _encrypt_secret(out["central_api"]["central"][key])
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _encrypt_secret(out["notifications"][key])
    return out


def _decrypt_settings(raw: dict) -> dict:
    """Return a deep copy of settings with sensitive fields decrypted into memory."""
    out = copy.deepcopy(raw)
    for key in _SENSITIVE_TOP_KEYS:
        if out.get(key):
            out[key] = _decrypt_secret(out[key])
    for key in _SENSITIVE_TOP_DICT_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {
                str(dict_key): _decrypt_secret(str(dict_value)) if dict_value not in (None, "") else ""
                for dict_key, dict_value in value.items()
            }
    for key in _SENSITIVE_CFG_KEYS:
        if out.get("central_config", {}).get(key):
            out["central_config"][key] = _decrypt_secret(out["central_config"][key])
    for key in _SENSITIVE_CLASSIC_API_KEYS:
        if out.get("central_api", {}).get("classic", {}).get(key):
            out["central_api"]["classic"][key] = _decrypt_secret(out["central_api"]["classic"][key])
    for key in _SENSITIVE_CENTRAL_API_KEYS:
        if out.get("central_api", {}).get("central", {}).get(key):
            out["central_api"]["central"][key] = _decrypt_secret(out["central_api"]["central"][key])
    for key in _SENSITIVE_NOTIF_KEYS:
        if out.get("notifications", {}).get(key):
            out["notifications"][key] = _decrypt_secret(out["notifications"][key])
    return out
