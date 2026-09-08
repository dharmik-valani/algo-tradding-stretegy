from __future__ import annotations

"""Email for pre-market / post-market health digests.

Preferred: Brevo HTTP API when ``BREVO_API_KEY`` is set.
Fallback: SMTP via ``SMTP_*`` / ``EMAIL_*`` env vars.
"""

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any


def report_email_to() -> str:
    return (os.environ.get("REPORT_EMAIL_TO") or "dharmikvalani57@gmail.com").strip()


def _from_addr() -> str:
    return (
        os.environ.get("SMTP_FROM")
        or os.environ.get("EMAIL_FROM")
        or os.environ.get("EMAIL_USER")
        or os.environ.get("SMTP_USER")
        or os.environ.get("REPORT_SMTP_USER")
        or "paper-desk@localhost"
    ).strip()


def _brevo_api_key() -> str:
    return (
        os.environ.get("BREVO_API_KEY")
        or os.environ.get("SENDINBLUE_API_KEY")
        or ""
    ).strip()


def smtp_configured() -> bool:
    if _brevo_api_key():
        return True
    user = (
        os.environ.get("SMTP_USER")
        or os.environ.get("EMAIL_USER")
        or os.environ.get("REPORT_SMTP_USER")
        or ""
    ).strip()
    password = (
        os.environ.get("SMTP_PASSWORD")
        or os.environ.get("EMAIL_PASSWORD")
        or os.environ.get("REPORT_SMTP_PASSWORD")
        or ""
    ).strip()
    return bool(user and password)


def _send_via_brevo(
    *,
    subject: str,
    body_text: str,
    body_html: str | None,
    to_addr: str,
    from_addr: str,
    api_key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sender": {"email": from_addr, "name": "Paper Desk"},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body_text,
    }
    if body_html:
        payload["htmlContent"] = body_html

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body: Any = {}
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:200]}
            return {
                "ok": True,
                "provider": "brevo",
                "to": to_addr,
                "from": from_addr,
                "subject": subject,
                "message_id": (body or {}).get("messageId"),
            }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:400]
        return {
            "ok": False,
            "provider": "brevo",
            "to": to_addr,
            "from": from_addr,
            "subject": subject,
            "error": f"HTTP {exc.code}: {err_body}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "brevo",
            "to": to_addr,
            "from": from_addr,
            "subject": subject,
            "error": str(exc)[:400],
        }


def _send_via_smtp(
    *,
    subject: str,
    body_text: str,
    body_html: str | None,
    to_addr: str,
) -> dict[str, Any]:
    user = (
        os.environ.get("SMTP_USER")
        or os.environ.get("EMAIL_USER")
        or os.environ.get("REPORT_SMTP_USER")
        or ""
    ).strip()
    password = (
        os.environ.get("SMTP_PASSWORD")
        or os.environ.get("EMAIL_PASSWORD")
        or os.environ.get("REPORT_SMTP_PASSWORD")
        or ""
    ).strip()
    # Brevo SMTP relay is smtp-relay.brevo.com; do not use smtp.gmail.com with Brevo keys.
    host = (
        os.environ.get("SMTP_HOST")
        or os.environ.get("EMAIL_HOST")
        or "smtp.gmail.com"
    ).strip()
    port = int(os.environ.get("SMTP_PORT") or os.environ.get("EMAIL_PORT") or "587")
    from_addr = _from_addr()

    if not user or not password:
        return {
            "ok": False,
            "skipped": True,
            "reason": "No BREVO_API_KEY and SMTP_USER/EMAIL_USER + password not set",
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
        return {
            "ok": True,
            "provider": "smtp",
            "host": host,
            "to": to_addr,
            "from": from_addr,
            "subject": subject,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "smtp",
            "host": host,
            "to": to_addr,
            "from": from_addr,
            "subject": subject,
            "error": str(exc)[:400],
        }


def send_email(
    *,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    to_addr: str | None = None,
) -> dict[str, Any]:
    """Send via Brevo API (preferred) or SMTP. Returns status dict; never raises."""
    to_addr = (to_addr or report_email_to()).strip()
    from_addr = _from_addr()
    api_key = _brevo_api_key()
    if api_key:
        return _send_via_brevo(
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            to_addr=to_addr,
            from_addr=from_addr,
            api_key=api_key,
        )
    return _send_via_smtp(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        to_addr=to_addr,
    )
