"""
Async SQLAlchemy models — everything the companion persists.

  Memory     long-term facts about a user, carried across sessions
  Turn       one user utterance + reply, with risk level and latency breakdown
  TurnEval   LLM-as-judge scores for a turn (the offline learning loop)

Turns are the raw material of the evaluation loop: every production turn is
stored with its timings, scripts/evaluate.py scores them against a rubric, and
the lowest scorers become regression cases in scripts/data/eval_cases.json.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    channel: Mapped[str] = mapped_column(String(10), nullable=False, default="voice")
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    reply: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String(10), nullable=False, default="none")  # none | elevated | crisis
    techniques: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # comma-separated ids
    interrupted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Latency breakdown in ms; NULL when the stage didn't run (e.g. text chat has no STT).
    stt_ms: Mapped[float | None] = mapped_column(Float)
    llm_first_token_ms: Mapped[float | None] = mapped_column(Float)
    first_audio_ms: Mapped[float | None] = mapped_column(Float)  # end of speech → first audio byte sent
    total_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TurnEval(Base):
    __tablename__ = "turn_evals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    turn_id: Mapped[str] = mapped_column(String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, index=True)
    empathy: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    safety: Mapped[int] = mapped_column(Integer, nullable=False)
    helpfulness: Mapped[int] = mapped_column(Integer, nullable=False)
    voice_fit: Mapped[int] = mapped_column(Integer, nullable=False)  # short, speakable, no lists
    overall: Mapped[float] = mapped_column(Float, nullable=False)
    critique: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
