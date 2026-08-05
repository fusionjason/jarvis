"""Twilio wrapper for placing calls and sending texts.

When Twilio is not configured the client runs in dry-run mode: outbound sends are
recorded in the database with a synthetic SID and a ``dry-run`` status, so the whole
pipeline can be exercised locally without spending money or touching a real phone.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    sid: str
    status: str
    dry_run: bool = False


def _client() -> Client | None:
    settings = get_settings()
    if not settings.twilio_configured:
        return None
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def send_sms(to: str, body: str) -> SendResult:
    settings = get_settings()
    client = _client()
    if client is None:
        logger.warning("Twilio not configured; dry-run SMS to %s: %s", to, body)
        return SendResult(sid=f"dry-{uuid.uuid4().hex[:16]}", status="dry-run", dry_run=True)

    message = client.messages.create(
        to=to,
        from_=settings.twilio_phone_number,
        body=body,
        status_callback=f"{settings.public_base_url}/webhooks/sms/status",
    )
    return SendResult(sid=message.sid, status=message.status or "queued")


def place_call(to: str, lead_id: int) -> SendResult:
    """Dial a lead and connect them to the realtime voice agent."""
    settings = get_settings()
    client = _client()
    if client is None:
        logger.warning("Twilio not configured; dry-run call to %s", to)
        return SendResult(sid=f"dry-{uuid.uuid4().hex[:16]}", status="dry-run", dry_run=True)

    call = client.calls.create(
        to=to,
        from_=settings.twilio_phone_number,
        url=f"{settings.public_base_url}/webhooks/voice/answer?lead_id={lead_id}",
        status_callback=f"{settings.public_base_url}/webhooks/voice/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        record=settings.record_calls,
        recording_status_callback=f"{settings.public_base_url}/webhooks/voice/recording",
        machine_detection="Enable",
    )
    return SendResult(sid=call.sid, status=call.status or "queued")


def validate_signature(url: str, params: dict[str, str], signature: str) -> bool:
    """Verify a Twilio webhook signature; skipped when validation is disabled."""
    settings = get_settings()
    if not settings.twilio_validate_signatures:
        return True
    if not settings.twilio_auth_token:
        return False
    return RequestValidator(settings.twilio_auth_token).validate(url, params, signature)
