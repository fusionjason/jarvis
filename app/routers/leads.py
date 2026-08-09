from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Channel, Lead, LeadStatus
from app.schemas import (
    CallOut,
    ImportResult,
    LeadCreate,
    LeadOut,
    LeadUpdate,
    MessageOut,
    SendEmailRequest,
    SendTextRequest,
)
from app.services import compliance, conversation

router = APIRouter(prefix="/api/leads", tags=["leads"])


def _get_lead(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.get("", response_model=list[LeadOut])
def list_leads(
    status: LeadStatus | None = None,
    session: Session = Depends(get_session),
) -> list[Lead]:
    query = select(Lead).order_by(Lead.created_at.desc())
    if status is not None:
        query = query.where(Lead.status == status)
    return list(session.scalars(query))


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(payload: LeadCreate, session: Session = Depends(get_session)) -> Lead:
    phone = compliance.normalize_phone(payload.phone)
    if session.scalar(select(Lead).where(Lead.phone == phone)):
        raise HTTPException(status_code=409, detail="a lead with that phone number already exists")

    lead = Lead(**payload.model_dump(exclude={"phone", "consent_evidence"}), phone=phone)
    session.add(lead)
    session.flush()
    if payload.consent_evidence:
        for channel in Channel:
            compliance.grant_consent(session, lead, channel, payload.consent_evidence)
    compliance.record_audit(session, lead=lead, event="lead_created", detail=payload.source)
    return lead


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: int, session: Session = Depends(get_session)) -> Lead:
    return _get_lead(session, lead_id)


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: int, payload: LeadUpdate, session: Session = Depends(get_session)) -> Lead:
    lead = _get_lead(session, lead_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    return lead


@router.get("/{lead_id}/messages", response_model=list[MessageOut])
def lead_messages(lead_id: int, session: Session = Depends(get_session)) -> list[object]:
    return list(_get_lead(session, lead_id).messages)


@router.get("/{lead_id}/calls", response_model=list[CallOut])
def lead_calls(lead_id: int, session: Session = Depends(get_session)) -> list[object]:
    return list(_get_lead(session, lead_id).calls)


@router.post("/{lead_id}/text", response_model=MessageOut, status_code=201)
def text_lead(
    lead_id: int,
    payload: SendTextRequest,
    session: Session = Depends(get_session),
) -> object:
    lead = _get_lead(session, lead_id)
    body = payload.body or conversation.llm.draft_sms_reply(
        lead.full_name,
        conversation.conversation_history(session, lead),
        goal="Book a short call with a licensed agent",
    )
    try:
        return conversation.send_text(session, lead, body)
    except conversation.ContactBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc


@router.post("/{lead_id}/email", response_model=MessageOut, status_code=201)
def email_lead(
    lead_id: int,
    payload: SendEmailRequest,
    session: Session = Depends(get_session),
) -> object:
    lead = _get_lead(session, lead_id)
    try:
        return conversation.send_email(session, lead, payload.subject, payload.body)
    except conversation.ContactBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc


@router.post("/{lead_id}/call", response_model=CallOut, status_code=201)
def call_lead(lead_id: int, session: Session = Depends(get_session)) -> object:
    lead = _get_lead(session, lead_id)
    try:
        return conversation.start_call(session, lead)
    except conversation.ContactBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc


@router.post("/{lead_id}/opt-out", response_model=LeadOut)
def opt_out(lead_id: int, session: Session = Depends(get_session)) -> Lead:
    lead = _get_lead(session, lead_id)
    compliance.handle_opt_out(session, lead, evidence="manual opt-out from dashboard")
    return lead


@router.post("/import", response_model=ImportResult)
async def import_csv(file: UploadFile, session: Session = Depends(get_session)) -> ImportResult:
    """Import leads from a CSV with first_name, last_name, phone, state, consent_evidence columns."""
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    created = 0
    skipped: list[str] = []

    for row in reader:
        phone_raw = (row.get("phone") or "").strip()
        if not phone_raw:
            continue
        phone = compliance.normalize_phone(phone_raw)
        if session.scalar(select(Lead).where(Lead.phone == phone)):
            skipped.append(phone)
            continue
        state = (row.get("state") or "").strip().upper()[:2]
        lead = Lead(
            first_name=(row.get("first_name") or "").strip(),
            last_name=(row.get("last_name") or "").strip(),
            phone=phone,
            email=(row.get("email") or "").strip(),
            state=state,
            timezone=compliance.STATE_TIMEZONES.get(state, "America/New_York"),
            coverage_interest=(row.get("coverage_interest") or "").strip(),
            source=(row.get("source") or "csv_import").strip(),
        )
        session.add(lead)
        session.flush()
        evidence = (row.get("consent_evidence") or "").strip()
        if evidence:
            for channel in Channel:
                compliance.grant_consent(session, lead, channel, evidence)
        created += 1

    return ImportResult(created=created, skipped=skipped)
