"""
Companion engine — one conversational turn, shared by voice and text.

    user text
       │
       ▼
    safety.screen_input ──(crisis)──▶ scripted crisis response (no LLM)
       │
       ▼  (in parallel)
    short-term history (Redis)  +  technique retrieval (RAG)
       │
       ▼
    persona prompt = persona + long-term memory + techniques + risk guidance
       │
       ▼
    LLM token stream ──▶ sentence segmenter ──▶ safety.screen_output ──▶ caller
                                                                  (TTS per sentence)
       │
       ▼  (after the reply, off the hot path)
    persist Turn (+ latency breakdown) · append history · extract memory facts

The engine knows nothing about audio. ws_session.py turns the sentence
stream into speech; api/chat.py just joins it into text.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.db.models import Turn
from app.db.session import get_session_factory
from app.services import cache, knowledge, memory
from app.services.llm import llm_stream
from app.services.prompts import MEMORY_BLOCK, PERSONA_PROMPT, RISK_BLOCK, TECHNIQUE_BLOCK
from app.services.safety import CRISIS_RESPONSE, ELEVATED_GUIDANCE, Risk, screen_input, screen_output

logger = logging.getLogger(__name__)

SENTENCE_MAX_CHARS = 170  # force a cut on run-on sentences so TTS can start
_BOUNDARY_RE = re.compile(r"(?<=[.!?…])[\"')\]]?\s+|\n+")
_MARKDOWN_RE = re.compile(r"[*_#`>]|^\s*[-•]\s+|^\s*\d+\.\s+", re.MULTILINE)

# Fire-and-forget tasks must be referenced or the loop may GC them mid-run.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@dataclass
class Conversation:
    """State that lives for one conversation (a voice socket or chat session)."""

    session_id: str
    user_id: str = ""
    channel: str = "voice"
    facts: list[str] = field(default_factory=list)

    async def load(self) -> Conversation:
        self.facts = await memory.load_facts(self.user_id)
        return self


@dataclass
class TurnPlan:
    user_text: str
    risk: Risk
    techniques: list[knowledge.Technique]
    messages: list[dict[str, str]]
    scripted: str | None = None
    llm_first_token_ms: float | None = None


async def plan_turn(convo: Conversation, user_text: str) -> TurnPlan:
    """Screen the input and assemble the prompt for this turn."""
    risk = screen_input(user_text)
    if risk is Risk.CRISIS:
        logger.warning("Crisis language detected session=%s", convo.session_id)
        return TurnPlan(user_text, risk, [], [], scripted=CRISIS_RESPONSE)

    settings = get_settings()
    # Techniques are withheld under elevated risk: listen first, fix later.
    retrieval = knowledge.retrieve(user_text, settings.knowledge_top_k) if risk is Risk.NONE else _no_techniques()
    history, techniques = await asyncio.gather(cache.get_history(convo.session_id), retrieval)

    system = PERSONA_PROMPT.format(
        memory_block=MEMORY_BLOCK.format(facts="\n".join(f"- {f}" for f in convo.facts)) if convo.facts else "",
        technique_block=TECHNIQUE_BLOCK.format(
            techniques="\n".join(f"- {t.title}: {t.how}" for t in techniques)
        ) if techniques else "",
        risk_block=RISK_BLOCK.format(guidance=ELEVATED_GUIDANCE) if risk is Risk.ELEVATED else "",
    )
    messages = [{"role": "system", "content": system}]
    for turn in history:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["bot"]})
    messages.append({"role": "user", "content": user_text})
    return TurnPlan(user_text, risk, techniques, messages)


async def _no_techniques() -> list[knowledge.Technique]:
    return []


def _clean(sentence: str) -> str:
    return re.sub(r"\s+", " ", _MARKDOWN_RE.sub("", sentence)).strip()


async def sentences(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Re-chunk a token stream into speakable sentences as early as possible."""
    buf = ""
    async for delta in deltas:
        buf += delta
        while True:
            match = _BOUNDARY_RE.search(buf)
            if match:
                head, buf = buf[: match.start()], buf[match.end():]
            elif len(buf) > SENTENCE_MAX_CHARS:
                cut = max(buf.rfind(",", 0, SENTENCE_MAX_CHARS), buf.rfind(" ", 0, SENTENCE_MAX_CHARS))
                cut = cut if cut > 0 else SENTENCE_MAX_CHARS
                head, buf = buf[: cut + 1], buf[cut + 1:]
            else:
                break
            if cleaned := _clean(head):
                yield cleaned
    if cleaned := _clean(buf):
        yield cleaned


async def reply_stream(plan: TurnPlan) -> AsyncIterator[str]:
    """Speakable, safety-screened sentences for this turn."""
    if plan.scripted is not None:
        async for sentence in sentences(_once(plan.scripted)):
            yield sentence
        return

    t0 = time.perf_counter()

    async def timed() -> AsyncIterator[str]:
        async for delta in llm_stream(plan.messages):
            if plan.llm_first_token_ms is None:
                plan.llm_first_token_ms = (time.perf_counter() - t0) * 1000
            yield delta

    async for sentence in sentences(timed()):
        safe, replaced = screen_output(sentence)
        if replaced:
            logger.warning("Output guardrail replaced a sentence: %r", sentence[:120])
        yield safe


async def _once(text: str) -> AsyncIterator[str]:
    yield text


async def finish_turn(
    convo: Conversation,
    plan: TurnPlan,
    spoken: str,
    *,
    interrupted: bool = False,
    stt_ms: float | None = None,
    first_audio_ms: float | None = None,
    total_ms: float | None = None,
) -> str | None:
    """Persist the turn and update memory. Returns the stored turn id.

    `spoken` is what the user actually heard — after a barge-in that is only
    the sentences delivered before the interruption, so history matches the
    user's experience rather than what the model intended to say.
    """
    await cache.add_turn(convo.session_id, plan.user_text, spoken + (" …" if interrupted else ""))

    turn_id = None
    try:
        async with get_session_factory()() as db:
            turn = Turn(
                session_id=convo.session_id,
                user_id=convo.user_id,
                channel=convo.channel,
                user_text=plan.user_text,
                reply=spoken,
                risk=plan.risk.value,
                techniques=",".join(t.id for t in plan.techniques),
                interrupted=int(interrupted),
                stt_ms=stt_ms,
                llm_first_token_ms=plan.llm_first_token_ms,
                first_audio_ms=first_audio_ms,
                total_ms=total_ms,
            )
            db.add(turn)
            await db.commit()
            turn_id = turn.id
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a conversation
        logger.warning("Turn persist failed session=%s: %s", convo.session_id, exc)

    if plan.risk is not Risk.CRISIS:
        async def remember() -> None:
            convo.facts += await memory.extract_and_store(convo.user_id, plan.user_text, spoken, convo.facts)

        _spawn(remember())
    return turn_id
