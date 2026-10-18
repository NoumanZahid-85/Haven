"""
Pipeline mode: Deepgram Flux STT → companion engine (safety, memory, RAG,
Groq LLM stream) → per-sentence safety screen → Deepgram Aura-2 streaming TTS.

Turn-taking comes from Flux's own end-of-turn model — no VAD, no silence timer:

  EagerEndOfTurn  start the reply speculatively: LLM + TTS begin, audio is held
  TurnResumed     user kept talking → drop the speculative reply, nothing heard
  EndOfTurn       commit: release the held audio if the transcript matches,
                  otherwise regenerate from the final transcript
  StartOfTurn     while the bot is speaking and barge-in is on → interrupt

If the user starts talking again before any reply audio went out, the earlier
words are carried into the next turn, so a mid-thought pause never loses text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from app.core.config import get_settings
from app.services import companion, knowledge
from app.voice import deepgram, session_registry
from app.voice.bridge import VoiceBridge
from app.voice.deepgram import FluxSTT

logger = logging.getLogger(__name__)

GREETING = "Hi, I'm Haven. I'm here to listen, whatever's on your mind. How are you feeling right now?"
GREETING_RETURNING = "Hi, it's good to hear from you again. How have things been since we last talked?"
TURN_TIMEOUT_S = 45.0


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


class _Reply:
    """One in-flight reply (possibly speculative)."""

    def __init__(self, text: str, speculative: bool) -> None:
        self.text = text
        self.commit = asyncio.Event()
        if not speculative:
            self.commit.set()
        self.task: asyncio.Task | None = None
        self.audible = asyncio.Event()  # first reply audio has reached the client


class PipelineSession(VoiceBridge):
    mode = "pipeline"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stt = FluxSTT()
        self.reply: _Reply | None = None
        self.carry = ""  # words from a turn the user resumed before hearing a reply
        # Technique retrieval runs on the partial transcript while the user is
        # still speaking (an embedding round trip is ~0.6 s from far regions).
        self._prefetch: tuple[str, asyncio.Task] | None = None
        self._events_task: asyncio.Task | None = None
        self._greeting: asyncio.Task | None = None

    async def start(self) -> None:
        await self.stt.connect()
        self._events_task = asyncio.create_task(self._stt_loop())
        greeting = GREETING_RETURNING if self.convo.facts else GREETING
        self._greeting = asyncio.create_task(self.speak_text(greeting))

    async def stop(self) -> None:
        for task in (self._events_task, self._greeting, self.reply.task if self.reply else None):
            if task and not task.done():
                task.cancel()
        await self.stt.close()

    async def on_audio(self, pcm16: bytes) -> None:
        await self.stt.send(pcm16)

    # ── Flux events ────────────────────────────────────────────────────
    async def _stt_loop(self) -> None:
        try:
            async for ev in self.stt.events():
                if ev.get("type") == "TurnInfo":
                    await self.on_turn_event(ev.get("event", ""), (ev.get("transcript") or "").strip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Flux stream ended session=%s: %s", self.session_id, exc)
            await self.send_json({"type": "error", "message": "Speech recognition disconnected — reconnect to continue."})

    async def on_turn_event(self, event: str, transcript: str) -> None:
        if event != "Update":
            ms = self.energy.since_speech_ms()
            session_registry.record_event(self.session_id, f"flux_{event}", f"+{ms:.0f}ms {transcript[:50]}" if ms else "")
        if event == "StartOfTurn":
            deepgram.touch()
            await self._on_user_started()
        elif event == "Update" and transcript:
            text = self._with_carry(transcript)
            await self.send_json({"type": "user_partial", "text": text})
            self._maybe_prefetch(text)
        elif event == "EagerEndOfTurn" and transcript:
            if self.reply is None:
                self._begin_reply(self._with_carry(transcript), speculative=True)
        elif event == "TurnResumed":
            if self.reply and not self.reply.commit.is_set():
                self._cancel_reply()
        elif event == "EndOfTurn" and transcript:
            await self._on_end_of_turn(self._with_carry(transcript))

    def _with_carry(self, transcript: str) -> str:
        return f"{self.carry} {transcript}".strip() if self.carry else transcript

    async def _on_user_started(self) -> None:
        if self._greeting and not self._greeting.done() and self.barge_in:
            self._greeting.cancel()
            await self.interrupt()
        reply = self.reply
        if reply is None or not reply.commit.is_set():
            return
        if reply.audible.is_set():
            if self.barge_in and self.speaking:
                session_registry.record_event(self.session_id, "barge_in", str(self.turn_no))
                self._cancel_reply()
                await self.interrupt()
        else:
            # Committed but nothing audible yet: the user is continuing their
            # thought. Drop the reply and keep their words for the next turn.
            self.carry = reply.text
            self._cancel_reply()

    async def _on_end_of_turn(self, text: str) -> None:
        # Transcript first: committing can release audio immediately, and the
        # user's words must appear before the reply in the conversation.
        await self.send_json({"type": "transcript", "text": text})
        reply = self.reply
        if reply and not reply.commit.is_set() and _norm(reply.text) == _norm(text):
            reply.commit.set()  # speculation was right — release the held audio
        else:
            if reply:
                self._cancel_reply()
            self._begin_reply(text, speculative=False)
        self.carry = ""
        self._prefetch = None

    def _maybe_prefetch(self, text: str) -> None:
        words = len(text.split())
        if words < 4:
            return
        if self._prefetch and words - len(self._prefetch[0].split()) < 3:
            return  # not enough new words to change the match
        task = asyncio.create_task(knowledge.retrieve(text, get_settings().knowledge_top_k))
        self._prefetch = (text, task)

    async def _prefetched_techniques(self, text: str) -> list[knowledge.Technique] | None:
        """Use the prefetch if it covered most of the final transcript."""
        if not self._prefetch:
            return None
        partial, task = self._prefetch
        if len(partial.split()) < 0.7 * len(text.split()):
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        except Exception:  # noqa: BLE001 — fall back to retrieving inline
            return None

    # ── Replies ────────────────────────────────────────────────────────
    def _begin_reply(self, text: str, *, speculative: bool) -> None:
        reply = _Reply(text, speculative)
        reply.task = asyncio.create_task(self._run_reply(reply, time.perf_counter()))
        self.reply = reply

    def _cancel_reply(self) -> None:
        if self.reply and self.reply.task and not self.reply.task.done():
            self.reply.task.cancel()
        self.reply = None

    async def _run_reply(self, reply: _Reply, t_start: float) -> None:
        plan = None
        spoken: list[str] = []
        first_audio = None
        t_speech_end = self.energy.last_voiced or t_start
        interrupted = False
        speaking: asyncio.Task | None = None
        try:
            techniques = await self._prefetched_techniques(reply.text)
            plan = await companion.plan_turn(self.convo, reply.text, techniques)
            session_registry.record_event(
                self.session_id, "planned", f"{(time.perf_counter() - t_start) * 1000:.0f}ms commit={reply.commit.is_set()}"
            )
            if plan.risk is not companion.Risk.NONE:
                await self.send_json({"type": "risk", "level": plan.risk.value})
            # LLM + TTS start now, even for a speculative reply; speak() holds
            # the audio until the turn is committed.
            speaking = asyncio.create_task(self.speak(
                companion.reply_stream(plan), commit=reply.commit, audible=reply.audible,
                cache=plan.scripted is not None,
            ))
            await reply.commit.wait()
            t_committed = time.perf_counter()
            spoken, first_audio = await asyncio.wait_for(speaking, timeout=TURN_TIMEOUT_S)
            metrics = {
                "stt_ms": (t_committed - t_speech_end) * 1000,
                "llm_first_token_ms": plan.llm_first_token_ms,
                "first_audio_ms": (first_audio - t_speech_end) * 1000 if first_audio else None,
                "total_ms": (time.perf_counter() - t_speech_end) * 1000,
            }
            await self.send_json({"type": "metrics", "turn": self.turn_no, **metrics})
        except asyncio.CancelledError:
            interrupted = True
            metrics = {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline turn failed session=%s", self.session_id)
            session_registry.record_event(self.session_id, "turn_error", str(exc)[:120])
            metrics = {}
            with contextlib.suppress(Exception):
                await self.speak_text("Something went wrong on my side. Could you say that again?")
        finally:
            if speaking and not speaking.done():
                speaking.cancel()
            if self.reply is reply:
                self.reply = None
        # Persist only turns the user actually reached (speculative drops are noise).
        if plan is not None and reply.commit.is_set():
            asyncio.create_task(companion.finish_turn(
                self.convo, plan, " ".join(spoken), interrupted=interrupted,
                stt_ms=metrics.get("stt_ms"), first_audio_ms=metrics.get("first_audio_ms"),
                total_ms=metrics.get("total_ms"),
            ))
