from __future__ import annotations

from datetime import datetime, timezone

from app.models import Campaign, CampaignStep, Channel, Direction, Lead, LeadStatus
from app.services import compliance, conversation, mailer, scheduler


def _lead(session, **kwargs) -> Lead:
    lead = Lead(
        first_name="Dana",
        last_name="Whitfield",
        phone=kwargs.pop("phone", "+15551234321"),
        email=kwargs.pop("email", "dana@example.com"),
        state="TX",
        timezone="America/Chicago",
        coverage_interest="term life, $300k",
        **kwargs,
    )
    session.add(lead)
    session.flush()
    for channel in Channel:
        compliance.grant_consent(session, lead, channel, evidence="web form opt-in")
    return lead


def test_send_email_dry_runs_and_records_message(session) -> None:
    lead = _lead(session)
    message = conversation.send_email(session, lead)

    assert message.channel is Channel.email
    assert message.direction is Direction.outbound
    assert message.status == "dry-run"
    assert message.subject
    assert message.body
    assert lead.attempts == 1
    assert lead.status is LeadStatus.contacted
    assert lead.unsubscribe_token


def test_email_ignores_calling_window_but_not_consent(session) -> None:
    lead = _lead(session)
    middle_of_the_night = datetime(2026, 3, 4, 8, 0, tzinfo=timezone.utc)  # 02:00 in Chicago

    assert compliance.check_contact_allowed(session, lead, Channel.email, middle_of_the_night).allowed
    assert not compliance.check_contact_allowed(session, lead, Channel.voice, middle_of_the_night).allowed

    compliance.revoke_consent(session, lead, Channel.email, evidence="unsubscribed")
    decision = compliance.check_contact_allowed(session, lead, Channel.email, middle_of_the_night)
    assert not decision.allowed
    assert "consent" in decision.reason


def test_email_blocked_without_address(session) -> None:
    lead = _lead(session, email="")
    decision = compliance.check_contact_allowed(session, lead, Channel.email)
    assert not decision.allowed
    assert "email address" in decision.reason


def test_email_body_carries_disclosure_and_unsubscribe(session) -> None:
    lead = _lead(session)
    token = lead.ensure_unsubscribe_token()
    message = mailer.build_message(
        to_email=lead.email,
        to_name=lead.full_name,
        subject="Quick question",
        body="Hi Dana, are you still looking at coverage?",
        unsubscribe_token=token,
    )

    payload = message.get_content()
    assert "automated assistant" in payload
    assert f"/unsubscribe/{token}" in payload
    assert message["List-Unsubscribe"] == f"<{mailer.unsubscribe_url(token)}>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_one_click_unsubscribe_revokes_email_only(client, session) -> None:
    lead = _lead(session)
    conversation.send_email(session, lead)
    session.commit()
    token = lead.unsubscribe_token

    response = client.post(f"/unsubscribe/{token}")
    assert response.status_code == 200
    assert "unsubscribed" in response.text

    session.expire_all()
    assert not compliance.has_consent(session, lead, Channel.email)
    assert compliance.has_consent(session, lead, Channel.sms)
    assert lead.status is not LeadStatus.do_not_contact


def test_unknown_unsubscribe_token_is_404(client) -> None:
    assert client.get("/unsubscribe/not-a-real-token").status_code == 404


def test_scheduler_runs_email_step_outside_calling_hours(session) -> None:
    lead = _lead(session)
    campaign = Campaign(name="email cadence")
    session.add(campaign)
    session.flush()
    session.add(CampaignStep(campaign_id=campaign.id, position=1, day_offset=0, channel=Channel.email))
    session.flush()
    session.refresh(campaign)

    enrollment = scheduler.enroll(session, campaign, lead)
    three_am_central = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    enrollment.due_at = three_am_central

    executed, blocked, details = scheduler.run_due_steps(session, three_am_central)

    assert (executed, blocked) == (1, 0)
    assert "emailed" in details[0]


def test_api_email_endpoint_and_blocking(client, session) -> None:
    lead = _lead(session, phone="+15559998888", email="api@example.com")
    session.commit()

    created = client.post(f"/api/leads/{lead.id}/email", json={})
    assert created.status_code == 201
    assert created.json()["channel"] == "email"

    compliance.revoke_consent(session, lead, Channel.email, evidence="test")
    session.commit()
    blocked = client.post(f"/api/leads/{lead.id}/email", json={"subject": "Hi", "body": "There"})
    assert blocked.status_code == 409
