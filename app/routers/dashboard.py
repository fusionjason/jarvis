from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import AuditLog, Call, Campaign, Channel, Lead, LeadStatus, Message
from app.services import compliance, conversation, scheduler

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _chrome() -> dict[str, object]:
    """Context every page needs: settings plus the provider status badges."""
    settings = get_settings()
    return {
        "settings": settings,
        "twilio_ready": settings.twilio_configured,
        "smtp_ready": settings.smtp_configured,
        "openai_ready": settings.openai_configured,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    leads = list(session.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(200)))
    counts: dict[LeadStatus, int] = {
        status: count
        for status, count in session.execute(
            select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
        ).all()
    }
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            **_chrome(),
            "leads": leads,
            "counts": counts,
            "statuses": list(LeadStatus),
            "campaigns": list(session.scalars(select(Campaign).order_by(Campaign.id))),
        },
    )


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    lead_id: int, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    messages = list(session.scalars(select(Message).where(Message.lead_id == lead.id).order_by(Message.id)))
    calls = list(session.scalars(select(Call).where(Call.lead_id == lead.id).order_by(Call.id.desc())))
    audits = list(
        session.scalars(
            select(AuditLog).where(AuditLog.lead_id == lead.id).order_by(AuditLog.id.desc()).limit(50)
        )
    )
    sms_decision = compliance.check_contact_allowed(session, lead, Channel.sms)
    email_decision = compliance.check_contact_allowed(session, lead, Channel.email)
    voice_decision = compliance.check_contact_allowed(session, lead, Channel.voice)
    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            **_chrome(),
            "lead": lead,
            "messages": messages,
            "calls": calls,
            "audits": audits,
            "sms_decision": sms_decision,
            "email_decision": email_decision,
            "voice_decision": voice_decision,
        },
    )


@router.post("/leads/{lead_id}/text")
def dashboard_text(
    lead_id: int,
    body: str = Form(default=""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    text = body.strip() or conversation.llm.draft_sms_reply(
        lead.full_name,
        conversation.conversation_history(session, lead),
        goal="Book a short call with a licensed agent",
    )
    try:
        conversation.send_text(session, lead, text)
    except conversation.ContactBlocked as exc:
        return RedirectResponse(f"/leads/{lead_id}?error={exc.reason}", status_code=303)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.post("/leads/{lead_id}/email")
def dashboard_email(
    lead_id: int,
    subject: str = Form(default=""),
    body: str = Form(default=""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    try:
        conversation.send_email(session, lead, subject.strip(), body.strip())
    except conversation.ContactBlocked as exc:
        return RedirectResponse(f"/leads/{lead_id}?error={exc.reason}", status_code=303)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.post("/leads/{lead_id}/call")
def dashboard_call(lead_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    try:
        conversation.start_call(session, lead)
    except conversation.ContactBlocked as exc:
        return RedirectResponse(f"/leads/{lead_id}?error={exc.reason}", status_code=303)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.post("/leads/{lead_id}/opt-out")
def dashboard_opt_out(lead_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    compliance.handle_opt_out(session, lead, evidence="manual opt-out from dashboard")
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(token: str, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """One-click email unsubscribe target (also used by List-Unsubscribe-Post)."""
    lead = compliance.find_by_unsubscribe_token(session, token)
    if lead is not None:
        compliance.handle_email_unsubscribe(session, lead, evidence=f"one-click unsubscribe: {token[:8]}")
    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {**_chrome(), "found": lead is not None},
        status_code=200 if lead is not None else 404,
    )


@router.post("/campaigns/run")
def dashboard_run_campaigns(session: Session = Depends(get_session)) -> RedirectResponse:
    executed, blocked, _ = scheduler.run_due_steps(session)
    return RedirectResponse(f"/?executed={executed}&blocked={blocked}", status_code=303)
