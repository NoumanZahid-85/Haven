"""
Long-term memory — durable facts and wins, carried across sessions.

Short-term context (the last N turns of THIS conversation) lives in Redis
(app/services/cache.py). This module is the other half: after each turn an
LLM extracts at most two durable facts ("has a job interview on Friday",
"breathing exercises helped last time") and at most two wins ("You closed
the Meier deal"). Facts shape every future conversation; wins are brought
back on purpose, because people who never feel their wins are a big part of
who reaches for a companion at 2 a.m.

Extraction runs off the hot path (fire-and-forget after the reply is
spoken), so memory never adds latency to a voice turn.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import Memory, Win
from app.db.session import get_session_factory
from app.services.llm import llm_generate
from app.services.prompts import MEMORY_EXTRACT_PROMPT

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
WINS_IN_PROMPT = 5


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


async def load_wins(user_id: str, limit: int = WINS_IN_PROMPT) -> list[str]:
    """Most recent wins for this user, newest first."""
    if not user_id:
        return []
    try:
        async with get_session_factory()() as db:
            rows = await db.scalars(
                select(Win.text).where(Win.user_id == user_id).order_by(Win.created_at.desc()).limit(limit)
            )
            return list(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wins load failed for user=%s: %s", user_id, exc)
        return []


async def extract_and_store(
    user_id: str, user_text: str, reply: str, known: list[str], session_id: str = ""
) -> list[str]:
    """Extract new durable facts and wins from one exchange and persist them.

    Returns the new facts (wins are only stored)."""
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
            max_tokens=500,
        )
        match = _JSON_RE.search(raw)
        data = json.loads(match.group(0)) if match else {}
        facts = [str(f).strip() for f in data.get("facts", []) if str(f).strip() and str(f).strip() not in known][:2]
        wins = [str(w).strip() for w in data.get("wins", []) if str(w).strip()][:2]
        if facts or wins:
            async with get_session_factory()() as db:
                db.add_all(Memory(user_id=user_id, fact=f) for f in facts)
                db.add_all(Win(user_id=user_id, session_id=session_id, text=w) for w in wins)
                await db.commit()
            logger.info("Stored %d fact(s), %d win(s) for user=%s", len(facts), len(wins), user_id)
        return facts
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory extraction failed for user=%s: %s", user_id, exc)
        return []


async def forget(user_id: str) -> int:
    """Delete everything remembered about a user (facts and wins). Returns rows removed."""
    async with get_session_factory()() as db:
        removed = 0
        for model in (Memory, Win):
            result = await db.execute(delete(model).where(model.user_id == user_id))
            removed += int(getattr(result, "rowcount", 0) or 0)
        await db.commit()
        return removed
