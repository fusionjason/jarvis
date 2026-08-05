from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Channel, Lead, LeadStatus
from app.services import compliance


def test_normalize_phone_variants() -> None:
    assert compliance.normalize_phone("(555) 123-0001") == "+15551230001"
    assert compliance.normalize_phone("15551230001") == "+15551230001"
    assert compliance.normalize_phone("+44 20 7946 0958") == "+442079460958"


def test_contact_blocked_without_consent(session: Session) -> None:
    lead = Lead(first_name="No", last_name="Consent", phone="+15551230009", state="TX")
    session.add(lead)
    session.flush()

    decision = compliance.check_contact_allowed(session, lead, Channel.sms)
    assert not decision
    assert "consent" in decision.reason


def test_contact_allowed_with_consent_inside_window(session: Session, consented_lead: Lead) -> None:
    noon_central = datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc)  # 12:00 in America/Chicago
    assert compliance.check_contact_allowed(session, consented_lead, Channel.sms, noon_central)


def test_contact_blocked_outside_calling_window(session: Session, consented_lead: Lead) -> None:
    six_am_central = datetime(2026, 3, 10, 11, 0, tzinfo=timezone.utc)
    decision = compliance.check_contact_allowed(session, consented_lead, Channel.sms, six_am_central)
    assert not decision
    assert "contact window" in decision.reason


def test_florida_uses_stricter_evening_cutoff(session: Session) -> None:
    lead = Lead(first_name="James", phone="+15551230002", state="FL", timezone="America/New_York")
    session.add(lead)
    session.flush()
    compliance.grant_consent(session, lead, Channel.voice, evidence="test")

    assert compliance.call_window_for(lead) == (8, 20)
    eight_thirty_pm_et = datetime(2026, 3, 11, 0, 30, tzinfo=timezone.utc)
    decision = compliance.check_contact_allowed(session, lead, Channel.voice, eight_thirty_pm_et)
    assert not decision


def test_attempt_cap_blocks_further_outreach(session: Session, consented_lead: Lead) -> None:
    consented_lead.attempts = 99
    noon_central = datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc)
    decision = compliance.check_contact_allowed(session, consented_lead, Channel.sms, noon_central)
    assert not decision
    assert "attempt cap" in decision.reason


@pytest.mark.parametrize("body", ["STOP", "stop.", " Unsubscribe ", "quit"])
def test_opt_out_keywords_are_recognized(body: str) -> None:
    assert compliance.classify_keyword(body) == "opt_out"


def test_handle_opt_out_revokes_everything(session: Session, consented_lead: Lead) -> None:
    compliance.handle_opt_out(session, consented_lead, evidence="inbound SMS: STOP")

    assert consented_lead.status is LeadStatus.do_not_contact
    assert compliance.is_on_dnc(session, consented_lead.phone)
    assert not compliance.has_consent(session, consented_lead, Channel.sms)
    assert not compliance.has_consent(session, consented_lead, Channel.voice)
    assert not compliance.check_contact_allowed(session, consented_lead, Channel.sms)


def test_every_contact_check_is_audited(session: Session, consented_lead: Lead) -> None:
    compliance.check_contact_allowed(session, consented_lead, Channel.sms)
    entries = [entry for entry in session.query(compliance.AuditLog).all() if entry.event == "contact_check"]
    assert len(entries) == 1
    assert entries[0].lead_id == consented_lead.id
