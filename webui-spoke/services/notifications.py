"""Notifications (helpers moved verbatim from server.py; shared deps imported from server)."""
from __future__ import annotations

from server import (
    Any,
    _HW_FRIENDLY,
    _client_count_payload,
    _prev_check_states,
    asyncio,
    copy,
    httpx,
    logger,
    settings,
    state,
)




def _public_notification_settings() -> dict[str, Any]:
    notif = copy.deepcopy(settings.get("notifications", {}))
    smtp_password = str(notif.pop("smtp_password", "") or "")
    teams_webhook_url = str(notif.pop("teams_webhook_url", "") or "")
    notif["smtp_password_configured"] = bool(smtp_password)
    notif["teams_webhook_url_configured"] = bool(teams_webhook_url)
    return notif




async def _check_transitions_and_notify(now: float) -> None:
    """Detect green→red transitions for sim checks and hardware checks, fire notifications."""
    notif = settings.get("notifications", {})
    transitions: list[dict[str, Any]] = []

    # ── Sim check transitions ─────────────────────────────────────
    for wsite, checks in state.central_status.items():
        for check_id, info in checks.items():
            key = f"sim:{check_id}:{wsite}"
            new_state = info["status"]  # "OK" or "ERROR"
            old_state = _prev_check_states.get(key)
            _prev_check_states[key] = new_state
            if old_state == "OK" and new_state == "ERROR":
                transitions.append({
                    "type": "sim",
                    "name": info.get("check_name", check_id),
                    "wsite": wsite,
                    "detail": f"Check '{info.get('check_name', check_id)}' turned red at site {wsite}",
                })

    # ── Hardware alert transitions ────────────────────────────────
    hw_checks: list[dict[str, Any]] = settings.get("hardware_checks", [])
    for check in hw_checks:
        cid = check["id"]
        total = sum(len(d) for d in state.hardware_alert_devices.get(cid, {}).values())
        new_state = "ERROR" if total > 0 else "OK"
        key = f"hw:{cid}"
        old_state = _prev_check_states.get(key)
        _prev_check_states[key] = new_state
        if old_state == "OK" and new_state == "ERROR":
            name = check.get("name") or _HW_FRIENDLY.get(cid, cid)
            transitions.append({
                "type": "hardware",
                "name": name,
                "detail": f"Hardware alert '{name}' is now active ({total} device(s) affected)",
            })

    for wsite, info in _client_count_payload().items():
        key = f"cc:{wsite}"
        new_state = info["status"]
        if new_state == "NO_DATA":
            _prev_check_states[key] = new_state
            continue
        old_state = _prev_check_states.get(key)
        _prev_check_states[key] = new_state
        if old_state == "OK" and new_state == "DEGRADED":
            transitions.append({
                "type": "client_count",
                "name": f"Client count — {info['site_name']}",
                "detail": (
                    f"Client count at {info['site_name']} dropped {info['drop_pct']:.1f}% "
                    f"(current: {info['current']}, avg: {info['hourly_avg']:.1f})"
                ),
            })

    if not transitions:
        return

    # ── Send notifications ────────────────────────────────────────
    for t in transitions:
        logger.warning("ALERT TRANSITION: %s", t["detail"])

    if notif.get("teams_enabled") and notif.get("teams_webhook_url"):
        await _send_teams_notifications(notif["teams_webhook_url"], transitions)

    if notif.get("email_enabled") and notif.get("smtp_host") and notif.get("smtp_to"):
        await asyncio.to_thread(_send_email_notifications, notif, transitions)




async def _send_teams_notifications(webhook_url: str, transitions: list[dict]) -> None:
    """POST an Adaptive Card to a Teams incoming webhook for each transition."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for t in transitions:
                card = {
                    "type": "message",
                    "attachments": [{
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
                                 "text": f"🔴 Client Simulator Alert: {t['name']}"},
                                {"type": "TextBlock", "text": t["detail"], "wrap": True},
                            ],
                        },
                    }],
                }
                resp = await client.post(webhook_url, json=card)
                if resp.status_code not in (200, 202):
                    logger.warning("Teams webhook returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Teams notification failed: %s", exc)




def _send_email_notifications(notif: dict, transitions: list[dict]) -> None:
    """Send SMTP email for each transition (runs in thread pool)."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    to_addrs = notif.get("smtp_to", [])
    if isinstance(to_addrs, str):
        to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]
    if not to_addrs:
        return

    body_lines = ["Client Simulator Alert\n"]
    for t in transitions:
        body_lines.append(f"• {t['detail']}")
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = notif.get("smtp_from", "client-sim@localhost")
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = f"[Client Simulator] {len(transitions)} check(s) turned RED"
    msg.attach(MIMEText(body, "plain"))

    try:
        host = notif.get("smtp_host", "")
        port = int(notif.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if port != 25:
                smtp.starttls()
            user = notif.get("smtp_user", "")
            pwd = notif.get("smtp_password", "")
            if user and pwd:
                smtp.login(user, pwd)
            smtp.sendmail(msg["From"], to_addrs, msg.as_string())
        logger.info("Email notification sent to %s", to_addrs)
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)
