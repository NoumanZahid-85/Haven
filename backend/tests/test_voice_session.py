"""Voice engines — no network: fake browser socket, fake Deepgram TTS, fake LLM, fake Gemini messages."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app.services import companion
from app.services.safety import CRISIS_RESPONSE
from app.voice import deepgram
from app.voice.live_session import LiveSession
from app.voice.pipeline_session import PipelineSession


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict | bytes] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def events(self) -> list[str]:
        return [m["type"] for m in self.sent if isinstance(m, dict)]

    def audio(self) -> list[bytes]:
        return [m for m in self.sent if isinstance(m, bytes)]

    def captions(self) -> str:
        return "".join(m["text"] for m in self.sent if isinstance(m, dict) and m["type"] == "agent_caption")


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def fake_tts(monkeypatch):
    async def tts(text: str, **_kwargs):
        yield b"\x01\x00" * 8  # one tiny PCM chunk per sentence

    monkeypatch.setattr(deepgram, "tts_stream", tts)


@pytest.fixture()
def fake_llm(monkeypatch):
    calls: list[list[dict]] = []

    async def stream(messages):
        calls.append(messages)
        for part in ["That sounds hard. ", "What's on your mind?"]:
            await asyncio.sleep(0.01)
            yield part

    monkeypatch.setattr(companion, "llm_stream", stream)
    return calls


def pipeline(barge_in: bool = False) -> tuple[PipelineSession, FakeWS]:
    ws = FakeWS()
    return PipelineSession(ws, "pipe-test", barge_in=barge_in), ws


# ── Shared bridge ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speak_holds_audio_until_committed():
    s, ws = pipeline()
    commit = asyncio.Event()

    async def sentences():
        yield "First."
        yield "Second."

    task = asyncio.create_task(s.speak(sentences(), commit=commit))
    await asyncio.sleep(0.05)
    assert ws.sent == []  # prepared, not heard
    commit.set()
    spoken, first = await task
    assert spoken == ["First.", "Second."]
    assert ws.events() == ["speaking_start", "agent_caption", "agent_caption", "reply_end"]
    assert len(ws.audio()) == 2 and first is not None


# ── Pipeline mode (Flux turn events) ─────────────────────────────────


@pytest.mark.asyncio
async def test_eager_reply_is_released_on_matching_end_of_turn(fake_llm):
    s, ws = pipeline()
    await s.on_turn_event("EagerEndOfTurn", "I can't sleep tonight")
    await asyncio.sleep(0.1)
    assert ws.audio() == []  # speculative: generated but held
    await s.on_turn_event("EndOfTurn", "I can't sleep tonight.")
    await _until(lambda: "reply_end" in ws.events())
    assert len(fake_llm) == 1  # the speculative generation was reused, not redone
    assert ws.captions() == "That sounds hard. What's on your mind? "


@pytest.mark.asyncio
async def test_turn_resumed_discards_speculative_reply(fake_llm):
    s, ws = pipeline()
    await s.on_turn_event("EagerEndOfTurn", "I can't sleep")
    await s.on_turn_event("TurnResumed", "")
    await s.on_turn_event("EndOfTurn", "I can't sleep because of my exam tomorrow")
    await _until(lambda: "reply_end" in ws.events())
    # The heard reply was generated from the full turn, not the eager fragment.
    assert fake_llm[-1][-1]["content"] == "I can't sleep because of my exam tomorrow"
    assert ws.events().count("transcript") == 1
    assert ws.events().count("speaking_start") == 1


@pytest.mark.asyncio
async def test_barge_in_interrupts_audible_reply(fake_llm, monkeypatch):
    release = asyncio.Event()

    async def slow_stream(messages):
        yield "First sentence. "
        await release.wait()  # the rest never arrives on its own
        yield "Never heard."

    monkeypatch.setattr(companion, "llm_stream", slow_stream)
    s, ws = pipeline(barge_in=True)
    await s.on_turn_event("EndOfTurn", "Tell me something")
    await _until(lambda: len(ws.audio()) == 1)
    await s.on_turn_event("StartOfTurn", "")
    assert "interrupt" not in ws.events()  # a sound alone is not an interruption
    await s.on_turn_event("Update", "Wait, hold on")
    await _until(lambda: "interrupt" in ws.events())
    assert s.reply is None
    assert "Never heard." not in ws.captions()


@pytest.mark.asyncio
async def test_backchannel_and_echo_do_not_interrupt(monkeypatch):
    release = asyncio.Event()

    async def slow_stream(messages):
        yield "Try breathing in for four counts. "
        await release.wait()
        yield "Then out for six."

    monkeypatch.setattr(companion, "llm_stream", slow_stream)
    s, ws = pipeline(barge_in=True)
    await s.on_turn_event("EndOfTurn", "Help me calm down")
    await _until(lambda: len(ws.audio()) == 1)
    replies_before = ws.events().count("speaking_start")

    await s.on_turn_event("Update", "Mm-hmm")
    await s.on_turn_event("EndOfTurn", "Mm-hmm.")
    await s.on_turn_event("Update", "breathing in for four counts")  # Haven's own voice via the speakers
    assert "interrupt" not in ws.events()
    assert s.reply is not None  # still speaking
    assert ws.events().count("speaking_start") == replies_before  # never answered "mm-hmm"
    def reasons() -> set[str]:
        return {m["reason"] for m in ws.sent if isinstance(m, dict) and m["type"] == "heard"}

    await _until(lambda: reasons() >= {"backchannel", "echo"})
    release.set()


@pytest.mark.asyncio
async def test_words_are_carried_when_user_continues_before_audio(monkeypatch):
    gate = asyncio.Event()
    seen: list[str] = []

    async def stream(messages):
        seen.append(messages[-1]["content"])
        await gate.wait()
        yield "Okay."

    monkeypatch.setattr(companion, "llm_stream", stream)
    s, ws = pipeline()
    await s.on_turn_event("EndOfTurn", "I had a fight with my sister")
    await _until(lambda: seen)
    await s.on_turn_event("StartOfTurn", "")  # nothing audible yet: user is mid-thought
    await s.on_turn_event("EndOfTurn", "and now she won't talk to me")
    gate.set()
    await _until(lambda: "reply_end" in ws.events())
    assert seen[-1] == "I had a fight with my sister and now she won't talk to me"


@pytest.mark.asyncio
async def test_pipeline_crisis_bypasses_llm(fake_llm):
    s, ws = pipeline()
    await s.on_turn_event("EndOfTurn", "I want to end my life")
    await _until(lambda: "reply_end" in ws.events())
    assert fake_llm == []
    assert {"type": "risk", "level": "crisis"} in ws.sent
    assert "988" in ws.captions()


# ── Live mode (Gemini server messages) ───────────────────────────────


def live() -> tuple[LiveSession, FakeWS]:
    ws = FakeWS()
    return LiveSession(ws, "live-test"), ws


def msg(*, heard=None, said=None, audio=None, interrupted=False, done=False):
    parts = [NS(inline_data=NS(data=audio))] if audio else None
    return NS(
        go_away=None,
        server_content=NS(
            input_transcription=NS(text=heard) if heard else None,
            output_transcription=NS(text=said) if said else None,
            model_turn=NS(parts=parts) if parts else None,
            interrupted=interrupted,
            turn_complete=done,
        ),
    )


@pytest.mark.asyncio
async def test_live_turn_relays_audio_and_captions():
    s, ws = live()
    await s.handle(msg(heard="I feel stressed"))
    await s.handle(msg(audio=b"\x00\x01" * 4, said="That sounds heavy."))
    await s.handle(msg(done=True))
    assert ws.events() == ["user_partial", "transcript", "speaking_start", "agent_caption", "reply_end", "metrics"]
    assert len(ws.audio()) == 1


@pytest.mark.asyncio
async def test_live_crisis_mutes_model_and_speaks_script():
    s, ws = live()
    await s.handle(msg(audio=b"\x00\x01" * 4))  # model already talking
    await s.handle(msg(heard="I don't want to be alive anymore"))
    assert s.suppressed
    await s.handle(msg(audio=b"\xff\xff" * 4, said="model words"))  # must be dropped
    await _until(lambda: ws.events().count("reply_end") == 1)
    assert "interrupt" in ws.events()
    assert {"type": "risk", "level": "crisis"} in ws.sent
    assert "model words" not in ws.captions()
    assert ws.captions().strip() == CRISIS_RESPONSE
    await s.handle(msg(done=True))
    assert not s.suppressed  # next turn is the model's again


@pytest.mark.asyncio
async def test_live_output_guardrail_replaces_unsafe_reply():
    s, ws = live()
    await s.handle(msg(heard="what should I take"))
    await s.handle(msg(audio=b"\x00\x01" * 4, said="You could take 50 mg tonight."))
    await _until(lambda: "reply_end" in ws.events())
    assert "interrupt" in ws.events()
    assert "doctor or therapist" in ws.captions()


@pytest.mark.asyncio
async def test_live_barge_in_forwards_interrupt():
    s, ws = live()
    await s.handle(msg(heard="hello"))
    await s.handle(msg(audio=b"\x00\x01" * 4))
    await s.handle(msg(interrupted=True))
    assert ws.events()[-1] == "interrupt"
    assert not s.turn_open


# ── Natural mode barge-in: silence to Gemini, judged side channel, replay ──


class FakeGeminiSession:
    def __init__(self) -> None:
        self.audio: list[bytes] = []

    async def send_realtime_input(self, *, audio) -> None:
        self.audio.append(audio.data)


class FakeFlux:
    def __init__(self) -> None:
        self.audio: list[bytes] = []

    async def send(self, pcm: bytes) -> None:
        self.audio.append(pcm)


@pytest.mark.asyncio
async def test_live_barge_in_hands_buffered_words_to_gemini():
    s, ws = live()
    s.session = FakeGeminiSession()
    s._flux = FakeFlux()
    await s.handle(msg(heard="hello"))
    await s.handle(msg(audio=b"\x00\x01" * 4, said="So one thing you could try"))
    assert s.speaking

    voice = b"\x10\x27" * 800  # 50 ms of loud audio
    await s.on_audio(voice)
    assert s.session.audio[-1] == bytes(len(voice))  # Gemini hears silence while Haven speaks
    assert s._flux.audio[-1] == voice  # the judge's side channel hears the real mic

    assert s.judge_heard("mm-hmm").interrupt is False
    assert "interrupt" not in ws.events()

    await s._barge()
    assert ws.events()[-1] == "interrupt"
    assert s.session.audio[-1] == voice  # buffered words replayed to Gemini
    await s.handle(msg(audio=b"\xff\xff" * 4, said="rest of the old reply"))
    assert "rest of the old reply" not in ws.captions()  # cut-off generation is dropped
    await s.handle(msg(interrupted=True))
    assert not s.discarding
