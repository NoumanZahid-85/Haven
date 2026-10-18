"""Voice session floor control — no network: fake socket, fake TTS, fake VAD."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services import companion
from app.voice import ws_session
from app.voice.ws_session import GREETING, VoiceWSSession


class FakeWS:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict | bytes] = []

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def events(self) -> list[str]:
        return [m["type"] for m in self.sent if isinstance(m, dict)]

    def control(self, payload: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(payload)})


class FakeVAD:
    """Scripted speech probabilities instead of the Silero model."""

    def __init__(self) -> None:
        self.next = 0.0

    def feed(self, _pcm: bytes) -> float:
        return self.next


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setattr(ws_session, "SileroGate", FakeVAD)
    ws = FakeWS()
    s = VoiceWSSession(ws, "test-session")
    return s, ws


@pytest.mark.asyncio
async def test_late_turn_completion_releases_the_mic(session, monkeypatch):
    """playback_done arriving before a slow last sentence finishes used to
    latch `busy` forever — the mic was dead for the rest of the session."""
    s, ws = session
    release = asyncio.Event()

    async def synth(text: str, _sem) -> bytes:
        if text.startswith("How are you"):
            await release.wait()  # last greeting sentence stuck in TTS retry
        return b"RIFF-fake-wav"

    monkeypatch.setattr(s, "_synthesize", synth)
    runner = asyncio.create_task(s.run())
    await _until(lambda: any(isinstance(m, bytes) for m in ws.sent))

    ws.control({"type": "playback_done", "turn": 1})
    await _until(lambda: not s.speaking)
    assert s.busy  # turn still in flight

    release.set()
    await _until(lambda: "reply_end" in ws.events())
    await _until(lambda: not s.busy)
    runner.cancel()


@pytest.mark.asyncio
async def test_stale_playback_done_is_ignored(session, monkeypatch):
    s, ws = session

    async def synth(_text: str, _sem) -> bytes:
        return b"wav"

    monkeypatch.setattr(s, "_synthesize", synth)
    runner = asyncio.create_task(s.run())
    await _until(lambda: "reply_end" in ws.events())
    s.turn_no = 5  # a later turn now has the floor
    s.speaking = True
    ws.control({"type": "playback_done", "turn": 1})
    await asyncio.sleep(0.05)
    assert s.speaking
    runner.cancel()


@pytest.mark.asyncio
async def test_barge_in_cancels_reply_and_starts_capture(monkeypatch):
    monkeypatch.setattr(ws_session, "SileroGate", FakeVAD)
    ws = FakeWS()
    s = VoiceWSSession(ws, "barge", barge_in=True)
    gate = asyncio.Event()

    async def synth(text: str, _sem) -> bytes:
        if not text.startswith("Hi"):
            await gate.wait()  # later sentences never finish on their own
        return b"wav"

    monkeypatch.setattr(s, "_synthesize", synth)
    runner = asyncio.create_task(s.run())
    await _until(lambda: s._audio_out)

    s.vad.next = 0.95  # user starts talking over the bot
    chunk = b"\x00\x00" * 1600  # 100 ms of 16 kHz PCM16
    for _ in range(5):
        s._feed_vad(chunk)
    await _until(lambda: "interrupt" in ws.events())
    assert s._state == "capturing"
    assert not s.busy and not s.speaking
    runner.cancel()


@pytest.mark.asyncio
async def test_half_duplex_discards_audio_while_bot_speaks(session):
    s, _ws = session
    s.busy = True
    s.vad.next = 0.99
    for _ in range(10):
        s._feed_vad(b"\x00\x00" * 1600)
    assert s._state == "idle"


@pytest.mark.asyncio
async def test_prewarm_caches_exactly_what_speak_requests(monkeypatch):
    synthesized: list[str] = []

    class FakeSpeech:
        async def create(self, *, input: str, **_kw):
            synthesized.append(input)

            class Resp:
                async def read(self) -> bytes:
                    return b"wav"

            return Resp()

    class FakeGroq:
        def __init__(self, **_kw) -> None:
            self.audio = type("A", (), {"speech": FakeSpeech()})()

    monkeypatch.setattr(ws_session, "AsyncGroq", FakeGroq)
    ws_session._tts_cache.clear()
    await ws_session.prewarm_tts("key")

    pieces = [p async for s in companion.sentences(ws_session._once(GREETING)) for p in ws_session._split_for_tts(s)]
    assert pieces and all(ws_session._tts_cache_get(p) is not None for p in pieces)
    assert GREETING not in synthesized  # the whole string is never a cache key
