"""Alert delivery for tenants: Microsoft Teams webhook cards and SMTP e-mail.

Notification settings are stored encrypted per tenant (or as JSON on a workspace) and are
normalised here so older key names keep working.
"""
# Allow ``dict[str, Any] | None`` style hints on older Python versions.
from __future__ import annotations

# asyncio runs the blocking SMTP call in a thread; json parses workspace config.
import asyncio
import json
# logging reports delivery failures; smtplib/MIMEText build and send e-mail.
import logging
import smtplib
from email.mime.text import MIMEText
# Any is used in type hints for loosely structured config dicts.
from typing import Any

# httpx is the async HTTP client used to post to Teams webhooks.
import httpx

# Application settings accessor.
from app.config import get_settings

# Module logger for notification messages.
logger = logging.getLogger(__name__)
# Settings loaded once at import (kept for module consumers).
settings = get_settings()


def _normalize_notification_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a notification config with legacy keys mapped to the current names.

    Accepts to_emails as a comma string or list, and teams_webhook/teams_webhook_url and
    smtp_pass/smtp_password as aliases; both webhook keys are populated in the result.
    """
    # Copy so the caller's dict is untouched; None becomes an empty config.
    data = dict(cfg or {})
    # Recipients may be stored as "a@x, b@y"; split into a clean list.
    to_emails = data.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [item.strip() for item in to_emails.split(",") if item.strip()]
    # Prefer the new webhook key, fall back to the legacy one.
    teams_webhook = data.get("teams_webhook") or data.get("teams_webhook_url") or ""
    # Prefer smtp_pass; only use legacy smtp_password when smtp_pass is absent.
    smtp_pass = data.get("smtp_pass")
    if smtp_pass is None:
        smtp_pass = data.get("smtp_password", "")
    # Merge the original data with the normalised fields (normalised values win).
    return {
        **data,
        "enabled": bool(data.get("enabled")),
        "teams_webhook": teams_webhook,
        "teams_webhook_url": teams_webhook,
        "smtp_pass": smtp_pass or "",
        "to_emails": to_emails,
    }


async def send_teams_webhook(url: str, title: str, message: str, color: str = "FF0000"):
    """Post an Office 365 "MessageCard" to a Teams incoming webhook; errors are only logged.

    color is a hex string for the card's accent bar (red by default).
    """
    # Legacy connector card format accepted by Teams incoming webhooks.
    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": title,
        "sections": [{"activityTitle": title, "activityText": message}],
    }
    # Short timeout so a slow webhook cannot stall the caller.
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(url, json=payload)
        # Notifications are best effort: log and carry on rather than failing the caller.
        except Exception as e:
            logger.warning(f"Teams webhook failed: {e}")


def _send_email_sync(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    to_addrs: list,
    message: str,
) -> None:
    """Send an already-formatted e-mail message synchronously (blocking)."""
    # Open the SMTP connection; the with-block closes it automatically.
    with smtplib.SMTP(smtp_host, smtp_port) as s:
        # Log in only when a username is configured (some relays allow anonymous sending).
        if smtp_user:
            s.login(smtp_user, smtp_pass)
        s.sendmail(from_addr, to_addrs, message)


async def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    to_addrs: list,
    subject: str,
    body: str,
):
    """Build a plain-text e-mail and send it without blocking the event loop; errors are logged."""
    # Plain-text body with the standard headers.
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    # smtplib is blocking, so run the send in a worker thread.
    try:
        await asyncio.to_thread(
            _send_email_sync,
            smtp_host,
            smtp_port,
            smtp_user,
            smtp_pass,
            from_addr,
            to_addrs,
            msg.as_string(),
        )
    # Best-effort delivery: log the failure instead of raising.
    except Exception as e:
        logger.warning(f"Email notification failed: {e}")


def get_notification_config(tenant_id: str) -> dict | None:
    """Get decrypted notification config for a tenant. Returns None if not configured."""
    # Imported here to avoid circular imports at module load time.
    from . import store
    from .crypto import decrypt_dict

    # Tenants without saved notification settings have nothing to send.
    tenant = store.get_tenant(tenant_id)
    if not tenant or not tenant.notification_config_enc:
        return None
    # Decrypt and normalise; return the config only when notifications are enabled.
    try:
        cfg = _normalize_notification_config(decrypt_dict(tenant.notification_config_enc))
        return cfg if cfg.get("enabled") else None
    # A config that cannot be decrypted/parsed is treated as "not configured".
    except Exception:
        return None


async def notify_check_red(workspace, check):
    """Fire notifications when a check transitions to red."""
    # Start with no config; filled from the workspace JSON if present.
    cfg = {}
    if workspace.notification_config:
        # Parse and normalise the stored JSON config.
        try:
            cfg = _normalize_notification_config(json.loads(workspace.notification_config))
        # Bad JSON should not crash the checker; just skip notifying this workspace.
        except Exception:
            logger.warning("Malformed notification_config for workspace %s, skipping", getattr(workspace, "name", "unknown"))
    # Do nothing unless notifications are switched on.
    if not cfg.get("enabled"):
        return
    # Message text; timeout is doubled because a check only goes red after two missed windows.
    title = f"🔴 Check Failed: {check.check_name}"
    message = (
        f"Workspace **{workspace.name}** — check `{check.check_name}` has gone RED "
        f"(no report in {check.timeout_minutes * 2}min)."
    )
    # Send to Teams when a webhook URL is configured.
    if cfg.get("teams_webhook"):
        await send_teams_webhook(cfg["teams_webhook"], title, message)
    # Send e-mail when a server and recipients are set; 587 is the standard submission port.
    if cfg.get("smtp_host") and cfg.get("to_emails"):
        await send_email(
            cfg["smtp_host"],
            cfg.get("smtp_port", 587),
            cfg.get("smtp_user", ""),
            cfg.get("smtp_pass", ""),
            cfg.get("from_email", "csw@localhost"),
            cfg["to_emails"],
            title,
            message,
        )
