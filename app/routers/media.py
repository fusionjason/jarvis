"""Serves the cached Google TTS clips that Twilio fetches for `<Play>`."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services import speech

router = APIRouter(tags=["media"])

KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@router.get("/audio/{key}.mp3")
def audio_clip(key: str) -> FileResponse:
    if not KEY_PATTERN.match(key):
        raise HTTPException(status_code=404, detail="clip not found")
    path = speech.clip_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="clip not found")
    return FileResponse(path, media_type="audio/mpeg")
