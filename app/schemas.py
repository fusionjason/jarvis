from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Channel, Direction, LeadStatus


class LeadCreate(BaseModel):
    first_name: str
    last_name: str = ""
    phone: str
    email: str = ""
    state: str = ""
    timezone: str = "America/New_York"
    age: int | None = None
    coverage_interest: str = ""
    source: str = "manual"
    notes: str = ""
    consent_evidence: str = Field(
        default="",
        description="How consent was captured (e.g. 'web form 2026-08-01, ip 1.2.3.4'). "
        "Outbound contact is blocked until consent exists.",
    )


class LeadUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    state: str | None = None
    timezone: str | None = None
    age: int | None = None
    coverage_interest: str | None = None
    status: LeadStatus | None = None
    notes: str | None = None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str
    last_name: str
    phone: str
    email: str
    state: str
    timezone: str
    age: int | None
    coverage_interest: str
    source: str
    status: LeadStatus
    notes: str
    attempts: int
    last_contacted_at: datetime | None
    next_action_at: datetime | None
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    channel: Channel
    direction: Direction
    subject: str
    body: str
    provider_sid: str
    status: str
    created_at: datetime


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    direction: Direction
    provider_sid: str
    status: str
    recording_url: str
    transcript: str
    summary: str
    disposition: str
    duration_seconds: int
    created_at: datetime


class SendTextRequest(BaseModel):
    body: str = Field(default="", description="Leave empty to let the assistant draft the message.")


class SendEmailRequest(BaseModel):
    subject: str = Field(default="", description="Leave empty to let the assistant draft it.")
    body: str = Field(default="", description="Leave empty to let the assistant draft it.")


class ImportResult(BaseModel):
    created: int
    skipped: list[str]


class CampaignStepIn(BaseModel):
    day_offset: int = 0
    channel: Channel = Channel.sms
    prompt: str = ""


class CampaignCreate(BaseModel):
    name: str
    goal: str = "Book a call with a licensed agent"
    steps: list[CampaignStepIn] = Field(default_factory=list)


class CampaignStepOut(CampaignStepIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    goal: str
    active: bool
    created_at: datetime
    steps: list[CampaignStepOut]


class EnrollRequest(BaseModel):
    lead_ids: list[int]


class RunResult(BaseModel):
    executed: int
    blocked: int
    details: list[str]
