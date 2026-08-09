"""Google Cloud Text-to-Speech for the scripted call prompts."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import speech

DISCLOSURE = speech.disclosure_text()


@pytest.fixture
def google_voice(tmp_path, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_tts_voice", "en-US-Neural2-F", raising=False)
    monkeypatch.setattr(settings, "tts_cache_dir", str(tmp_path / "tts"), raising=False)
    calls: list[str] = []

    def fake_synthesize(text: str) -> bytes:
        calls.append(text)
        return b"ID3fake-mp3-audio"

    monkeypatch.setattr(speech, "_synthesize", fake_synthesize)
    monkeypatch.setattr(speech, "_retry_after", 0.0)
    return calls


def test_no_google_voice_configured_falls_back_to_twilio_say(client: TestClient) -> None:
    response = client.post("/webhooks/voice/answer?lead_id=1", data={"CallSid": "CA1"})
    assert "<Play>" not in response.text
    assert "<Say" in response.text
    assert DISCLOSURE in response.text


def test_disclosure_is_played_in_the_google_voice(client: TestClient, google_voice) -> None:
    response = client.post("/webhooks/voice/answer?lead_id=1", data={"CallSid": "CA2"})
    assert "<Say" not in response.text
    assert f"/audio/{speech.clip_key(DISCLOSURE)}.mp3" in response.text
    assert google_voice == [DISCLOSURE]


def test_voicemail_message_is_played_in_the_google_voice(client: TestClient, google_voice) -> None:
    response = client.post(
        "/webhooks/voice/answer?lead_id=1", data={"CallSid": "CA3", "AnsweredBy": "machine_start"}
    )
    assert "<Play>" in response.text
    assert "<Hangup" in response.text
    assert len(google_voice) == 1


def test_clips_are_cached_rather_than_resynthesised(google_voice) -> None:
    first = speech.clip_url(DISCLOSURE)
    second = speech.clip_url(DISCLOSURE)
    assert first == second
    assert google_voice == [DISCLOSURE]


def test_synthesis_failure_falls_back_to_twilio_say(
    client: TestClient, google_voice, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text: str) -> bytes:
        raise RuntimeError("credentials rejected")

    monkeypatch.setattr(speech, "_synthesize", boom)
    response = client.post("/webhooks/voice/answer?lead_id=1", data={"CallSid": "CA4"})
    assert "<Play>" not in response.text
    assert DISCLOSURE in response.text


def test_a_failure_stops_further_synthesis_attempts_for_a_while(
    google_voice, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def boom(text: str) -> bytes:
        attempts.append(text)
        raise RuntimeError("credentials rejected")

    monkeypatch.setattr(speech, "_synthesize", boom)
    assert speech.clip_url(DISCLOSURE) is None
    assert speech.clip_url("another line") is None
    assert attempts == [DISCLOSURE]


def test_audio_route_serves_the_cached_clip(client: TestClient, google_voice) -> None:
    url = speech.clip_url(DISCLOSURE)
    assert url is not None
    response = client.get(url.removeprefix(get_settings().public_base_url))
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3fake-mp3-audio"


def test_prewarm_synthesises_the_fixed_prompts_up_front(google_voice) -> None:
    speech.prewarm()
    for _ in range(200):
        if len(google_voice) == 2:
            break
        time.sleep(0.01)
    assert google_voice == [speech.disclosure_text(), speech.voicemail_text()]


def test_prewarm_is_a_no_op_without_a_google_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str) -> bytes:
        raise AssertionError("should not synthesise without a configured voice")

    monkeypatch.setattr(speech, "_synthesize", boom)
    speech.prewarm()


def test_audio_route_rejects_unknown_and_malformed_keys(client: TestClient, google_voice) -> None:
    assert client.get("/audio/%s.mp3" % ("a" * 32)).status_code == 404
    assert client.get("/audio/..%2f..%2fetc%2fpasswd.mp3").status_code == 404
