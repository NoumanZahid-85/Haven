"""Chat request/response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="What the user said")
    session_id: str = Field(..., min_length=1, max_length=200, description="Conversation session identifier")
    user_id: str = Field(default="", max_length=64, description="Supabase auth user id or guest id")
    lang: Literal["en", "de"] = Field(default="en", description="Conversation language")


class ChatResponse(BaseModel):
    reply: str
    risk: str = "none"
    techniques: list[str] = []
    llm_first_token_ms: float | None = None
