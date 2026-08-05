from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Campaign, CampaignStep, Channel, Direction, Lead, LeadStatus, Message
from app.services import compliance, conversation, scheduler


def test_send_text_records_message_and_bumps_attempts(session: Session, consented_lead: Lead) -> None:
    message = conversation.send_text(session, consented_lead, "Hi Maria", force=True)

    assert message.direction is Direction.outbound
    assert message.status == "dry-run"  # Twilio not configured in tests
    assert consented_lead.attempts == 1
    assert consented_lead.status is LeadStatus.contacted


def test_send_text_blocked_without_consent(session: Session) -> None:
    lead = Lead(first_name="No", phone="+15551230010", state="TX")
    session.add(lead)
    session.flush()

    with pytest.raises(conversation.ContactBlocked):
        conversation.send_text(session, lead, "hello")
    assert session.query(Message).count() == 0


def test_inbound_stop_opts_out_and_sends_no_reply(session: Session, consented_lead: Lead) -> None:
    reply = conversation.handle_inbound_text(session, consented_lead, "STOP")

    assert reply is None
    assert consented_lead.status is LeadStatus.do_not_contact
    assert compliance.is_on_dnc(session, consented_lead.phone)


def test_inbound_help_returns_disclosure(session: Session, consented_lead: Lead) -> None:
    reply = conversation.handle_inbound_text(session, consented_lead, "HELP")

    assert reply is not None
    assert "STOP" in reply
    assert "AI assistant" in reply


def test_inbound_message_gets_assistant_reply(session: Session, consented_lead: Lead) -> None:
    reply = conversation.handle_inbound_text(session, consented_lead, "yes tell me more")

    assert reply
    bodies = [m.body for m in session.query(Message).order_by(Message.id).all()]
    assert bodies[0] == "yes tell me more"
    assert bodies[-1] == reply


def test_finalize_call_stores_transcript(session: Session, consented_lead: Lead) -> None:
    call = conversation.start_call(session, consented_lead, force=True)
    conversation.finalize_call(session, call, "assistant: hi\ncaller: sounds good")

    assert "sounds good" in call.transcript
    assert call.summary


def _campaign_with_sms_step(session: Session) -> Campaign:
    campaign = Campaign(name="cadence")
    session.add(campaign)
    session.flush()
    session.add(CampaignStep(campaign_id=campaign.id, position=1, day_offset=0, channel=Channel.sms))
    session.add(CampaignStep(campaign_id=campaign.id, position=2, day_offset=2, channel=Channel.voice))
    session.flush()
    session.refresh(campaign)
    return campaign


def test_scheduler_runs_due_step_and_advances(session: Session, consented_lead: Lead) -> None:
    campaign = _campaign_with_sms_step(session)
    enrollment = scheduler.enroll(session, campaign, consented_lead)
    noon_central = datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc)
    enrollment.due_at = noon_central

    executed, blocked, _ = scheduler.run_due_steps(session, noon_central)

    assert (executed, blocked) == (1, 0)
    assert enrollment.next_step_position == 2
    assert enrollment.due_at == noon_central + timedelta(days=2)


def test_scheduler_defers_blocked_step_by_an_hour(session: Session, consented_lead: Lead) -> None:
    campaign = _campaign_with_sms_step(session)
    enrollment = scheduler.enroll(session, campaign, consented_lead)
    three_am_central = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    enrollment.due_at = three_am_central

    executed, blocked, details = scheduler.run_due_steps(session, three_am_central)

    assert (executed, blocked) == (0, 1)
    assert "contact window" in details[0]
    assert enrollment.next_step_position == 1
    assert enrollment.due_at == three_am_central + timedelta(hours=1)


def test_scheduler_completes_enrollment_for_opted_out_lead(
    session: Session, consented_lead: Lead
) -> None:
    campaign = _campaign_with_sms_step(session)
    enrollment = scheduler.enroll(session, campaign, consented_lead)
    now = datetime(2026, 3, 10, 17, 0, tzinfo=timezone.utc)
    enrollment.due_at = now
    compliance.handle_opt_out(session, consented_lead, evidence="test")

    scheduler.run_due_steps(session, now)

    assert enrollment.completed
