from __future__ import annotations

import io

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Lead


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["ok"] is True


def test_create_lead_normalizes_phone_and_records_consent(client: TestClient) -> None:
    response = client.post(
        "/api/leads",
        json={
            "first_name": "Maria",
            "last_name": "Alvarez",
            "phone": "(555) 123-0001",
            "state": "TX",
            "consent_evidence": "web quote form 2026-08-01",
        },
    )
    assert response.status_code == 201
    assert response.json()["phone"] == "+15551230001"


def test_duplicate_phone_rejected(client: TestClient) -> None:
    payload = {"first_name": "A", "phone": "+15551230055"}
    assert client.post("/api/leads", json=payload).status_code == 201
    assert client.post("/api/leads", json=payload).status_code == 409


def test_texting_lead_without_consent_returns_409(client: TestClient) -> None:
    lead_id = client.post("/api/leads", json={"first_name": "B", "phone": "+15551230066"}).json()["id"]
    response = client.post(f"/api/leads/{lead_id}/text", json={"body": "hi"})
    assert response.status_code == 409
    assert "consent" in response.json()["detail"]


def test_csv_import(client: TestClient) -> None:
    csv_bytes = io.BytesIO(
        b"first_name,last_name,phone,state,consent_evidence\n"
        b"Tom,Becker,555-123-0077,NY,web form\n"
        b"Priya,Raman,5551230088,CA,web form\n"
    )
    response = client.post(
        "/api/leads/import", files={"file": ("leads.csv", csv_bytes, "text/csv")}
    )
    assert response.json() == {"created": 2, "skipped": []}


def test_inbound_sms_webhook_creates_lead_and_replies(client: TestClient) -> None:
    response = client.post(
        "/webhooks/sms/inbound",
        data={"From": "+15551239999", "Body": "I want a quote", "MessageSid": "SM1"},
    )
    assert response.status_code == 200
    assert "<Message>" in response.text


def test_inbound_stop_webhook_returns_empty_twiml(client: TestClient, session: Session) -> None:
    client.post("/webhooks/sms/inbound", data={"From": "+15551237777", "Body": "hi", "MessageSid": "SM2"})
    response = client.post(
        "/webhooks/sms/inbound", data={"From": "+15551237777", "Body": "STOP", "MessageSid": "SM3"}
    )
    assert "<Message>" not in response.text
    lead = session.query(Lead).filter(Lead.phone == "+15551237777").one()
    assert lead.status.value == "do_not_contact"


def test_dashboard_renders(client: TestClient) -> None:
    client.post(
        "/api/leads",
        json={"first_name": "Dash", "phone": "+15551230044", "consent_evidence": "web form"},
    )
    index = client.get("/")
    assert index.status_code == 200
    assert "Outreach Assistant" in index.text

    detail = client.get("/leads/1")
    assert detail.status_code == 200
    assert "Compliance audit trail" in detail.text


def test_dashboard_shows_reason_a_send_was_blocked(client: TestClient) -> None:
    client.post(
        "/api/leads",
        json={"first_name": "Blocked", "phone": "+15551230055", "consent_evidence": "web form"},
    )
    client.post("/api/leads/1/opt-out")

    redirect = client.post("/leads/1/email", data={"subject": "", "body": ""}, follow_redirects=False)
    assert redirect.status_code == 303
    page = client.get(redirect.headers["location"])
    assert "lead marked do-not-contact" in page.text
