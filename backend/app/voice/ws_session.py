"""
Pure-WebSocket voice pipeline: Silero VAD → Whisper → companion → Orpheus.

Protocol
--------
Client → server : binary frames = raw PCM16 mono @16 kHz mic audio
                  text frames  = JSON {"type": "playback_done", "turn": n}
Server → client : text frames  = JSON events
                    {"type": "ready"}
                    {"type": "speaking_start", "turn": n}  bot audio for turn n begins
                    {"type": "transcript", "text"}          user's words (Whisper)
                    {"type": "agent_caption", "text"}       caption for the next WAV
                    {"type": "reply_end", "turn": n}        all audio for turn n has been sent
                    {"type": "interrupt"}                   user barged in: stop playback now
                    {"type": "metrics", ...}                per-turn latency breakdown (ms)
                    {"type": "risk", "level"}               safety tier for the turn
                    {"type": "error", "message"}
                  binary frames = one complete WAV per sentence (Orpheus, 24 kHz mono)

Latency: streaming, not batch
-----------------------------
The LLM reply is consumed as a token stream and cut into sentences as they
complete. Each sentence is sent to TTS the moment it exists, while later
sentences are still being generated; WAVs are delivered strictly in order.
Time-to-first-audio is therefore STT + LLM-first-sentence + one TTS call,
not STT + whole reply + all TTS.

Turn-taking: half-duplex by default, barge-in opt-in
----------------------------------------------------
Echo was the recurring failure of the first (Daily/Pipecat) build: the bot's
own voice leaked into the mic, got transcribed, and the agent answered
itself. Default mode is half-duplex — inbound audio is discarded while the
bot has the floor, so leakage can never be transcribed by construction.

With ?barge_in=1 (best with headphones) the server keeps running VAD while
the bot speaks, with a stricter threshold and a minimum duration. Sustained
user speech cancels the in-flight reply (LLM stream + pending TTS), tells the
client to flush its playback queue, and the speech that triggered it becomes
the start of the next utterance — nothing the user said is lost. Only the
sentences actually delivered are written to history.

Every reply carries a turn number; a late playback_done from an interrupted
turn can't reopen the mic in the middle of the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
import wave
from collections import OrderedDict, deque
from collections.abc import AsyncIterator

import numpy as np
from groq import AsyncGroq

from app.core.config import get_settings
from app.services import companion
from app.voice import session_registry

logger = logging.getLogger(__name__)

# ── Audio / VAD constants ────────────────────────────────────────────
SAMPLE_RATE = 16000
VAD_WINDOW_BYTES = 1024          # 512 int16 samples @16 kHz = 32 ms (Silero window)
START_CONFIDENCE = 0.5           # speech probability to open a turn
END_CONFIDENCE = 0.35            # below this counts as silence
MIN_SPEECH_WINDOWS = 3           # consecutive speech messages before we commit to capture
SILENCE_END_S = 1.1              # silence that closes the turn
MAX_UTTERANCE_S = 30.0           # safety cap on a single utterance
PREROLL_WINDOWS = 18             # client frames (~85 ms each) kept before onset so it isn't clipped

# Barge-in is deliberately harder to trigger than turn start: residual echo
# and a cough must not cut the bot off mid-sentence.
BARGE_CONFIDENCE = 0.75
BARGE_MIN_SPEECH_S = 0.35

# ── Model constants (Groq-only stack) ────────────────────────────────
STT_MODEL = "whisper-large-v3-turbo"
TTS_MODEL = "canopylabs/orpheus-v1-english"
TTS_VOICE = "autumn"
TTS_CHAR_LIMIT = 180             # hard API limit is 200; stay safely under
TTS_CONCURRENCY = 3
STT_TIMEOUT_S = 12.0
TTS_TIMEOUT_S = 30.0
TURN_TIMEOUT_S = 45.0

GREETING = "Hi, I'm Haven. I'm here to listen, whatever's on your mind. How are you feeling right now?"
GREETING_RETURNING = "Hi, it's good to hear from you again. How have things been since we last talked?"

_TAG_RE = re.compile(r"\[[a-z]{1,16}\]", re.IGNORECASE)

# ── TTS audio cache ─────────────────────────────────────────────────
# Synthesized WAVs are deterministic per (model, voice, text): greetings and
# the crisis script are synthesized once per process, then served from memory.
# That makes repeat crisis responses start speaking in ~0.4 s, and it is the
# biggest saver on a rate-limited TTS quota.
_TTS_CACHE_MAX = 128
_tts_cache: OrderedDict[str, bytes] = OrderedDict()


def _tts_cache_get(text: str) -> bytes | None:
    wav = _tts_cache.get(text)
    if wav is not None:
        _tts_cache.move_to_end(text)
    return wav


def _tts_cache_put(text: str, wav: bytes) -> None:
    _tts_cache[text] = wav
    _tts_cache.move_to_end(text)
    while len(_tts_cache) > _TTS_CACHE_MAX:
        _tts_cache.popitem(last=False)


def _clean_caption(text: str) -> str:
    """Strip Orpheus vocal-direction tags ([cheerful] …) from display text."""
    return _TAG_RE.sub("", text).strip()


def _split_for_tts(sentence: str) -> list[str]:
    """Split one sentence into ≤TTS_CHAR_LIMIT pieces at commas, then spaces."""
    if len(sentence) <= TTS_CHAR_LIMIT:
        return [sentence]
    cut = sentence.rfind(",", 0, TTS_CHAR_LIMIT)
    if cut < TTS_CHAR_LIMIT // 2:                       # comma too early → space
        cut = sentence.rfind(" ", 0, TTS_CHAR_LIMIT)
    if cut <= 0:                                        # no break point → hard cut
        cut = TTS_CHAR_LIMIT
    head, tail = sentence[: cut + 1].strip(), sentence[cut + 1:].strip()
    return ([head] if head else []) + (_split_for_tts(tail) if tail else [])


def _pcm_to_wav(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM16 mono bytes in a minimal WAV container for Whisper."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


class SileroGate:
    """Streaming wrapper over pipecat's bundled Silero ONNX model.

    Accepts arbitrary-size PCM16 byte fragments and yields at most one
    speech-probability score (the max over complete 32 ms windows) per call.
    """

    def __init__(self) -> None:
        from pipecat.audio.vad.silero import SileroVADAnalyzer

        self._analyzer = SileroVADAnalyzer(sample_rate=SAMPLE_RATE)
        # CRITICAL: VADAnalyzer.__init__ leaves the internal sample rate at 0;
        # the transport framework normally calls set_sample_rate() during
        # pipeline wiring. Bare construction means every voice_confidence()
        # call hits the model with sr=0, raises "Supported sampling rates:
        # [8000, 16000]", and the caller sees a silent permanent 0.0 — the bot
        # never hears anything. Speech scored 0.995 after this line, 0.000 without.
        self._analyzer.set_sample_rate(SAMPLE_RATE)
        self._buf = bytearray()
        self._warned = False

    def feed(self, pcm16: bytes) -> float | None:
        self._buf += pcm16
        confidences: list[float] = []
        while len(self._buf) >= VAD_WINDOW_BYTES:
            window = bytes(self._buf[:VAD_WINDOW_BYTES])
            del self._buf[:VAD_WINDOW_BYTES]
            try:
                # voice_confidence returns a shape-(1,) ndarray, not a float.
                arr = np.asarray(self._analyzer.voice_confidence(window)).reshape(-1)
                confidences.append(float(arr[0]) if arr.size else 0.0)
            except Exception as exc:  # noqa: BLE001 — VAD hiccups must not kill the stream
                if not self._warned:
                    logger.error("Silero analysis failed: %s", exc)
                    self._warned = True
                return None
        return max(confidences) if confidences else None


class VoiceWSSession:
    """One browser voice connection."""

    def __init__(self, ws, session_id: str, user_id: str = "", barge_in: bool = False) -> None:
        self.ws = ws
        self.session_id = session_id
        self.barge_in = barge_in
        self.convo = companion.Conversation(session_id=session_id, user_id=user_id, channel="voice")
        self.groq = AsyncGroq(api_key=get_settings().groq_api_key)
        self.vad = SileroGate()

        # Floor control. busy: a turn is being processed. speaking: reply
        # audio is out and the client hasn't reported playback_done for it.
        self.busy = False
        self.speaking = False
        self.turn_no = 0
        self._audio_out = False             # first WAV of the current turn has been sent
        self._turn_task: asyncio.Task | None = None
        self._barge_s = 0.0

        # VAD state machine
        self._state = "idle"                # idle | capturing
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_WINDOWS)
        self._utterance = bytearray()
        self._speech_windows = 0
        self._silence_s = 0.0
        self._captured_s = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def run(self) -> None:
        session_registry.record_event(self.session_id, "ws_connected", "barge_in" if self.barge_in else "half_duplex")
        await self.convo.load()
        await self._send_json({"type": "ready", "session_id": self.session_id, "barge_in": self.barge_in})
        greeting = GREETING_RETURNING if self.convo.facts else GREETING
        self._start_turn(self._safe_speak(greeting))

        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    self._feed_vad(data)
                elif (text := message.get("text")) is not None:
                    self._on_control(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS loop ended for session=%s: %s", self.session_id, exc)
        finally:
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()
            session_registry.record_event(self.session_id, "ws_disconnected")

    def _on_control(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if payload.get("type") == "playback_done" and payload.get("turn", self.turn_no) == self.turn_no:
            self.speaking = False
            if self._turn_task is None or self._turn_task.done():
                self.busy = False
            self._preroll.clear()  # audio from while the bot spoke is stale (and may be echo)
            session_registry.record_event(self.session_id, "playback_done", str(self.turn_no))

    def _start_turn(self, coro) -> None:
        self.busy = True
        self.turn_no += 1
        self._audio_out = False
        self._barge_s = 0.0
        self._turn_task = asyncio.create_task(coro)
        self._turn_task.add_done_callback(self._on_turn_done)

    def _on_turn_done(self, task: asyncio.Task) -> None:
        # If playback_done arrived while this turn was still finishing (e.g.
        # a late sentence stuck in TTS retry), nobody else will release the
        # floor — without this the mic stays latched shut for the session.
        if task is self._turn_task and not self.speaking:
            self.busy = False

    # ── VAD state machine ─────────────────────────────────────────────
    def _feed_vad(self, pcm16: bytes) -> None:
        bot_has_floor = self.busy or self.speaking
        if bot_has_floor and not (self.barge_in and self._audio_out):
            return  # half-duplex (or still thinking): discard

        confidence = self.vad.feed(pcm16)
        if confidence is None:
            return
        # Duration comes from the payload — client message sizes vary.
        chunk_s = (len(pcm16) // 2) / SAMPLE_RATE

        if bot_has_floor:
            self._preroll.append(pcm16)
            self._barge_s = self._barge_s + chunk_s if confidence >= BARGE_CONFIDENCE else 0.0
            if self._barge_s >= BARGE_MIN_SPEECH_S:
                self._interrupt()
            return

        if self._state == "idle":
            self._preroll.append(pcm16)
            self._speech_windows = self._speech_windows + 1 if confidence >= START_CONFIDENCE else 0
            if self._speech_windows >= MIN_SPEECH_WINDOWS:
                self._begin_capture()
        else:
            self._utterance += pcm16
            self._captured_s += chunk_s
            self._silence_s = 0.0 if confidence >= END_CONFIDENCE else self._silence_s + chunk_s
            if self._silence_s >= SILENCE_END_S or self._captured_s >= MAX_UTTERANCE_S:
                utterance = bytes(self._utterance)
                self._state = "idle"
                self._utterance.clear()
                self._speech_windows = 0
                if len(utterance) > SAMPLE_RATE * 2 * 0.4:   # ignore blips <0.4 s
                    self._start_turn(self._handle_utterance(utterance, time.perf_counter()))

    def _begin_capture(self) -> None:
        self._state = "capturing"
        self._utterance = bytearray(b"".join(self._preroll))
        self._silence_s = 0.0
        self._captured_s = len(self._utterance) / 2 / SAMPLE_RATE
        session_registry.record_event(self.session_id, "vad_start")

    def _interrupt(self) -> None:
        """User barged in: kill the reply, flush the client, start listening."""
        session_registry.record_event(self.session_id, "barge_in", str(self.turn_no))
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self.busy = False
        self.speaking = False
        self._barge_s = 0.0
        asyncio.create_task(self._send_json({"type": "interrupt", "turn": self.turn_no}))
        self._begin_capture()  # the speech that interrupted is the next utterance's start

    # ── STT → companion → TTS turn ────────────────────────────────────
    async def _handle_utterance(self, pcm16: bytes, t_end: float) -> None:
        plan = None
        spoken: list[str] = []
        timings: dict[str, float | None] = {"stt_ms": None, "first_audio_ms": None}
        interrupted = False
        try:
            transcript_obj = await asyncio.wait_for(
                self.groq.audio.transcriptions.create(
                    model=STT_MODEL,
                    file=("audio.wav", _pcm_to_wav(pcm16), "audio/wav"),
                    language="en",
                ),
                timeout=STT_TIMEOUT_S,
            )
            timings["stt_ms"] = (time.perf_counter() - t_end) * 1000
            user_text = (transcript_obj.text or "").strip()
            session_registry.record_event(self.session_id, "stt", user_text[:70])
            if not user_text:
                return
            await self._send_json({"type": "transcript", "text": user_text})

            plan = await companion.plan_turn(self.convo, user_text)
            if plan.risk is not companion.Risk.NONE:
                await self._send_json({"type": "risk", "level": plan.risk.value})

            await asyncio.wait_for(
                self._speak(companion.reply_stream(plan), spoken, timings, t_end),
                timeout=TURN_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            interrupted = True
        except TimeoutError:
            await self._safe_speak("Sorry, I lost my train of thought for a second. Could you say that again?")
        except Exception as exc:  # noqa: BLE001 — one bad turn must not kill the socket
            logger.exception("Utterance handling failed session=%s", self.session_id)
            session_registry.record_event(self.session_id, "turn_error", str(exc)[:120])
            await self._safe_speak("Something went wrong on my side. Could you try that again?")
        finally:
            if not self.speaking:
                self.busy = False
            if plan is not None:
                total_ms = (time.perf_counter() - t_end) * 1000
                metrics = {
                    "stt_ms": timings["stt_ms"],
                    "llm_first_token_ms": plan.llm_first_token_ms,
                    "first_audio_ms": timings["first_audio_ms"],
                    "total_ms": total_ms,
                }
                if not interrupted:
                    await self._send_json({"type": "metrics", "turn": self.turn_no, **metrics})
                asyncio.create_task(companion.finish_turn(
                    self.convo, plan, " ".join(spoken), interrupted=interrupted,
                    stt_ms=timings["stt_ms"], first_audio_ms=timings["first_audio_ms"], total_ms=total_ms,
                ))

    async def _safe_speak(self, text: str) -> None:
        try:
            await self._speak_text(text)
        except Exception:  # noqa: BLE001
            logger.exception("Fallback audio also failed session=%s", self.session_id)
            self.speaking = False
            self.busy = False

    async def _speak_text(self, text: str) -> None:
        await self._speak(companion.sentences(_once(text)), [], {}, time.perf_counter())

    # ── Streaming TTS: synthesize sentences concurrently, deliver in order ──
    async def _speak(
        self,
        sentence_stream: AsyncIterator[str],
        spoken: list[str],
        timings: dict[str, float | None],
        t_end: float,
    ) -> None:
        sem = asyncio.Semaphore(TTS_CONCURRENCY)
        queue: asyncio.Queue[tuple[str, asyncio.Task[bytes | None]] | None] = asyncio.Queue()
        synth_tasks: list[asyncio.Task] = []

        async def produce() -> None:
            async for sentence in sentence_stream:
                for piece in _split_for_tts(sentence):
                    task = asyncio.create_task(self._synthesize(piece, sem))
                    synth_tasks.append(task)
                    await queue.put((piece, task))
            await queue.put(None)

        producer = asyncio.create_task(produce())
        turn = self.turn_no
        try:
            self.speaking = True
            await self._send_json({"type": "speaking_start", "turn": turn})
            while (item := await queue.get()) is not None:
                piece, task = item
                wav = await task
                if wav is None:
                    continue
                await self._send_json({"type": "agent_caption", "text": _clean_caption(piece)})
                await self.ws.send_bytes(wav)
                if not self._audio_out:
                    self._audio_out = True
                    timings["first_audio_ms"] = (time.perf_counter() - t_end) * 1000
                spoken.append(_clean_caption(piece))
            await producer  # surface LLM errors
        finally:
            producer.cancel()
            for task in synth_tasks:
                task.cancel()

        # Explicit end-of-reply: the client can't tell "queue empty because
        # the reply is done" from "queue empty because the next sentence is
        # still being synthesized" without it.
        await self._send_json({"type": "reply_end", "turn": turn})

        if not spoken:
            # Nothing made it out — release the floor instead of waiting for
            # a playback_done that will never come.
            self.speaking = False
            await self._send_json({"type": "error", "message": "Speech synthesis failed — please try again."})

    async def _synthesize(self, text: str, sem: asyncio.Semaphore) -> bytes | None:
        if (cached := _tts_cache_get(text)) is not None:
            return cached
        async with sem:
            # 429 backoff: the free-tier Orpheus quota is small and bursty.
            for attempt, delay in enumerate((0.0, 2.0, 6.0)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    resp = await asyncio.wait_for(
                        self.groq.audio.speech.create(
                            model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="wav",
                        ),
                        timeout=TTS_TIMEOUT_S,
                    )
                    wav = await resp.read()
                    _tts_cache_put(text, wav)
                    return wav
                except Exception as exc:  # noqa: BLE001
                    rate_limited = "429" in str(exc) or "rate limit" in str(exc).lower()
                    session_registry.record_event(self.session_id, "tts_fail", f"a{attempt}:{str(exc)[:90]}")
                    if not rate_limited:
                        logger.error("TTS failed: %s", exc)
                        return None
        return None

    async def _send_json(self, payload: dict) -> None:
        with contextlib.suppress(Exception):  # socket closing; the receive loop will exit
            await self.ws.send_text(json.dumps(payload))


async def _once(text: str) -> AsyncIterator[str]:
    yield text


async def prewarm_tts(groq_api_key: str) -> None:
    """Synthesize the greetings at startup so every session opens instantly.

    Only the greetings: every session needs one, so this is a net saving.
    The crisis script is cached on first use instead — on Groq's free tier
    (100 TTS requests/day) pre-synthesizing it on every cold boot of a
    free Render instance would burn a large share of the daily quota.
    """
    if not groq_api_key:
        return
    groq = AsyncGroq(api_key=groq_api_key)
    # Cache keys must be the exact pieces _speak synthesizes: sentence-split,
    # then TTS-split — caching the whole greeting string never gets a hit.
    phrases: list[str] = []
    for text in (GREETING, GREETING_RETURNING):
        async for sentence in companion.sentences(_once(text)):
            phrases.extend(_split_for_tts(sentence))
    for text in phrases:
        if _tts_cache_get(text) is not None:
            continue
        try:
            resp = await groq.audio.speech.create(model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="wav")
            _tts_cache_put(text, await resp.read())
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS prewarm skipped (%s)", str(exc)[:120])
            return
    logger.info("TTS prewarmed %d fixed phrases", len(phrases))
