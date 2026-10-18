"""
Haven — FastAPI application entrypoint.

Registers routers only. Startup/shutdown wiring lives in app.bootstrap.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.insights import router as insights_router
from app.api.journey import router as journey_router
from app.api.memory import router as memory_router
from app.api.voice import router as voice_router
from app.api.voice_ws import router as voice_ws_router
from app.bootstrap import ensure_schema, shutdown, warm_up
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting %s (debug=%s)", settings.app_name, settings.debug)
    await ensure_schema()
    warm_up()
    yield
    await shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Real-time voice companion for mental wellbeing: streaming STT → LLM → TTS with "
        "barge-in, safety guardrails, long-term memory, technique retrieval and an evaluation loop.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (health_router, voice_ws_router, voice_router, chat_router, memory_router, journey_router, insights_router):
        app.include_router(router)
    return app


app = create_app()
