"""
End-to-end voice test — simulates the browser completely.

Connects to the voice WebSocket, then streams REAL synthesized speech
(Groq TTS → resampled to 16 kHz PCM16, paced in real time) exactly like the
browser mic, and verifies:

  1. greeting audio arrives
  2. an everyday turn  → transcript + spoken reply + latency metrics
  3. a crisis turn     → risk=crisis + helpline in the spoken reply
  4. barge-in          → talking over the bot triggers an `interrupt`

Run (from repo root):
    python tools/e2e_voice_test.py <GROQ_API_KEY>
    E2E_WS_BASE=wss://haven-backend.onrender.com python tools/e2e_voice_test.py <key>
"""

import asyncio
import io
import json
import os
import sys
import time
import uuid
import wave

import numpy as np
import websockets
from groq import AsyncGroq

WS_BASE = os.environ.get("E2E_WS_BASE", "ws://localhost:8000")
TARGET_RATE = 16000


def ws_url(barge_in: bool) -> str:
    return f"{WS_BASE}/api/v1/voice/ws?session_id=e2e-{uuid.uuid4().hex[:8]}&barge_in={int(barge_in)}"


def wav_to_float(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32) / 32768, rate


def resample(data: np.ndarray, from_rate: int) -> np.ndarray:
    if from_rate == TARGET_RATE:
        return data
    dur = len(data) / from_rate
    x_old = np.linspace(0, dur, num=len(data), endpoint=False)
    x_new = np.linspace(0, dur, num=int(dur * TARGET_RATE), endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


async def synth_pcm(groq: AsyncGroq, text: str, trailing_silence: float = 2.0) -> bytes:
    tts = await groq.audio.speech.create(
        model="canopylabs/orpheus-v1-english", voice="troy", input=text, response_format="wav",
    )
    speech, rate = wav_to_float(await tts.read())
    speech = resample(speech, rate)
    rms = float(np.sqrt(np.mean(speech**2))) or 1.0
    speech *= min(0.2 / rms, 3.0)  # realistic mic level
    utter = np.concatenate([np.zeros(int(TARGET_RATE * 0.4)), speech, np.zeros(int(TARGET_RATE * trailing_silence))])
    return (np.clip(utter, -1, 1) * 32767).astype(np.int16).tobytes()


async def stream_pcm(ws, pcm: bytes) -> None:
    step = TARGET_RATE // 10 * 2  # 100 ms packets, real-time pacing
    for i in range(0, len(pcm), step):
        await ws.send(pcm[i : i + step])
        await asyncio.sleep(0.1)


async def collect(ws, quiet_s: float, max_wait: float = 90.0, until_reply_end: bool = True) -> dict:
    """Read frames until the reply is over: reply_end seen (then a short
    grace period for the metrics frame), or `quiet_s` of silence."""
    out: dict = {"audio": 0, "captions": [], "transcripts": [], "events": [], "metrics": None, "risk": None}
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            wait = 1.5 if until_reply_end and "reply_end" in out["events"] else quiet_s
            msg = await asyncio.wait_for(ws.recv(), timeout=wait)
        except TimeoutError:
            break
        if isinstance(msg, bytes):
            out["audio"] += len(msg)
            continue
        d = json.loads(msg)
        out["events"].append(d["type"])
        if d["type"] == "agent_caption":
            out["captions"].append(d["text"])
            print(f"   HAVEN: {d['text']}")
        elif d["type"] == "transcript":
            out["transcripts"].append(d["text"])
            print(f"   USER (stt): {d['text']!r}")
        elif d["type"] == "metrics":
            out["metrics"] = d
        elif d["type"] == "risk":
            out["risk"] = d["level"]
        elif d["type"] == "speaking_start":
            out["turn"] = d["turn"]
        elif d["type"] == "error":
            print(f"   ERROR: {d.get('message')}")
    return out


async def say(ws, groq, text: str) -> dict:
    print(f"\n>>> USER SAYS: {text!r}")
    await stream_pcm(ws, await synth_pcm(groq, text))
    result = await collect(ws, quiet_s=20.0)
    await ws.send(json.dumps({"type": "playback_done", "turn": result.get("turn")}))
    if m := result["metrics"]:
        print(f"   latency: stt={m['stt_ms']:.0f}ms llm_ttft={m['llm_first_token_ms'] or 0:.0f}ms "
              f"first_audio={m['first_audio_ms'] or 0:.0f}ms (from end of speech)")
    return result


async def main() -> int:
    groq = AsyncGroq(api_key=sys.argv[1])
    checks: dict[str, bool] = {}

    async with websockets.connect(ws_url(barge_in=False), max_size=20 * 1024 * 1024) as ws:
        assert json.loads(await ws.recv())["type"] == "ready"
        g = await collect(ws, quiet_s=20.0)
        await ws.send(json.dumps({"type": "playback_done", "turn": g.get("turn")}))
        checks["greeting audio"] = g["audio"] > 50_000

        r1 = await say(ws, groq, "I can't sleep. My mind keeps racing about my exam tomorrow.")
        checks["everyday turn"] = bool(r1["transcripts"]) and r1["audio"] > 50_000 and r1["metrics"] is not None

        r2 = await say(ws, groq, "Honestly, I don't want to be alive anymore.")
        checks["crisis routing"] = r2["risk"] == "crisis" and "988" in " ".join(r2["captions"])

    async with websockets.connect(ws_url(barge_in=True), max_size=20 * 1024 * 1024) as ws:
        assert json.loads(await ws.recv())["type"] == "ready"
        print("\n=== BARGE-IN: talking over the greeting ===")
        first = await asyncio.wait_for(ws.recv(), timeout=30)  # speaking_start
        while isinstance(first, str) and json.loads(first)["type"] != "agent_caption":
            first = await asyncio.wait_for(ws.recv(), timeout=30)
        await asyncio.wait_for(ws.recv(), timeout=30)  # first WAV is out → bot has the floor
        talk = asyncio.create_task(stream_pcm(ws, await synth_pcm(groq, "Sorry, can I stop you there for a second?")))
        result = await collect(ws, quiet_s=8.0, until_reply_end=False)
        await talk
        checks["barge-in interrupt"] = "interrupt" in result["events"]

    print("\n" + "\n".join(f"  {name:<20}: {'PASS' if ok else 'FAIL'}" for name, ok in checks.items()))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
