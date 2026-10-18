"""
Long-term memory — durable facts about a user, carried across sessions.

Short-term context (the last N turns of THIS conversation) lives in Redis
(app/services/cache.py). This module is the other half: after each turn an
LLM extracts at most two durable facts ("has a job interview on Friday",
"breathing exercises helped last time") and they are injected into the
system prompt of every future conversation.

Extraction runs off the hot path (fire-and-forget after the reply is
spoken), so memory never adds latency to a voice turn.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import Memory
from app.db.session import get_session_factory
from app.services.llm import llm_generate
from app.services.prompts import MEMORY_EXTRACT_PROMPT

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def load_facts(user_id: str) -> list[str]:
    """Most recent facts for this user, oldest first."""
    if not user_id:
        return []
    try:
        async with get_session_factory()() as db:
            rows = await db.scalars(
                select(Memory.fact)
                .where(Memory.user_id == user_id)
                .order_by(Memory.created_at.desc())
                .limit(get_settings().memory_facts)
            )
            return list(reversed(list(rows)))
    except Exception as exc:  # noqa: BLE001 — memory is an enhancement, never a blocker
        logger.warning("Memory load failed for user=%s: %s", user_id, exc)
        return []


async def extract_and_store(user_id: str, user_text: str, reply: str, known: list[str]) -> list[str]:
    """Extract new durable facts from one exchange and persist them."""
    if not user_id or len(user_text.split()) < 5:
        return []
    try:
        raw = await llm_generate(
            [{
                "role": "user",
                "content": MEMORY_EXTRACT_PROMPT.format(
                    known="\n".join(f"- {f}" for f in known) or "(nothing yet)",
                    user=user_text,
                    reply=reply,
                ),
            }],
            temperature=0.0,
            max_tokens=400,
        )
        match = _JSON_RE.search(raw)
        facts = [str(f).strip() for f in json.loads(match.group(0)).get("facts", [])] if match else []
        facts = [f for f in facts if f and f not in known][:2]
        if facts:
            async with get_session_factory()() as db:
                db.add_all(Memory(user_id=user_id, fact=f) for f in facts)
                await db.commit()
            logger.info("Stored %d memory fact(s) for user=%s", len(facts), user_id)
        return facts
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory extraction failed for user=%s: %s", user_id, exc)
        return []


async def forget(user_id: str) -> int:
    """Delete everything remembered about a user. Returns rows removed."""
    async with get_session_factory()() as db:
        result = await db.execute(delete(Memory).where(Memory.user_id == user_id))
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)
