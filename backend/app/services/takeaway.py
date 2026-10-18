"""
End-of-conversation takeaway: one sentence on what was talked through, how
the person's mood moved from start to end, and one small next step they can
take by tomorrow.

Why "by tomorrow": a conversation that ends with a concrete, near action is
far more likely to change something than one that ends with "take care".
Small, immediate steps are the core of behavioural activation, and the same
idea drives fast-moving teams: don't park the next step two weeks out.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select

from app.db.models import Takeaway, Turn
from app.db.session import get_session_factory
from app.services.llm import llm_generate
from app.services.prompts import TAKEAWAY_PROMPT

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
MIN_TURNS = 1


def _as_dict(t: Takeaway) -> dict:
    return {
        "session_id": t.session_id,
        "summary": t.summary,
        "mood_start": t.mood_start,
        "mood_end": t.mood_end,
        "next_step": t.next_step,
        "lang": t.lang,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


async def build(session_id: str, user_id: str = "", lang: str = "en") -> dict | None:
    """Create (or return the existing) takeaway for one conversation."""
    factory = get_session_factory()
    async with factory() as db:
        existing = await db.scalar(select(Takeaway).where(Takeaway.session_id == session_id))
        if existing is not None:
            return _as_dict(existing)
        turns = list(await db.scalars(
            select(Turn).where(Turn.session_id == session_id).order_by(Turn.created_at)
        ))
    if len(turns) < MIN_TURNS:
        return None

    transcript = "\n".join(f"Person: {t.user_text}\nHaven: {t.reply}" for t in turns[-20:])
    raw = await llm_generate(
        [{
            "role": "user",
            "content": TAKEAWAY_PROMPT.format(
                transcript=transcript, language="German (use du)" if lang == "de" else "English"
            ),
        }],
        temperature=0.3,
        max_tokens=600,
    )
    match = _JSON_RE.search(raw)
    data = json.loads(match.group(0)) if match else {}
    if not data.get("summary"):
        logger.warning("Takeaway generation returned no summary for session=%s", session_id)
        return None

    takeaway = Takeaway(
        session_id=session_id,
        user_id=user_id or turns[0].user_id,
        summary=str(data["summary"])[:600],
        mood_start=str(data.get("mood_start", ""))[:40],
        mood_end=str(data.get("mood_end", ""))[:40],
        next_step=str(data.get("next_step", ""))[:400],
        lang=lang,
    )
    async with factory() as db:
        db.add(takeaway)
        await db.commit()
        await db.refresh(takeaway)
    return _as_dict(takeaway)


async def recent(user_id: str, limit: int = 20) -> list[dict]:
    async with get_session_factory()() as db:
        rows = await db.scalars(
            select(Takeaway).where(Takeaway.user_id == user_id).order_by(Takeaway.created_at.desc()).limit(limit)
        )
        return [_as_dict(t) for t in rows]
