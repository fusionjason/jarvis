from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.routers import campaigns, dashboard, leads, webhooks

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(leads.router)
app.include_router(campaigns.router)
app.include_router(webhooks.router)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {
        "ok": True,
        "twilio_configured": settings.twilio_configured,
        "openai_configured": settings.openai_configured,
    }
