from __future__ import annotations

import asyncio

import pytest

from app.services import realtime


def test_greeting_discloses_automation_and_recording() -> None:
    text = realtime.greeting("Maria")
    assert "Maria" in text
    assert "automated assistant" in text
    assert "recorded" in text


def test_session_payload_uses_twilio_codec() -> None:
    session = realtime.session_update_payload("Maria")["session"]
    assert isinstance(session, dict)
    assert session["input_audio_format"] == "g711_ulaw"
    assert session["output_audio_format"] == "g711_ulaw"
    assert session["turn_detection"]["type"] == "server_vad"


def test_persona_forbids_quoting_and_binding() -> None:
    instructions = realtime.session_update_payload("Maria")["session"]["instructions"]  # type: ignore[index]
    assert "never quote a premium" in instructions.lower()
    assert "bind coverage" in instructions.lower()


def test_transcript_render_skips_blank_turns() -> None:
    transcript = realtime.CallTranscript()
    transcript.add("assistant", "Hi there")
    transcript.add("caller", "   ")
    transcript.add("caller", "Sounds good")
    assert transcript.render() == "assistant: Hi there\ncaller: Sounds good"


def test_connect_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(realtime.connect_realtime())
