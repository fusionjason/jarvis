"""LLM helpers: SMS reply drafting, call summarization, and the shared persona.

Every entry point degrades to a deterministic fallback when no OpenAI key is
configured so the app stays runnable (and testable) without credentials.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import OpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)

DISPOSITIONS = [
    "interested",
    "appointment_set",
    "callback_requested",
    "not_interested",
    "do_not_contact",
    "wrong_number",
    "voicemail",
    "no_answer",
]


def persona_instructions(lead_name: str = "") -> str:
    settings = get_settings()
    who = f"You are speaking with {lead_name}. " if lead_name else ""
    return f"""You are {settings.assistant_name}, an AI assistant for {settings.agency_name}, \
a life insurance agency. {who}

Your only job is to (1) confirm you are speaking to the right person, (2) understand what kind of \
life insurance coverage they are looking for, (3) collect basic qualification details \
(age, tobacco use, general health, desired coverage amount, beneficiaries), and (4) book a short \
call with {settings.licensed_agent_name}.

Hard rules you must never break:
- Immediately say you are an AI assistant if asked, and disclose it up front on calls.
- Never quote a premium, never state a rate, never promise approval, and never bind coverage. \
Only a licensed agent can do those things. If pressed, say a licensed agent will confirm exact numbers.
- Never give medical, tax, or legal advice.
- Never ask for a Social Security number, bank details, or payment information.
- If the person asks to be removed, says stop, or sounds distressed, apologize, confirm you will \
remove them, and end the conversation politely.
- Keep replies short and conversational: one or two sentences for text, one or two for speech.
"""


@dataclass
class CallAnalysis:
    summary: str
    disposition: str
    next_step: str


def _client() -> OpenAI | None:
    settings = get_settings()
    if not settings.openai_configured:
        return None
    return OpenAI(api_key=settings.openai_api_key)


def draft_sms_reply(lead_name: str, history: list[tuple[str, str]], goal: str) -> str:
    """Draft the next outbound text given ``history`` as (role, body) pairs."""
    client = _client()
    if client is None:
        return _fallback_sms(lead_name, history)

    settings = get_settings()
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": persona_instructions(lead_name)
            + f"\nGoal of this conversation: {goal}\n"
            + "Write only the body of the next text message. Under 320 characters. "
            + "Include 'Reply STOP to opt out.' only on the first message you send.",
        }
    ]
    for role, body in history:
        messages.append({"role": "assistant" if role == "outbound" else "user", "content": body})

    try:
        response = client.chat.completions.create(
            model=settings.openai_text_model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=160,
            temperature=0.6,
        )
        return (response.choices[0].message.content or "").strip() or _fallback_sms(lead_name, history)
    except Exception:
        logger.exception("SMS drafting failed; using fallback copy")
        return _fallback_sms(lead_name, history)


def _fallback_sms(lead_name: str, history: list[tuple[str, str]]) -> str:
    settings = get_settings()
    first_name = lead_name.split(" ")[0] if lead_name else "there"
    if not history:
        return (
            f"Hi {first_name}, this is {settings.assistant_name}, an AI assistant with "
            f"{settings.agency_name}. You asked about life insurance coverage - is now a good time "
            f"for a quick chat with {settings.licensed_agent_name}? Reply STOP to opt out."
        )
    return (
        f"Thanks {first_name} - I'll have {settings.licensed_agent_name} follow up with the exact "
        "details. What time works best for a short call?"
    )


@dataclass
class EmailDraft:
    subject: str
    body: str


def draft_email(
    lead_name: str, coverage_interest: str, history: list[tuple[str, str]], goal: str
) -> EmailDraft:
    """Draft the next outbound email. ``history`` is (role, body) pairs across channels."""
    client = _client()
    if client is None:
        return _fallback_email(lead_name, coverage_interest)

    settings = get_settings()
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": persona_instructions(lead_name)
            + f"\nGoal of this email: {goal}\n"
            + "Write a short plain-text email (under 120 words), no markdown, no placeholders like "
            + "[Name], and no premium figures. Respond with JSON: "
            + '{"subject": str, "body": str}. Do not add a signature or unsubscribe line - the '
            + "system appends those.",
        }
    ]
    for role, body in history:
        messages.append({"role": "assistant" if role == "outbound" else "user", "content": body})

    try:
        response = client.chat.completions.create(
            model=settings.openai_text_model,
            response_format={"type": "json_object"},
            messages=messages,  # type: ignore[call-overload]
            max_tokens=500,
            temperature=0.6,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        subject = str(payload.get("subject", "")).strip()
        body = str(payload.get("body", "")).strip()
        if not subject or not body:
            return _fallback_email(lead_name, coverage_interest)
        return EmailDraft(subject=subject, body=body)
    except Exception:
        logger.exception("Email drafting failed; using fallback copy")
        return _fallback_email(lead_name, coverage_interest)


def _fallback_email(lead_name: str, coverage_interest: str) -> EmailDraft:
    settings = get_settings()
    first_name = lead_name.split(" ")[0] if lead_name else "there"
    interest = f" about {coverage_interest}" if coverage_interest else ""
    return EmailDraft(
        subject=f"Your life insurance question, {first_name}",
        body=(
            f"Hi {first_name},\n\n"
            f"I'm {settings.assistant_name}, an AI assistant with {settings.agency_name}. You reached "
            f"out{interest}, and I'd like to get you in front of {settings.licensed_agent_name} who can "
            "confirm your options and exact numbers.\n\n"
            "What day and time work best for a short call this week?"
        ),
    )


def analyze_call(transcript: str) -> CallAnalysis:
    """Summarize a call transcript and pick a disposition."""
    client = _client()
    if client is None or not transcript.strip():
        return CallAnalysis(summary=transcript.strip()[:500], disposition="", next_step="")

    settings = get_settings()
    try:
        response = client.chat.completions.create(
            model=settings.openai_text_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this life insurance outreach call for a licensed agent. Respond "
                        'with JSON: {"summary": str, "disposition": one of '
                        f"{DISPOSITIONS}, \"next_step\": str}}. The summary must capture stated age, "
                        "tobacco use, health notes, coverage amount and beneficiaries when mentioned."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        disposition = str(payload.get("disposition", ""))
        return CallAnalysis(
            summary=str(payload.get("summary", "")),
            disposition=disposition if disposition in DISPOSITIONS else "",
            next_step=str(payload.get("next_step", "")),
        )
    except Exception:
        logger.exception("Call analysis failed; storing raw transcript")
        return CallAnalysis(summary=transcript.strip()[:500], disposition="", next_step="")
