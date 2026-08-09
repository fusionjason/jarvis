"""Orchestration between compliance, the LLM, and the telephony provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Call, Channel, Direction, Lead, LeadStatus, Message
from app.services import compliance, llm, mailer, telephony


class ContactBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def get_lead_by_phone(session: Session, phone: str) -> Lead | None:
    normalized = compliance.normalize_phone(phone)
    return session.scalar(select(Lead).where(Lead.phone == normalized))


def conversation_history(session: Session, lead: Lead, limit: int = 20) -> list[tuple[str, str]]:
    messages = session.scalars(
        select(Message).where(Message.lead_id == lead.id).order_by(Message.id.desc()).limit(limit)
    ).all()
    return [(m.direction.value, m.body) for m in reversed(messages)]


def send_text(
    session: Session, lead: Lead, body: str, *, force: bool = False, deliver: bool = True
) -> Message:
    """Send an outbound text after a compliance check (unless ``force``).

    ``deliver=False`` records the message without calling Twilio, for replies that are
    handed back to Twilio inline as TwiML.
    """
    if not force:
        decision = compliance.check_contact_allowed(session, lead, Channel.sms)
        if not decision:
            raise ContactBlocked(decision.reason)

    result = telephony.send_sms(lead.phone, body) if deliver else telephony.SendResult("twiml", "twiml")
    message = Message(
        lead_id=lead.id,
        channel=Channel.sms,
        direction=Direction.outbound,
        body=body,
        provider_sid=result.sid,
        status=result.status,
    )
    session.add(message)
    lead.attempts += 1
    lead.last_contacted_at = datetime.now(timezone.utc)
    if lead.status is LeadStatus.new:
        lead.status = LeadStatus.contacted
    compliance.record_audit(
        session, lead=lead, event="sms_sent", channel=Channel.sms, allowed=True, detail=body[:400]
    )
    session.flush()
    return message


def send_email(
    session: Session,
    lead: Lead,
    subject: str = "",
    body: str = "",
    *,
    force: bool = False,
) -> Message:
    """Send an outbound email, letting the assistant draft it when subject/body are empty."""
    if not force:
        decision = compliance.check_contact_allowed(session, lead, Channel.email)
        if not decision:
            raise ContactBlocked(decision.reason)

    if not subject or not body:
        draft = llm.draft_email(
            lead.full_name,
            lead.coverage_interest,
            conversation_history(session, lead),
            goal=f"Book a short call with {get_settings().licensed_agent_name}",
        )
        subject = subject or draft.subject
        body = body or draft.body

    result = mailer.send_email(
        to_email=lead.email,
        to_name=lead.full_name,
        subject=subject,
        body=body,
        unsubscribe_token=lead.ensure_unsubscribe_token(),
    )
    message = Message(
        lead_id=lead.id,
        channel=Channel.email,
        direction=Direction.outbound,
        subject=subject,
        body=body,
        provider_sid=result.message_id,
        status=result.status,
    )
    session.add(message)
    lead.attempts += 1
    lead.last_contacted_at = datetime.now(timezone.utc)
    if lead.status is LeadStatus.new:
        lead.status = LeadStatus.contacted
    compliance.record_audit(
        session, lead=lead, event="email_sent", channel=Channel.email, allowed=True, detail=subject[:400]
    )
    session.flush()
    return message


def start_call(session: Session, lead: Lead, *, force: bool = False) -> Call:
    if not force:
        decision = compliance.check_contact_allowed(session, lead, Channel.voice)
        if not decision:
            raise ContactBlocked(decision.reason)

    result = telephony.place_call(lead.phone, lead.id)
    call = Call(
        lead_id=lead.id,
        direction=Direction.outbound,
        provider_sid=result.sid,
        status=result.status,
    )
    session.add(call)
    lead.attempts += 1
    lead.last_contacted_at = datetime.now(timezone.utc)
    if lead.status is LeadStatus.new:
        lead.status = LeadStatus.contacted
    compliance.record_audit(
        session, lead=lead, event="call_placed", channel=Channel.voice, allowed=True, detail=result.sid
    )
    session.flush()
    return call


def record_inbound_text(session: Session, lead: Lead, body: str, sid: str = "") -> Message:
    message = Message(
        lead_id=lead.id,
        channel=Channel.sms,
        direction=Direction.inbound,
        body=body,
        provider_sid=sid,
    )
    session.add(message)
    if lead.status in (LeadStatus.new, LeadStatus.contacted):
        lead.status = LeadStatus.engaged
    session.flush()
    return message


def handle_inbound_text(
    session: Session, lead: Lead, body: str, sid: str = "", *, deliver: bool = False
) -> str | None:
    """Process an inbound text and return the reply body, if the assistant should reply.

    Opt-out and HELP keywords are answered deterministically and never reach the LLM.
    The reply is returned for the caller to deliver as TwiML unless ``deliver`` is set.
    """
    settings = get_settings()
    record_inbound_text(session, lead, body, sid)
    keyword = compliance.classify_keyword(body)

    if keyword == "opt_out":
        compliance.handle_opt_out(session, lead, evidence=f"inbound SMS: {body.strip()[:100]}")
        return None  # Twilio's Advanced Opt-Out sends the confirmation itself.

    if keyword == "help":
        help_text = (
            f"{settings.assistant_name} here, an AI assistant for {settings.agency_name}. "
            f"Call {settings.twilio_phone_number or 'our office'} to reach a licensed agent. "
            "Reply STOP to opt out."
        )
        send_text(session, lead, help_text, force=True, deliver=deliver)
        return help_text

    if keyword == "opt_in":
        compliance.grant_consent(session, lead, Channel.sms, evidence=f"inbound SMS: {body.strip()[:100]}")
        lead.status = LeadStatus.engaged

    # A reply to a message the lead just sent is not cold outreach, so the local
    # contact window does not gate it - consent and DNC still do.
    decision = compliance.check_contact_allowed(session, lead, Channel.sms, enforce_time_window=False)
    if not decision:
        return None

    reply = llm.draft_sms_reply(
        lead.full_name,
        conversation_history(session, lead),
        goal=f"Book a short call with {settings.licensed_agent_name}",
    )
    send_text(session, lead, reply, force=True, deliver=deliver)
    return reply


def finalize_call(session: Session, call: Call, transcript: str) -> Call:
    """Attach the transcript, summary and disposition once a call ends."""
    call.transcript = transcript
    analysis = llm.analyze_call(transcript)
    call.summary = analysis.summary
    call.disposition = analysis.disposition

    lead = call.lead
    if analysis.disposition == "do_not_contact":
        compliance.handle_opt_out(session, lead, evidence="verbal opt-out on call")
    elif analysis.disposition == "not_interested":
        lead.status = LeadStatus.not_interested
    elif analysis.disposition == "appointment_set":
        lead.status = LeadStatus.appointment_set
    elif analysis.disposition in ("interested", "callback_requested"):
        lead.status = LeadStatus.engaged
        lead.next_action_at = datetime.now(timezone.utc) + timedelta(days=1)

    compliance.record_audit(
        session,
        lead=lead,
        event="call_completed",
        channel=Channel.voice,
        allowed=True,
        detail=f"{analysis.disposition}: {analysis.next_step}"[:400],
    )
    session.flush()
    return call
