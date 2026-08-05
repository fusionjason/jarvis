from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURES", "false")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Channel, Lead  # noqa: E402
from app.services import compliance  # noqa: E402


@pytest.fixture
def engine():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield session
        session.flush()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def consented_lead(session: Session) -> Lead:
    lead = Lead(first_name="Maria", last_name="Alvarez", phone="+15551230001", state="TX")
    session.add(lead)
    session.flush()
    for channel in Channel:
        compliance.grant_consent(session, lead, channel, evidence="test consent")
    session.flush()
    return lead
