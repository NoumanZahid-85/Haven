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

The engine knows nothing about audio. voice/pipeline_session.py turns the sentence
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
from app.services.llm import llm_generate, llm_stream
from app.services.prompts import (
    GREETING_PROMPT,
    LANGUAGE_BLOCK_DE,
    MEMORY_BLOCK,
    PERSONA_PROMPT,
    RISK_BLOCK,
    TECHNIQUE_BLOCK,
    WINS_BLOCK,
)
from app.services.safety import ELEVATED_GUIDANCE, Risk, crisis_response, screen_input, screen_output

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
    lang: str = "en"  # "en" | "de"
    facts: list[str] = field(default_factory=list)
    wins: list[str] = field(default_factory=list)

    async def load(self) -> Conversation:
        self.facts, self.wins = await asyncio.gather(
            memory.load_facts(self.user_id), memory.load_wins(self.user_id)
        )
        return self

    def context_blocks(self) -> dict[str, str]:
        """Prompt blocks every engine shares: memory, wins and language."""
        return {
            "memory_block": MEMORY_BLOCK.format(facts="\n".join(f"- {f}" for f in self.facts)) if self.facts else "",
            "wins_block": WINS_BLOCK.format(wins="\n".join(f"- {w}" for w in self.wins)) if self.wins else "",
            "language_block": LANGUAGE_BLOCK_DE if self.lang == "de" else "",
        }


GREETING = {
    "en": "Hi, I'm Haven. I'm here to listen, whatever's on your mind. How are you feeling right now?",
    "de": "Hi, ich bin Haven. Ich bin da und höre dir zu, was auch immer dich beschäftigt. Wie geht es dir gerade?",
}
GREETING_RETURNING = {
    "en": "Hi, it's good to hear from you again. How have things been since we last talked?",
    "de": "Hi, schön, dass du wieder da bist. Wie ist es dir seit unserem letzten Gespräch ergangen?",
}
GREETING_TIMEOUT_S = 2.5


async def greeting(convo: Conversation) -> str:
    """Open with a follow-up on what the person was facing, if Haven remembers
    anything ("How did the pitch go?"), otherwise a fixed greeting."""
    lang = convo.lang if convo.lang in GREETING else "en"
    if not convo.facts:
        return GREETING[lang]
    try:
        text = await asyncio.wait_for(
            llm_generate(
                [{
                    "role": "user",
                    "content": GREETING_PROMPT.format(
                        facts="\n".join(f"- {f}" for f in convo.facts[-6:]),
                        language="German (use du)" if lang == "de" else "English",
                    ),
                }],
                temperature=0.7,
                max_tokens=300,
            ),
            timeout=GREETING_TIMEOUT_S,
        )
        text = _clean(text.strip().strip('"'))
        if text and len(text) < 220:
            return text
    except Exception as exc:  # noqa: BLE001 — a generic greeting is fine
        logger.info("Personal greeting skipped: %s", exc)
    return GREETING_RETURNING[lang]


@dataclass
class TurnPlan:
    user_text: str
    risk: Risk
    techniques: list[knowledge.Technique]
    messages: list[dict[str, str]]
    scripted: str | None = None
    llm_first_token_ms: float | None = None
    lang: str = "en"


async def plan_turn(
    convo: Conversation,
    user_text: str,
    techniques: list[knowledge.Technique] | None = None,
) -> TurnPlan:
    """Screen the input and assemble the prompt for this turn.

    `techniques` lets a voice engine pass retrieval it already ran on the
    partial transcript while the user was still speaking, which takes the
    embedding round trip off the critical path.
    """
    risk = screen_input(user_text)
    if risk is Risk.CRISIS:
        logger.warning("Crisis language detected session=%s", convo.session_id)
        return TurnPlan(user_text, risk, [], [], scripted=crisis_response(convo.lang), lang=convo.lang)

    settings = get_settings()
    # Techniques are withheld under elevated risk: listen first, fix later.
    if risk is not Risk.NONE:
        retrieval = _no_techniques()
    elif techniques is not None:
        retrieval = _given(techniques)
    else:
        retrieval = knowledge.retrieve(user_text, settings.knowledge_top_k)
    history, techniques = await asyncio.gather(cache.get_history(convo.session_id), retrieval)

    system = PERSONA_PROMPT.format(
        **convo.context_blocks(),
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
    return TurnPlan(user_text, risk, techniques, messages, lang=convo.lang)


async def _no_techniques() -> list[knowledge.Technique]:
    return []


async def _given(techniques: list[knowledge.Technique]) -> list[knowledge.Technique]:
    return techniques


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
        safe, replaced = screen_output(sentence, plan.lang)
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
            convo.facts += await memory.extract_and_store(
                convo.user_id, plan.user_text, spoken, convo.facts, session_id=convo.session_id
            )

        _spawn(remember())
    return turn_id
