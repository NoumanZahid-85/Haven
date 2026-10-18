"""
WebSocket voice endpoint — the entire voice stack in one socket.

GET (ws) /api/v1/voice/ws?session_id=<id>&user_id=<id>&barge_in=0|1

user_id is the Supabase auth user id, or a per-browser guest id in guest
mode — it scopes long-term memory to that person.
"""

from __future__ import annotations

import contextlib
import uuid

from fastapi import APIRouter, Query, WebSocket

from app.voice.ws_session import VoiceWSSession

router = APIRouter(prefix="/api/v1/voice")


@router.websocket("/ws")
async def voice_ws(
    websocket: WebSocket,
    session_id: str = Query(default=""),
    user_id: str = Query(default="", max_length=64),
    barge_in: bool = Query(default=False),
) -> None:
    await websocket.accept()
    session = VoiceWSSession(websocket, session_id or str(uuid.uuid4()), user_id=user_id, barge_in=barge_in)
    try:
        await session.run()
    finally:
        with contextlib.suppress(Exception):  # already closing/closed
            await websocket.close()
