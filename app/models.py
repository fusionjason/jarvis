from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class LeadStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    engaged = "engaged"
    appointment_set = "appointment_set"
    not_interested = "not_interested"
    do_not_contact = "do_not_contact"
    sold = "sold"


class Channel(str, enum.Enum):
    sms = "sms"
    voice = "voice"


class Direction(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class ConsentKind(str, enum.Enum):
    granted = "granted"
    revoked = "revoked"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(160), default="")
    state: Mapped[str] = mapped_column(String(2), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_interest: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.new, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list[Message]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    calls: Mapped[list[Call]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    consents: Mapped[list[Consent]] = relationship(back_populates="lead", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Consent(Base):
    """Immutable record of consent granted or revoked for a channel."""

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    channel: Mapped[Channel] = mapped_column(Enum(Channel))
    kind: Mapped[ConsentKind] = mapped_column(Enum(ConsentKind))
    evidence: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="consents")


class DoNotContact(Base):
    __tablename__ = "do_not_contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    direction: Mapped[Direction] = mapped_column(Enum(Direction))
    body: Mapped[str] = mapped_column(Text)
    provider_sid: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    direction: Mapped[Direction] = mapped_column(Enum(Direction))
    provider_sid: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    recording_url: Mapped[str] = mapped_column(String(500), default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    disposition: Mapped[str] = mapped_column(String(40), default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="calls")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    goal: Mapped[str] = mapped_column(Text, default="Book a call with a licensed agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    steps: Mapped[list[CampaignStep]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="CampaignStep.day_offset"
    )
    enrollments: Mapped[list[Enrollment]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignStep(Base):
    __tablename__ = "campaign_steps"
    __table_args__ = (UniqueConstraint("campaign_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    day_offset: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[Channel] = mapped_column(Enum(Channel))
    prompt: Mapped[str] = mapped_column(Text, default="")

    campaign: Mapped[Campaign] = relationship(back_populates="steps")


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("campaign_id", "lead_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    next_step_position: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    campaign: Mapped[Campaign] = relationship(back_populates="enrollments")
    lead: Mapped[Lead] = relationship()


class AuditLog(Base):
    """Append-only record of every contact decision, for compliance review."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    event: Mapped[str] = mapped_column(String(60), index=True)
    channel: Mapped[Channel | None] = mapped_column(Enum(Channel), nullable=True)
    allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship()
