"""Cadence engine: walks due enrollments and performs the next campaign step."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Campaign, CampaignStep, Channel, Enrollment, Lead, LeadStatus
from app.services import compliance, conversation, llm

TERMINAL_STATUSES = {LeadStatus.do_not_contact, LeadStatus.not_interested, LeadStatus.sold}


def enroll(session: Session, campaign: Campaign, lead: Lead) -> Enrollment:
    existing = session.scalar(
        select(Enrollment).where(
            Enrollment.campaign_id == campaign.id, Enrollment.lead_id == lead.id
        )
    )
    if existing:
        return existing
    first_step = campaign.steps[0] if campaign.steps else None
    due = datetime.now(timezone.utc) + timedelta(days=first_step.day_offset if first_step else 0)
    enrollment = Enrollment(campaign_id=campaign.id, lead_id=lead.id, due_at=due)
    session.add(enrollment)
    session.flush()
    return enrollment


def due_enrollments(session: Session, now: datetime | None = None) -> list[Enrollment]:
    now = now or datetime.now(timezone.utc)
    return list(
        session.scalars(
            select(Enrollment)
            .join(Campaign)
            .where(
                Enrollment.completed.is_(False),
                Enrollment.paused.is_(False),
                Enrollment.due_at <= now,
                Campaign.active.is_(True),
            )
        )
    )


def _step_for(campaign: Campaign, position: int) -> CampaignStep | None:
    for step in campaign.steps:
        if step.position == position:
            return step
    return None


def run_due_steps(session: Session, now: datetime | None = None) -> tuple[int, int, list[str]]:
    """Execute every due step. Returns (executed, blocked, human-readable details)."""
    now = now or datetime.now(timezone.utc)
    session.flush()
    executed = 0
    blocked = 0
    details: list[str] = []

    for enrollment in due_enrollments(session, now):
        lead = enrollment.lead
        campaign = enrollment.campaign
        step = _step_for(campaign, enrollment.next_step_position)

        if step is None or lead.status in TERMINAL_STATUSES:
            enrollment.completed = True
            continue

        decision = compliance.check_contact_allowed(session, lead, step.channel, now)
        if not decision:
            blocked += 1
            details.append(f"lead {lead.id}: blocked - {decision.reason}")
            # Retry the same step in an hour; the window or cap may have changed by then.
            enrollment.due_at = now + timedelta(hours=1)
            continue

        try:
            if step.channel is Channel.sms:
                body = llm.draft_sms_reply(
                    lead.full_name,
                    conversation.conversation_history(session, lead),
                    goal=step.prompt or campaign.goal,
                )
                conversation.send_text(session, lead, body, force=True)
                details.append(f"lead {lead.id}: texted")
            elif step.channel is Channel.email:
                draft = llm.draft_email(
                    lead.full_name,
                    lead.coverage_interest,
                    conversation.conversation_history(session, lead),
                    goal=step.prompt or campaign.goal,
                )
                conversation.send_email(session, lead, draft.subject, draft.body, force=True)
                details.append(f"lead {lead.id}: emailed")
            else:
                conversation.start_call(session, lead, force=True)
                details.append(f"lead {lead.id}: called")
            executed += 1
        except conversation.ContactBlocked as exc:
            blocked += 1
            details.append(f"lead {lead.id}: blocked - {exc.reason}")
            continue

        next_step = _step_for(campaign, enrollment.next_step_position + 1)
        if next_step is None:
            enrollment.completed = True
        else:
            enrollment.next_step_position += 1
            enrollment.due_at = now + timedelta(days=max(next_step.day_offset - step.day_offset, 0))

    session.flush()
    return executed, blocked, details
