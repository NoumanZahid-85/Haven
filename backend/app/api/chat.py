"""
Text chat — the same companion engine as voice, minus audio.

Useful as a fallback when someone can't talk out loud (a shared room, a
train), and as the cheapest way to exercise the engine in tests and evals.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from app.schemas.chat import ChatRequest, ChatResponse
from app.services import companion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    t0 = time.perf_counter()
    convo = await companion.Conversation(session_id=req.session_id, user_id=req.user_id, channel="text", lang=req.lang).load()
    plan = await companion.plan_turn(convo, req.message)
    try:
        reply = " ".join([s async for s in companion.reply_stream(plan)])
    except Exception:
        logger.exception("Chat turn failed session=%s", req.session_id)
        return ChatResponse(reply="Sorry, something went wrong on my side. Could you say that again?")

    await companion.finish_turn(convo, plan, reply, total_ms=(time.perf_counter() - t0) * 1000)
    return ChatResponse(
        reply=reply,
        risk=plan.risk.value,
        techniques=[t.title for t in plan.techniques],
        llm_first_token_ms=plan.llm_first_token_ms,
    )
