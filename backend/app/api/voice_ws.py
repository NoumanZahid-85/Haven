"""
WebSocket voice endpoint — one socket per conversation, two engines behind it.

GET (ws) /api/v1/voice/ws?mode=live|pipeline&session_id=<id>&user_id=<id>&barge_in=0|1

  live      Gemini Live native speech-to-speech
  pipeline  Deepgram Flux STT → Groq LLM → Deepgram Aura-2 TTS

user_id is the Supabase auth user id, or a per-browser guest id in guest
mode — it scopes long-term memory to that person.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Literal

from fastapi import APIRouter, Query, WebSocket

from app.voice.live_session import LiveSession
from app.voice.pipeline_session import PipelineSession

router = APIRouter(prefix="/api/v1/voice")


@router.websocket("/ws")
async def voice_ws(
    websocket: WebSocket,
    mode: Literal["live", "pipeline"] = Query(default="live"),
    session_id: str = Query(default=""),
    user_id: str = Query(default="", max_length=64),
    barge_in: bool = Query(default=False),
) -> None:
    await websocket.accept()
    engine = LiveSession if mode == "live" else PipelineSession
    session = engine(websocket, session_id or str(uuid.uuid4()), user_id=user_id, barge_in=barge_in)
    try:
        await session.run()
    finally:
        with contextlib.suppress(Exception):  # already closing/closed
            await websocket.close()
