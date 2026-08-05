from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Campaign, CampaignStep, Lead
from app.schemas import CampaignCreate, CampaignOut, EnrollRequest, RunResult
from app.services import scheduler

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _get_campaign(session: Session, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


@router.get("", response_model=list[CampaignOut])
def list_campaigns(session: Session = Depends(get_session)) -> list[Campaign]:
    return list(session.scalars(select(Campaign).order_by(Campaign.id)))


@router.post("", response_model=CampaignOut, status_code=201)
def create_campaign(payload: CampaignCreate, session: Session = Depends(get_session)) -> Campaign:
    if session.scalar(select(Campaign).where(Campaign.name == payload.name)):
        raise HTTPException(status_code=409, detail="a campaign with that name already exists")

    campaign = Campaign(name=payload.name, goal=payload.goal)
    session.add(campaign)
    session.flush()
    for position, step in enumerate(payload.steps, start=1):
        session.add(
            CampaignStep(
                campaign_id=campaign.id,
                position=position,
                day_offset=step.day_offset,
                channel=step.channel,
                prompt=step.prompt,
            )
        )
    session.flush()
    session.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/enroll", response_model=CampaignOut)
def enroll_leads(
    campaign_id: int,
    payload: EnrollRequest,
    session: Session = Depends(get_session),
) -> Campaign:
    campaign = _get_campaign(session, campaign_id)
    for lead_id in payload.lead_ids:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")
        scheduler.enroll(session, campaign, lead)
    return campaign


@router.post("/run", response_model=RunResult)
def run_due(session: Session = Depends(get_session)) -> RunResult:
    """Execute every campaign step that is currently due (call this from cron/a worker)."""
    executed, blocked, details = scheduler.run_due_steps(session)
    return RunResult(executed=executed, blocked=blocked, details=details)
