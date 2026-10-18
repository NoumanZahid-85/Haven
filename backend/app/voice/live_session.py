"""
Live mode: native speech-to-speech with Gemini Live, relayed through our server.

Browser PCM goes straight to the model and its audio streams straight back,
so there is no separate STT/TTS hop. Gemini handles turn detection and
barge-in. Our server stays in the loop for the parts a mental-health
product can't hand off:

  memory      long-term facts are written into the session instructions
  techniques  the full catalog is in the instructions (a tool call cost ~0.8 s)
  safety      the model's live transcripts of the user and of itself are
              screened as they arrive. Crisis language or an unsafe reply
              mutes the model for the rest of that turn, flushes the client,
              and speaks the fixed crisis response (or a safe line) in a
              Deepgram voice. In this mode the override is best-effort:
              transcripts trail the audio, so a fragment may already be heard.
  telemetry   every turn is stored with its first-audio latency
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from google.genai import Client, types

from app.core.config import get_settings
from app.services import companion, knowledge
from app.services.prompts import (
    LIVE_GREETING,
    LIVE_GREETING_RETURNING,
    LIVE_SAFETY_BLOCK,
    LIVE_TECHNIQUE_BLOCK,
    MEMORY_BLOCK,
    PERSONA_PROMPT,
)
from app.services.safety import CRISIS_RESPONSE, SAFE_REPLACEMENT, Risk, screen_input, screen_output
from app.voice import session_registry
from app.voice.bridge import VoiceBridge

logger = logging.getLogger(__name__)

SUPPRESS_MAX_S = 20.0  # never mute the model longer than this, even if a turn_complete is lost


def build_instructions(facts: list[str]) -> str:
    persona = PERSONA_PROMPT.format(
        memory_block=MEMORY_BLOCK.format(facts="\n".join(f"- {f}" for f in facts)) if facts else "",
        technique_block=LIVE_TECHNIQUE_BLOCK.format(
            techniques="\n".join(f"- {t.title} (for {t.when}): {t.how}" for t in knowledge.TECHNIQUES)
        ),
        risk_block="",
    )
    return persona + LIVE_SAFETY_BLOCK


class LiveSession(VoiceBridge):
    mode = "live"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cm = None
        self.session = None
        self._recv_task: asyncio.Task | None = None
        self._override_task: asyncio.Task | None = None
        self._reset_turn()
        self.suppress_until = 0.0

    def _reset_turn(self) -> None:
        self.user_text = ""
        self.reply_text = ""
        self.turn_open = False
        self.risk = Risk.NONE
        self.first_audio: float | None = None
        self.t_speech_end: float | None = None

    @property
    def suppressed(self) -> bool:
        return time.perf_counter() < self.suppress_until

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        settings = get_settings()
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=build_instructions(self.convo.facts),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.live_voice)
                )
            ),
            # Long conversations: slide the context window instead of hitting the session cap.
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        )
        client = Client(api_key=settings.gemini_api_key)
        self._cm = client.aio.live.connect(model=settings.live_model, config=config)
        self.session = await self._cm.__aenter__()
        self._recv_task = asyncio.create_task(self._receive_loop())
        greeting = LIVE_GREETING_RETURNING if self.convo.facts else LIVE_GREETING
        await self.session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=greeting)]), turn_complete=True
        )

    async def stop(self) -> None:
        for task in (self._recv_task, self._override_task):
            if task and not task.done():
                task.cancel()
        if self._cm is not None:
            with contextlib.suppress(Exception):  # already closed
                await self._cm.__aexit__(None, None, None)

    async def on_audio(self, pcm16: bytes) -> None:
        if self.session is not None:
            await self.session.send_realtime_input(audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000"))

    # ── Model → client ─────────────────────────────────────────────────
    async def _receive_loop(self) -> None:
        assert self.session is not None
        try:
            while True:  # receive() ends after each turn_complete; keep listening
                async for msg in self.session.receive():
                    await self.handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live stream ended session=%s: %s", self.session_id, exc)
            session_registry.record_event(self.session_id, "live_error", str(exc)[:200])
            await self.send_json({"type": "error", "message": "Live session ended — reconnect, or switch to Pipeline mode."})

    async def handle(self, msg) -> None:
        sc = msg.server_content
        if sc is None:
            if getattr(msg, "go_away", None):
                session_registry.record_event(self.session_id, "go_away", str(msg.go_away.time_left))
            return

        if sc.input_transcription and sc.input_transcription.text:
            await self._on_user_words(sc.input_transcription.text)

        if sc.model_turn and not self.suppressed:
            for part in sc.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    await self._on_model_audio(part.inline_data.data)

        if sc.output_transcription and sc.output_transcription.text and not self.suppressed:
            text = sc.output_transcription.text
            self.reply_text += text
            await self.send_json({"type": "agent_caption", "text": text})
            _, unsafe = screen_output(self.reply_text)
            if unsafe:
                logger.warning("Live output guardrail tripped session=%s", self.session_id)
                await self._override(SAFE_REPLACEMENT, self.risk)

        if sc.interrupted and self.turn_open:
            # Gemini's own barge-in: the user talked over the reply.
            await self.interrupt()
            self._persist(interrupted=True)
            self._reset_turn()

        if sc.turn_complete:
            if self.suppressed:
                self.suppress_until = 0.0  # the muted generation is over
            elif self.turn_open:
                await self.send_json({"type": "reply_end", "turn": self.turn_no})
                metrics = self._metrics()
                await self.send_json({"type": "metrics", "turn": self.turn_no, **metrics})
                self._persist(interrupted=False)
            self._reset_turn()

    async def _on_user_words(self, text: str) -> None:
        self.user_text += text
        await self.send_json({"type": "user_partial", "text": self.user_text.strip()})
        risk = screen_input(self.user_text)
        if risk is Risk.CRISIS and self.risk is not Risk.CRISIS:
            self.risk = Risk.CRISIS
            await self._override(CRISIS_RESPONSE, Risk.CRISIS)
        elif risk is Risk.ELEVATED and self.risk is Risk.NONE:
            self.risk = Risk.ELEVATED
            await self.send_json({"type": "risk", "level": "elevated"})

    async def _on_model_audio(self, pcm: bytes) -> None:
        if not self.turn_open:
            self.turn_open = True
            self.t_speech_end = self.energy.last_voiced or None
            turn = self.open_turn()
            if self.user_text.strip():
                await self.send_json({"type": "transcript", "text": self.user_text.strip()})
            await self.send_json({"type": "speaking_start", "turn": turn})
        if self.first_audio is None:
            self.first_audio = time.perf_counter()
        await self.send_audio(pcm)

    # ── Safety override ────────────────────────────────────────────────
    async def _override(self, text: str, risk: Risk) -> None:
        """Mute the model for this turn and speak a fixed line instead."""
        self.suppress_until = time.perf_counter() + SUPPRESS_MAX_S
        if self.turn_open:
            await self.interrupt()
        await self.send_json({"type": "risk", "level": risk.value})
        if self.user_text.strip():
            await self.send_json({"type": "transcript", "text": self.user_text.strip()})
        plan = companion.TurnPlan(self.user_text.strip(), risk, [], [], scripted=text)
        self._reset_turn()
        self.risk = risk

        async def run() -> None:
            spoken, _ = await self.speak_text(text)
            await companion.finish_turn(self.convo, plan, " ".join(spoken))

        if self._override_task and not self._override_task.done():
            self._override_task.cancel()
        self._override_task = asyncio.create_task(run())

    # ── Telemetry ──────────────────────────────────────────────────────
    def _metrics(self) -> dict:
        end = self.t_speech_end
        now = time.perf_counter()
        return {
            "stt_ms": None,
            "llm_first_token_ms": None,
            "first_audio_ms": (self.first_audio - end) * 1000 if end and self.first_audio else None,
            "total_ms": (now - end) * 1000 if end else None,
        }

    def _persist(self, *, interrupted: bool) -> None:
        user_text = self.user_text.strip()
        if not user_text:  # the greeting, or a turn with no recognized speech
            return
        plan = companion.TurnPlan(user_text, self.risk, [], [])
        m = self._metrics()
        asyncio.create_task(companion.finish_turn(
            self.convo, plan, self.reply_text.strip(), interrupted=interrupted,
            first_audio_ms=m["first_audio_ms"], total_ms=m["total_ms"],
        ))
