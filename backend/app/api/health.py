"""
Health check — the first endpoint to call after a deploy.

Reports database connectivity plus which providers are configured, so a
missing secret is visible without reading logs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.schemas.health import HealthResponse
from app.services import knowledge
from app.services.cache import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    db_status = "disconnected"
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        logger.error("PostgreSQL health check failed: %s", exc)

    return HealthResponse(
        status="ok" if db_status == "connected" and settings.groq_api_key else "degraded",
        db=db_status,
        redis="configured" if get_redis() is not None else "disabled",
        voice="configured" if settings.groq_api_key else "missing GROQ_API_KEY",
        knowledge="embedded" if knowledge.is_embedded() else "tag-fallback",
    )
