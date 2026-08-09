"""Google Cloud Text-to-Speech for the scripted lines Twilio plays on a call.

Only the fixed prompts (recording/AI disclosure, voicemail message) go through here; the live
conversation audio comes from the OpenAI Realtime model. Synthesised clips are cached on disk by
content hash and served to Twilio as `<Play>` URLs. When Google credentials or a voice are not
configured, `clip_url` returns None and callers fall back to Twilio's built-in `<Say>`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client: object | None = None
# Twilio drops the call if TwiML takes too long, so after a synthesis failure (bad credentials,
# Google unreachable) stop trying for a while and let callers use `<Say>` immediately.
_RETRY_COOLDOWN_SECONDS = 60.0
_retry_after = 0.0


def disclosure_text() -> str:
    return "This call is with an automated assistant and may be recorded for quality assurance."


def voicemail_text() -> str:
    return (
        f"Hi, this is an automated assistant calling from {get_settings().agency_name} about your "
        "life insurance request. We'll try you again, or call us back any time."
    )


def prewarm() -> None:
    """Synthesise the fixed prompts in the background at startup.

    The first Google call in a process pays for credential discovery, which can take over ten
    seconds - long enough for Twilio to give up on the TwiML - so it must not happen mid-call.
    """
    if not get_settings().google_tts_configured:
        return

    def run() -> None:
        for text in (disclosure_text(), voicemail_text()):
            clip_url(text)

    threading.Thread(target=run, name="tts-prewarm", daemon=True).start()


def cache_dir() -> Path:
    path = Path(get_settings().tts_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clip_key(text: str) -> str:
    settings = get_settings()
    fingerprint = "|".join(
        [
            text.strip(),
            settings.google_tts_voice,
            settings.google_tts_language_code,
            f"{settings.google_tts_speaking_rate:.2f}",
        ]
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def clip_path(key: str) -> Path:
    return cache_dir() / f"{key}.mp3"


def _get_client() -> object:
    global _client
    with _lock:
        if _client is None:
            from google.cloud import texttospeech

            settings = get_settings()
            if settings.google_application_credentials:
                os.environ.setdefault(
                    "GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials
                )
            _client = texttospeech.TextToSpeechClient()
        return _client


def _synthesize(text: str) -> bytes:
    from google.cloud import texttospeech

    settings = get_settings()
    client = _get_client()
    response = client.synthesize_speech(  # type: ignore[attr-defined]
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=settings.google_tts_language_code,
            name=settings.google_tts_voice,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=settings.google_tts_speaking_rate,
            # Twilio calls are narrowband; synthesising at 8 kHz keeps the clips small.
            sample_rate_hertz=8000,
        ),
        timeout=settings.google_tts_timeout_seconds,
    )
    return bytes(response.audio_content)


def clip_url(text: str) -> str | None:
    """Return a public URL for `text` spoken by Google TTS, or None to fall back to `<Say>`."""
    global _retry_after
    settings = get_settings()
    if not settings.google_tts_configured or not text.strip():
        return None

    key = clip_key(text)
    path = clip_path(key)
    if not path.exists():
        if time.monotonic() < _retry_after:
            return None
        try:
            audio = _synthesize(text)
        except Exception:
            _retry_after = time.monotonic() + _RETRY_COOLDOWN_SECONDS
            logger.exception("Google TTS synthesis failed; falling back to Twilio Say")
            return None
        _retry_after = 0.0
        path.write_bytes(audio)
    return f"{settings.public_base_url}/audio/{key}.mp3"
