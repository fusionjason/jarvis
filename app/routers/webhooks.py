"""Twilio webhook endpoints for SMS and voice."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models import Call, Channel, Direction, Lead, Message
from app.services import compliance, conversation, speech, telephony
from app.services.realtime import MediaStreamBridge

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def verified_form(request: Request) -> dict[str, str]:
    form = {key: str(value) for key, value in (await request.form()).items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not telephony.validate_signature(str(request.url), form, signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


def _twiml(document: object) -> Response:
    return Response(content=str(document), media_type="application/xml")


def _speak(response: VoiceResponse, text: str) -> None:
    """Say `text` in the Google TTS voice, falling back to Twilio's own voice."""
    url = speech.clip_url(text)
    if url:
        response.play(url)
    else:
        response.say(text, voice=get_settings().twilio_say_voice)


@router.post("/sms/inbound")
async def sms_inbound(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    form = await verified_form(request)
    from_number = compliance.normalize_phone(form.get("From", ""))
    body = form.get("Body", "")

    lead = conversation.get_lead_by_phone(session, from_number)
    if lead is None:
        lead = Lead(first_name="", phone=from_number, source="inbound_sms")
        session.add(lead)
        session.flush()
        compliance.grant_consent(session, lead, Channel.sms, evidence="inbound text")

    reply = conversation.handle_inbound_text(session, lead, body, sid=form.get("MessageSid", ""))
    response = MessagingResponse()
    if reply:
        response.message(reply)
    return _twiml(response)


@router.post("/sms/status")
async def sms_status(request: Request, session: Session = Depends(get_session)) -> dict[str, str]:
    form = await verified_form(request)
    sid = form.get("MessageSid", "")
    message = session.scalar(select(Message).where(Message.provider_sid == sid))
    if message is not None:
        message.status = form.get("MessageStatus", message.status)
    return {"ok": "true"}


@router.post("/voice/answer")
async def voice_answer(request: Request, session: Session = Depends(get_session)) -> Response:
    """TwiML returned when the callee picks up: disclose, then open the media stream."""
    settings = get_settings()
    form = await verified_form(request)
    lead_id = request.query_params.get("lead_id", "")

    response = VoiceResponse()
    if form.get("AnsweredBy", "").startswith("machine"):
        _speak(response, speech.voicemail_text())
        response.hangup()
        return _twiml(response)

    if settings.record_calls:
        _speak(response, speech.disclosure_text())

    ws_base = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
    connect = Connect()
    connect.stream(url=f"{ws_base}/webhooks/voice/stream?lead_id={lead_id}")
    response.append(connect)

    call_sid = form.get("CallSid", "")
    if lead_id.isdigit() and call_sid:
        call = session.scalar(select(Call).where(Call.provider_sid == call_sid))
        if call is None:
            session.add(
                Call(
                    lead_id=int(lead_id),
                    direction=Direction.outbound,
                    provider_sid=call_sid,
                    status="in-progress",
                )
            )
    return _twiml(response)


@router.post("/voice/status")
async def voice_status(request: Request, session: Session = Depends(get_session)) -> dict[str, str]:
    form = await verified_form(request)
    call = session.scalar(select(Call).where(Call.provider_sid == form.get("CallSid", "")))
    if call is not None:
        call.status = form.get("CallStatus", call.status)
        call.duration_seconds = int(form.get("CallDuration") or 0)
    return {"ok": "true"}


@router.post("/voice/recording")
async def voice_recording(request: Request, session: Session = Depends(get_session)) -> dict[str, str]:
    form = await verified_form(request)
    call = session.scalar(select(Call).where(Call.provider_sid == form.get("CallSid", "")))
    if call is not None:
        call.recording_url = form.get("RecordingUrl", "")
    return {"ok": "true"}


@router.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    """Relay the live call audio to the realtime agent, then store the transcript."""
    await websocket.accept()
    lead_id_param = websocket.query_params.get("lead_id", "")
    lead_id = int(lead_id_param) if lead_id_param.isdigit() else None

    lead_name = ""
    if lead_id is not None:
        with SessionLocal() as session:
            lead = session.get(Lead, lead_id)
            lead_name = lead.full_name if lead else ""

    bridge = MediaStreamBridge(websocket, lead_name)
    try:
        transcript = await bridge.run()
    except Exception:
        logger.exception("Realtime bridge failed")
        await websocket.close()
        return

    if lead_id is not None and transcript.turns:
        with SessionLocal() as session:
            call = session.scalar(
                select(Call).where(Call.lead_id == lead_id).order_by(Call.id.desc())
            )
            if call is not None:
                conversation.finalize_call(session, call, transcript.render())
                session.commit()
