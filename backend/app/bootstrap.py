"""
Startup / shutdown bootstrap — the single owner of app lifecycle wiring.

Everything the process needs before serving traffic lives here so the
entrypoint (main.py) stays a thin assembly list.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.db.models import Base
from app.db.session import get_engine
from app.services import knowledge

logger = logging.getLogger(__name__)

_warm_tasks: set[asyncio.Task] = set()


async def ensure_schema() -> None:
    """Create DB tables if missing. Retries so a slow-waking managed Postgres
    doesn't crash the container; degrades instead of failing startup."""
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            async with get_engine().begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Connected to PostgreSQL — tables synced")
            return
        except Exception as exc:
            if attempt < max_retries:
                logger.warning("PostgreSQL attempt %d/%d failed: %s. Retrying in 2s...", attempt, max_retries, exc)
                await asyncio.sleep(2)
            else:
                logger.error("PostgreSQL schema sync failed after %d attempts: %s. Degraded mode.", max_retries, exc)


async def _warm_llm() -> None:
    """The first LiteLLM call pays ~8s of import + connection setup (measured).
    Pay it at boot, not on the first user's first sentence."""
    from app.services.llm import llm_generate

    try:
        await llm_generate([{"role": "user", "content": "Say ok."}], max_tokens=50)
        logger.info("LLM warm")
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM warm-up failed: %s", exc)


def warm_up() -> None:
    """Embed the knowledge base, warm the LLM, and pre-synthesize fixed
    phrases in the background — the server accepts traffic immediately."""
    from app.voice.ws_session import prewarm_tts

    for coro in (knowledge.warm_knowledge(), _warm_llm(), prewarm_tts(get_settings().groq_api_key)):
        task = asyncio.create_task(coro)
        _warm_tasks.add(task)
        task.add_done_callback(_warm_tasks.discard)


async def shutdown() -> None:
    for task in list(_warm_tasks):
        task.cancel()
    await get_engine().dispose()
    logger.info("Shutdown complete")
