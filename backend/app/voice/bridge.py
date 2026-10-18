"""
Browser-facing half of a voice session, shared by Live and Pipeline modes.

Protocol (identical for both modes — the browser doesn't know which is running)
--------------------------------------------------------------------------------
Client → server : binary  PCM16 mono @16 kHz mic audio. In half-duplex mode the
                          client sends silence while the bot has the floor, so
                          upstream recognizers always get a continuous stream.
                  text    {"type": "playback_done", "turn": n}
Server → client : binary  PCM16 mono @24 kHz reply audio, streamed in chunks
                  text    {"type": "ready", "mode", "barge_in"}
                          {"type": "user_partial", "text"}     words so far, while speaking
                          {"type": "transcript", "text"}       the user's finished turn
                          {"type": "speaking_start", "turn"}
                          {"type": "agent_caption", "text"}    reply text, as it's spoken
                          {"type": "reply_end", "turn"}        all audio for turn n sent
                          {"type": "interrupt", "turn"}        flush playback now
                          {"type": "risk", "level"}
                          {"type": "metrics", ...}             per-turn latency (ms)
                          {"type": "error", "message"}

Latency is measured from the *end of the user's speech*, taken from the
energy of the incoming audio — not from when a recognizer decided the turn
was over — so the number is what the user actually waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

import numpy as np

from app.services import companion
from app.voice import barge, deepgram, session_registry

logger = logging.getLogger(__name__)

TTS_CONCURRENCY = 3


class EnergyTracker:
    """Timestamp of the last audio frame that contained speech energy.

    An adaptive noise floor keeps steady background noise from counting as
    speech; the client's half-duplex silence frames never count.
    """

    def __init__(self) -> None:
        self.noise = 200.0
        self.last_voiced = 0.0

    def update(self, pcm16: bytes) -> None:
        if len(pcm16) < 2:
            return
        samples = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples)))
        threshold = max(400.0, self.noise * 3.0)
        if rms > threshold:
            self.last_voiced = time.perf_counter()
        else:
            self.noise = 0.95 * self.noise + 0.05 * rms

    def since_speech_ms(self, t: float | None = None) -> float | None:
        if not self.last_voiced:
            return None
        return ((t or time.perf_counter()) - self.last_voiced) * 1000


async def _once(text: str) -> AsyncIterator[str]:
    yield text


class VoiceBridge:
    mode = "voice"

    def __init__(self, ws, session_id: str, user_id: str = "", barge_in: bool = True, lang: str = "en") -> None:
        self.ws = ws
        self.session_id = session_id
        self.barge_in = barge_in
        self.lang = lang
        self.voice = deepgram.voice_for(lang)
        self.convo = companion.Conversation(session_id=session_id, user_id=user_id, channel=self.mode, lang=lang)
        self.bot_said = ""  # what Haven has said this turn — the interruption judge's echo reference
        self.energy = EnergyTracker()
        self.turn_no = 0
        self.speaking = False  # bot audio is out and the client hasn't finished playing it
        self.closed = False

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def run(self) -> None:
        session_registry.record_event(self.session_id, "connected", f"{self.mode} lang={self.lang} barge_in={self.barge_in}")
        await self.convo.load()
        try:
            await self.start()
            await self.send_json({"type": "ready", "mode": self.mode, "lang": self.lang, "barge_in": self.barge_in})
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    self.energy.update(data)
                    await self.on_audio(data)
                elif (text := message.get("text")) is not None:
                    self._on_control(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s session %s ended: %s", self.mode, self.session_id, exc)
            session_registry.record_event(self.session_id, "session_error", str(exc)[:200])
            await self.send_json({"type": "error", "message": "Voice service unavailable — try the other mode or text."})
        finally:
            self.closed = True
            await self.stop()
            session_registry.record_event(self.session_id, "disconnected")

    def _on_control(self, text: str) -> None:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(text)
            if payload.get("type") == "playback_done" and payload.get("turn") == self.turn_no:
                self.speaking = False

    # Subclass hooks
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def on_audio(self, pcm16: bytes) -> None: ...

    # ── Output ─────────────────────────────────────────────────────────
    async def send_json(self, payload: dict) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):  # socket closing; the receive loop will exit
            await self.ws.send_text(json.dumps(payload))

    async def send_audio(self, pcm: bytes) -> None:
        if not self.closed:
            with contextlib.suppress(Exception):
                await self.ws.send_bytes(pcm)

    def open_turn(self) -> int:
        self.turn_no += 1
        self.speaking = True
        self.bot_said = ""
        return self.turn_no

    def judge_heard(self, heard: str) -> barge.Verdict:
        """Judge words heard while Haven is speaking, and tell the client what
        was decided, so a kept-going "mm-hmm" is visible, not mysterious."""
        verdict = barge.judge(heard, self.bot_said[-400:])
        if verdict.reason in ("backchannel", "echo", "stop-phrase", "takeover"):
            session_registry.record_event(self.session_id, f"barge_{verdict.reason}", heard[:60])
            asyncio.create_task(self.send_json({
                "type": "heard", "text": heard, "interrupt": verdict.interrupt, "reason": verdict.reason,
            }))
        return verdict

    async def interrupt(self) -> None:
        """Tell the client to drop everything it has queued for this turn."""
        session_registry.record_event(self.session_id, "interrupt", str(self.turn_no))
        self.speaking = False
        await self.send_json({"type": "interrupt", "turn": self.turn_no})

    async def speak(
        self,
        sentences: AsyncIterator[str],
        *,
        commit: asyncio.Event | None = None,
        audible: asyncio.Event | None = None,
        cache: bool = False,
    ) -> tuple[list[str], float | None]:
        """Stream sentences through Deepgram TTS to the client, in order.

        Each sentence's synthesis starts as soon as the sentence exists, so
        sentence 2 is synthesizing while sentence 1 is playing. Nothing is
        sent until `commit` is set — that's how a speculative reply (started
        on Flux's eager end-of-turn) can be prepared but never heard if the
        user keeps talking. `audible` is set once the first chunk is sent.
        Returns (spoken sentences, time of first audio).
        """
        sem = asyncio.Semaphore(TTS_CONCURRENCY)
        order: asyncio.Queue[tuple[str, asyncio.Queue[bytes | None]] | None] = asyncio.Queue()
        tasks: list[asyncio.Task] = []

        async def synth(text: str, out: asyncio.Queue[bytes | None]) -> None:
            try:
                async with sem:
                    async for chunk in deepgram.tts_stream(text, cache=cache, voice=self.voice):
                        await out.put(chunk)
            except Exception as exc:  # noqa: BLE001 — one failed sentence must not kill the reply
                logger.error("TTS failed: %s", exc)
                session_registry.record_event(self.session_id, "tts_fail", str(exc)[:120])
            finally:
                await out.put(None)

        async def produce() -> None:
            async for sentence in sentences:
                q: asyncio.Queue[bytes | None] = asyncio.Queue()
                tasks.append(asyncio.create_task(synth(sentence, q)))
                await order.put((sentence, q))
            await order.put(None)

        producer = asyncio.create_task(produce())
        spoken: list[str] = []
        first_audio: float | None = None
        turn = None
        try:
            if commit is not None:
                await commit.wait()
            while (item := await order.get()) is not None:
                sentence, q = item
                captioned = False
                while (chunk := await q.get()) is not None:
                    if turn is None:
                        turn = self.open_turn()
                        await self.send_json({"type": "speaking_start", "turn": turn})
                    if not captioned:
                        # Captions are concatenated verbatim by the client (Live sends
                        # word fragments), so whole sentences carry their own space.
                        await self.send_json({"type": "agent_caption", "text": sentence + " "})
                        self.bot_said += sentence + " "
                        captioned = True
                    await self.send_audio(chunk)
                    if first_audio is None:
                        first_audio = time.perf_counter()
                        if audible is not None:
                            audible.set()
                if captioned:
                    spoken.append(sentence)
            await producer  # surface LLM errors
        finally:
            producer.cancel()
            for t in tasks:
                t.cancel()
        if turn is not None:
            await self.send_json({"type": "reply_end", "turn": turn})
        else:
            await self.send_json({"type": "error", "message": "Speech synthesis failed — please try again."})
        return spoken, first_audio

    async def speak_text(self, text: str, *, cache: bool = True) -> tuple[list[str], float | None]:
        return await self.speak(companion.sentences(_once(text)), cache=cache)
