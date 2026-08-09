"""Compliance guardrails for outbound contact.

Every outbound call or text must pass through :func:`check_contact_allowed`, which
enforces DNC membership, revoked consent, per-lead calling-hour windows and attempt
caps, and writes an audit record for the decision either way.

This is an engineering control, not legal advice: an agency still owns its own
TCPA/state-law review, DNC scrubbing subscription, and licensing obligations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AuditLog,
    Channel,
    Consent,
    ConsentKind,
    DoNotContact,
    Lead,
    LeadStatus,
)

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "remove me"}
OPT_IN_KEYWORDS = {"start", "unstop", "yes please contact me"}
HELP_KEYWORDS = {"help", "info"}

STATE_TIMEZONES = {
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/New_York",
    "NH": "America/New_York", "NJ": "America/New_York", "NY": "America/New_York",
    "NC": "America/New_York", "OH": "America/New_York", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "VT": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "IN": "America/New_York",
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/Chicago",
    "LA": "America/Chicago", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago",
    "OK": "America/Chicago", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "WI": "America/Chicago",
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Denver",
    "MT": "America/Denver", "NM": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver",
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}

# States with a calling window stricter than the federal 8am-9pm rule.
STRICTER_CALL_WINDOWS = {
    "FL": (8, 20),
    "MS": (8, 20),
    "AL": (8, 20),
}


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def normalize_phone(raw: str) -> str:
    """Return an E.164 phone number, assuming US when no country code is given."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def lead_timezone(lead: Lead) -> ZoneInfo:
    for candidate in (lead.timezone, STATE_TIMEZONES.get(lead.state.upper(), "")):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    return ZoneInfo("America/New_York")


def call_window_for(lead: Lead) -> tuple[int, int]:
    settings = get_settings()
    default = (settings.call_window_start_hour, settings.call_window_end_hour)
    state_window = STRICTER_CALL_WINDOWS.get(lead.state.upper())
    if state_window is None:
        return default
    return (max(default[0], state_window[0]), min(default[1], state_window[1]))


def within_calling_hours(lead: Lead, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(lead_timezone(lead))
    start, end = call_window_for(lead)
    return start <= local.hour < end


def is_on_dnc(session: Session, phone: str) -> bool:
    normalized = normalize_phone(phone)
    return session.scalar(select(DoNotContact).where(DoNotContact.phone == normalized)) is not None


def latest_consent(session: Session, lead: Lead, channel: Channel) -> Consent | None:
    return session.scalar(
        select(Consent)
        .where(Consent.lead_id == lead.id, Consent.channel == channel)
        .order_by(Consent.created_at.desc(), Consent.id.desc())
    )


def has_consent(session: Session, lead: Lead, channel: Channel) -> bool:
    consent = latest_consent(session, lead, channel)
    return consent is not None and consent.kind is ConsentKind.granted


def record_audit(
    session: Session,
    *,
    lead: Lead | None,
    event: str,
    channel: Channel | None = None,
    allowed: bool | None = None,
    detail: str = "",
) -> AuditLog:
    entry = AuditLog(
        lead_id=lead.id if lead else None,
        event=event,
        channel=channel,
        allowed=allowed,
        detail=detail,
    )
    session.add(entry)
    session.flush()
    return entry


def check_contact_allowed(
    session: Session,
    lead: Lead,
    channel: Channel,
    now: datetime | None = None,
    *,
    enforce_time_window: bool = True,
) -> ContactDecision:
    """Decide whether an outbound contact on ``channel`` may be made right now.

    ``enforce_time_window`` is relaxed only when replying to a message the lead just
    sent us, where the contact window does not apply.
    """
    settings = get_settings()
    decision: ContactDecision

    # Email is not restricted to calling hours, and needs an address rather than a number.
    if channel is Channel.email:
        enforce_time_window = False

    if lead.status is LeadStatus.do_not_contact:
        decision = ContactDecision(False, "lead marked do-not-contact")
    elif channel is Channel.email and not lead.email:
        decision = ContactDecision(False, "no email address on file")
    elif is_on_dnc(session, lead.phone):
        decision = ContactDecision(False, "phone is on the internal do-not-contact list")
    elif not has_consent(session, lead, channel):
        decision = ContactDecision(False, f"no active {channel.value} consent on file")
    elif lead.attempts >= settings.max_contact_attempts:
        decision = ContactDecision(False, f"attempt cap of {settings.max_contact_attempts} reached")
    elif enforce_time_window and not within_calling_hours(lead, now):
        start, end = call_window_for(lead)
        decision = ContactDecision(False, f"outside local contact window {start:02d}:00-{end:02d}:00")
    else:
        decision = ContactDecision(True)

    record_audit(
        session,
        lead=lead,
        event="contact_check",
        channel=channel,
        allowed=decision.allowed,
        detail=decision.reason,
    )
    return decision


def add_to_dnc(session: Session, phone: str, reason: str) -> DoNotContact:
    normalized = normalize_phone(phone)
    existing = session.scalar(select(DoNotContact).where(DoNotContact.phone == normalized))
    if existing:
        return existing
    entry = DoNotContact(phone=normalized, reason=reason)
    session.add(entry)
    session.flush()
    return entry


def grant_consent(session: Session, lead: Lead, channel: Channel, evidence: str) -> Consent:
    consent = Consent(lead_id=lead.id, channel=channel, kind=ConsentKind.granted, evidence=evidence)
    session.add(consent)
    session.flush()
    return consent


def revoke_consent(session: Session, lead: Lead, channel: Channel, evidence: str) -> Consent:
    consent = Consent(lead_id=lead.id, channel=channel, kind=ConsentKind.revoked, evidence=evidence)
    session.add(consent)
    session.flush()
    return consent


def find_by_unsubscribe_token(session: Session, token: str) -> Lead | None:
    if not token:
        return None
    return session.scalar(select(Lead).where(Lead.unsubscribe_token == token))


def handle_email_unsubscribe(session: Session, lead: Lead, evidence: str) -> None:
    """Revoke email consent only; an email unsubscribe says nothing about phone consent."""
    revoke_consent(session, lead, Channel.email, evidence)
    record_audit(session, lead=lead, event="email_unsubscribe", channel=Channel.email, detail=evidence)


def classify_keyword(body: str) -> str | None:
    """Return 'opt_out', 'opt_in', 'help' or None for a inbound message body."""
    text = body.strip().lower().strip(".!? ")
    if text in OPT_OUT_KEYWORDS:
        return "opt_out"
    if text in OPT_IN_KEYWORDS:
        return "opt_in"
    if text in HELP_KEYWORDS:
        return "help"
    return None


def handle_opt_out(session: Session, lead: Lead, evidence: str) -> None:
    """Revoke consent on every channel and put the lead beyond further outreach."""
    for channel in Channel:
        revoke_consent(session, lead, channel, evidence)
    add_to_dnc(session, lead.phone, reason=evidence)
    lead.status = LeadStatus.do_not_contact
    lead.next_action_at = None
    record_audit(session, lead=lead, event="opt_out", detail=evidence)
