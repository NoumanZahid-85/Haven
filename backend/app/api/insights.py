"""
Production insights — the monitoring half of the evaluation loop.

GET /api/v1/insights returns volume, latency percentiles per pipeline stage,
safety-tier counts, barge-in rate, and the aggregate LLM-judge scores written
by scripts/evaluate.py. Everything is computed from stored turns, so the
numbers are measured, not estimated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Turn, TurnEval
from app.db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["insights"])

WINDOW = 1000  # latency stats over the most recent N turns per voice mode
VOICE_MODES = ("live", "pipeline")


def _pcts(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "n": 0}
    arr = np.asarray(values)
    return {"p50": round(float(np.percentile(arr, 50))), "p95": round(float(np.percentile(arr, 95))), "n": len(values)}


@router.get("/insights")
async def insights(db: AsyncSession = Depends(get_db)) -> dict:
    since = datetime.now(UTC) - timedelta(hours=24)
    totals = (await db.execute(
        select(
            func.count(Turn.id),
            func.count(func.distinct(Turn.session_id)),
            func.count(func.distinct(Turn.user_id)),
            func.sum(Turn.interrupted),
        )
    )).one()
    turns_24h = await db.scalar(select(func.count(Turn.id)).where(Turn.created_at >= since))
    risk_rows = (await db.execute(select(Turn.risk, func.count(Turn.id)).group_by(Turn.risk))).all()

    latency: dict[str, dict] = {}
    for mode in VOICE_MODES:
        recent = (await db.execute(
            select(Turn.stt_ms, Turn.llm_first_token_ms, Turn.first_audio_ms, Turn.total_ms)
            .where(Turn.channel == mode, Turn.interrupted == 0)
            .order_by(Turn.created_at.desc())
            .limit(WINDOW)
        )).all()
        cols = [[r[i] for r in recent if r[i] is not None] for i in range(4)]
        latency[mode] = {
            "end_of_turn": _pcts(cols[0]),
            "llm_first_token": _pcts(cols[1]),
            "first_audio": _pcts(cols[2]),
            "full_turn": _pcts(cols[3]),
        }

    evals = (await db.execute(
        select(
            func.count(TurnEval.id),
            func.avg(TurnEval.overall),
            func.avg(TurnEval.empathy),
            func.avg(TurnEval.safety),
            func.avg(TurnEval.helpfulness),
            func.avg(TurnEval.voice_fit),
        )
    )).one()

    def avg(v) -> float | None:
        return round(float(v), 2) if v is not None else None

    return {
        "volume": {
            "turns": totals[0],
            "conversations": totals[1],
            "users": totals[2],
            "turns_24h": turns_24h or 0,
            "barge_ins": int(totals[3] or 0),
        },
        "safety": {risk: count for risk, count in risk_rows},
        "latency_ms": latency,
        "quality": {
            "evaluated_turns": evals[0],
            "overall": avg(evals[1]),
            "empathy": avg(evals[2]),
            "safety": avg(evals[3]),
            "helpfulness": avg(evals[4]),
            "voice_fit": avg(evals[5]),
        },
    }
