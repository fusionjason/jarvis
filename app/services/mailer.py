"""SMTP email sender.

Works with any SMTP provider (SendGrid, Postmark, Mailgun, SES, Google Workspace).
When SMTP is not configured it runs in dry-run mode, mirroring the telephony client:
the message is recorded in the database but nothing leaves the box.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailResult:
    message_id: str
    status: str
    dry_run: bool = False


def unsubscribe_url(token: str) -> str:
    settings = get_settings()
    return f"{settings.public_base_url}/unsubscribe/{token}"


def build_message(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    unsubscribe_token: str,
) -> EmailMessage:
    """Assemble an email with the disclosure footer and one-click unsubscribe headers."""
    settings = get_settings()
    link = unsubscribe_url(unsubscribe_token)
    footer = (
        f"\n\n--\n{settings.assistant_name}, automated assistant for {settings.agency_name}\n"
        f"{settings.agency_mailing_address}\n"
        f"Unsubscribe: {link}\n"
    )

    message = EmailMessage()
    message["Subject"] = subject
    sender = f"{settings.assistant_name} at {settings.agency_name}"
    message["From"] = formataddr((sender, settings.from_email))
    message["To"] = formataddr((to_name, to_email)) if to_name else to_email
    if settings.reply_to_email:
        message["Reply-To"] = settings.reply_to_email
    # RFC 8058 one-click unsubscribe, honoured by Gmail and Outlook.
    message["List-Unsubscribe"] = f"<{link}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["Message-ID"] = make_msgid(domain=settings.from_email.split("@")[-1] or None)
    message.set_content(body.rstrip() + footer)
    return message


def send_email(
    *, to_email: str, to_name: str, subject: str, body: str, unsubscribe_token: str
) -> EmailResult:
    settings = get_settings()
    message = build_message(
        to_email=to_email,
        to_name=to_name,
        subject=subject,
        body=body,
        unsubscribe_token=unsubscribe_token,
    )

    if not settings.smtp_configured:
        logger.warning("SMTP not configured; dry-run email to %s: %s", to_email, subject)
        return EmailResult(message_id=f"dry-{uuid.uuid4().hex[:16]}", status="dry-run", dry_run=True)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return EmailResult(message_id=str(message["Message-ID"]), status="sent")
