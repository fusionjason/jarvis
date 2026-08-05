"""Bridge between a Twilio Media Stream and the OpenAI Realtime API.

Twilio streams 8 kHz G.711 mu-law audio over a websocket; the Realtime API accepts and
emits the same codec, so the bridge is a bidirectional relay plus a little bookkeeping:
barge-in handling (clear Twilio's buffer when the caller interrupts) and transcript
capture for the post-call summary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field

from fastapi import WebSocket
from websockets.asyncio.client import ClientConnection, connect

from app.config import get_settings
from app.services.llm import persona_instructions

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime"


@dataclass
class CallTranscript:
    turns: list[tuple[str, str]] = field(default_factory=list)

    def add(self, speaker: str, text: str) -> None:
        text = text.strip()
        if text:
            self.turns.append((speaker, text))

    def render(self) -> str:
        return "\n".join(f"{speaker}: {text}" for speaker, text in self.turns)


def greeting(lead_first_name: str) -> str:
    settings = get_settings()
    name = f" {lead_first_name}" if lead_first_name else ""
    return (
        f"Hi{name}, this is {settings.assistant_name}, an automated assistant calling on behalf of "
        f"{settings.agency_name} about the life insurance coverage you asked about. "
        "This call may be recorded. Is now a good time?"
    )


def session_update_payload(lead_name: str) -> dict[str, object]:
    settings = get_settings()
    return {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "instructions": persona_instructions(lead_name),
            "voice": settings.openai_realtime_voice,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "silence_duration_ms": 600,
            },
            "temperature": 0.7,
        },
    }


async def connect_realtime() -> ClientConnection:
    settings = get_settings()
    if not settings.openai_configured:
        raise RuntimeError("OPENAI_API_KEY is not configured; cannot start the voice agent")
    return await connect(
        f"{REALTIME_URL}?model={settings.openai_realtime_model}",
        additional_headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        },
        max_size=None,
    )


class MediaStreamBridge:
    """Relays audio between one Twilio media stream and one Realtime session."""

    def __init__(self, twilio_ws: WebSocket, lead_name: str = "") -> None:
        self.twilio_ws = twilio_ws
        self.lead_name = lead_name
        self.stream_sid: str = ""
        self.transcript = CallTranscript()
        self._openai_ws: ClientConnection | None = None

    async def run(self) -> CallTranscript:
        openai_ws = await connect_realtime()
        self._openai_ws = openai_ws
        try:
            await openai_ws.send(json.dumps(session_update_payload(self.lead_name)))
            await self._say(greeting(self.lead_name.split(" ")[0] if self.lead_name else ""))
            await asyncio.gather(
                self._pump_twilio_to_openai(openai_ws),
                self._pump_openai_to_twilio(openai_ws),
            )
        finally:
            await openai_ws.close()
        return self.transcript

    async def _say(self, text: str) -> None:
        """Ask the model to speak a specific line (used for the required disclosure)."""
        assert self._openai_ws is not None
        await self._openai_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {"modalities": ["audio", "text"], "instructions": f"Say exactly: {text}"},
                }
            )
        )
        self.transcript.add("assistant", text)

    async def _pump_twilio_to_openai(self, openai_ws: ClientConnection) -> None:
        try:
            while True:
                raw = await self.twilio_ws.receive_text()
                event = json.loads(raw)
                kind = event.get("event")
                if kind == "start":
                    self.stream_sid = event["start"]["streamSid"]
                elif kind == "media":
                    await openai_ws.send(
                        json.dumps(
                            {"type": "input_audio_buffer.append", "audio": event["media"]["payload"]}
                        )
                    )
                elif kind == "stop":
                    break
        except Exception as exc:  # noqa: BLE001 - the call simply ended or dropped
            logger.info("Twilio media stream closed: %s", exc)
        finally:
            await openai_ws.close()

    async def _pump_openai_to_twilio(self, openai_ws: ClientConnection) -> None:
        try:
            async for raw in openai_ws:
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "response.audio.delta" and event.get("delta"):
                    await self._send_audio(event["delta"])
                elif kind == "input_audio_buffer.speech_started":
                    await self._clear_playback()
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self.transcript.add("caller", event.get("transcript", ""))
                elif kind == "response.audio_transcript.done":
                    self.transcript.add("assistant", event.get("transcript", ""))
                elif kind == "error":
                    logger.error("Realtime API error: %s", event.get("error"))
        except Exception as exc:  # noqa: BLE001 - session ended
            logger.info("Realtime session closed: %s", exc)

    async def _send_audio(self, payload_b64: str) -> None:
        # Twilio expects raw base64 mu-law; re-encoding guards against padding differences.
        payload = base64.b64encode(base64.b64decode(payload_b64)).decode()
        await self.twilio_ws.send_text(
            json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
        )

    async def _clear_playback(self) -> None:
        if self.stream_sid:
            await self.twilio_ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
