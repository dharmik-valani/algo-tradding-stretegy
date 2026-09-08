from __future__ import annotations

"""SMTP email for pre-market / post-market health digests."""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


def report_email_to() -> str:
    return (os.environ.get("REPORT_EMAIL_TO") or "dharmikvalani57@gmail.com").strip()


def smtp_configured() -> bool:
    user = (os.environ.get("SMTP_USER") or os.environ.get("REPORT_SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or os.environ.get("REPORT_SMTP_PASSWORD") or "").strip()
    return bool(user and password)


def send_email(
    *,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    to_addr: str | None = None,
) -> dict[str, Any]:
    """Send via Gmail SMTP (or any SMTP_* host). Returns status dict; never raises."""
    to_addr = (to_addr or report_email_to()).strip()
    user = (os.environ.get("SMTP_USER") or os.environ.get("REPORT_SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or os.environ.get("REPORT_SMTP_PASSWORD") or "").strip()
    host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT") or "587")
    from_addr = (os.environ.get("SMTP_FROM") or user or "paper-desk@localhost").strip()

    if not user or not password:
        return {
            "ok": False,
            "skipped": True,
            "reason": "SMTP_USER / SMTP_PASSWORD not set",
            "to": to_addr,
        }

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=45) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
        return {"ok": True, "to": to_addr, "subject": subject}
    except Exception as exc:
        return {"ok": False, "to": to_addr, "subject": subject, "error": str(exc)[:400]}
