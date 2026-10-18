"""
The person's journey across conversations.

  POST /api/v1/sessions/{session_id}/takeaway  — build the end-of-call card
  GET  /api/v1/journey/{user_id}               — takeaways + wins over time
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Win
from app.db.session import get_session_factory
from app.services import takeaway

router = APIRouter(prefix="/api/v1", tags=["journey"])


class TakeawayOut(BaseModel):
    session_id: str
    summary: str
    mood_start: str
    mood_end: str
    next_step: str
    lang: str
    created_at: str | None = None


class WinOut(BaseModel):
    text: str
    created_at: str | None = None


class JourneyOut(BaseModel):
    takeaways: list[TakeawayOut]
    wins: list[WinOut]


@router.post("/sessions/{session_id}/takeaway", response_model=TakeawayOut)
async def make_takeaway(
    session_id: str,
    user_id: str = Query(default="", max_length=64),
    lang: Literal["en", "de"] = Query(default="en"),
) -> TakeawayOut:
    result = await takeaway.build(session_id, user_id=user_id, lang=lang)
    if result is None:
        raise HTTPException(status_code=404, detail="Nothing to sum up yet: this conversation has no replies.")
    return TakeawayOut(**result)


@router.get("/journey/{user_id}", response_model=JourneyOut)
async def journey(user_id: str) -> JourneyOut:
    async with get_session_factory()() as db:
        wins = list(await db.scalars(
            select(Win).where(Win.user_id == user_id).order_by(Win.created_at.desc()).limit(40)
        ))
    return JourneyOut(
        takeaways=[TakeawayOut(**t) for t in await takeaway.recent(user_id)],
        wins=[WinOut(text=w.text, created_at=w.created_at.isoformat() if w.created_at else None) for w in wins],
    )
