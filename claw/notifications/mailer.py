"""Transactional email sending via a stdlib-only SMTP client.

No new dependency: `smtplib` + `email.message.EmailMessage`, wrapped in
`asyncio.to_thread` so the blocking network call never stalls the event loop
— the same idiom already used for other blocking work (claw/main.py's
migration runner, claw/knowledge/service.py's document parsing).
"""

import smtplib
from asyncio import to_thread
from email.message import EmailMessage
from typing import Any


class SmtpSendError(Exception):
    """Raised when a send attempt fails. Safe to surface to an admin (e.g.
    the Control Plane's "Test Send Mail" button) but never log verbatim
    elsewhere — SMTP auth failures can echo back server-provided text."""


def _strip_crlf(value: str) -> str:
    # EmailMessage's header setters already reject embedded CR/LF by raising
    # ValueError, but stripping defensively here means a malformed
    # admin-controlled from_address or an interpolated display name can't
    # silently blackhole the whole send inside a swallowed exception.
    return value.replace("\r", "").replace("\n", "")


def _send_sync(cfg: dict[str, Any], to_address: str, subject: str, text_body: str, html_body: str | None) -> None:
    msg = EmailMessage()
    msg["Subject"] = _strip_crlf(subject)
    msg["From"] = _strip_crlf(cfg["from_address"])
    msg["To"] = _strip_crlf(to_address)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    use_ssl = bool(cfg.get("use_ssl"))
    use_tls = bool(cfg.get("use_tls"))
    username = cfg.get("username") or ""
    password = cfg.get("password") or ""

    try:
        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(cfg["host"], int(cfg["port"]), timeout=10) as smtp:
            if use_tls and not use_ssl:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpSendError(str(exc)) from exc


async def send_email(
    cfg: dict[str, Any], to_address: str, subject: str, text_body: str, html_body: str | None = None
) -> None:
    """Send one email via the given SMTP config (host/port/username/password/
    from_address/use_tls/use_ssl). Raises SmtpSendError on failure."""
    await to_thread(_send_sync, cfg, to_address, subject, text_body, html_body)
