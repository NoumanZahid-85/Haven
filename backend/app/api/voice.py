"""
Voice diagnostics.

  GET /api/v1/voice/selftest  — checks both engines with this deployment's keys:
                                Deepgram TTS → Deepgram STT round trip, and a
                                Gemini Live connect + one spoken reply
  GET /api/v1/voice/events    — ring buffer of recent pipeline events
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from google.genai import Client, types
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.voice import deepgram, session_registry

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

PHRASE = "Hi, this is Haven. Can you hear me clearly?"


class SessionEvent(BaseModel):
    ts: str
    session_id: str
    event: str
    detail: str = ""


async def _pipeline_check(settings: Settings) -> dict:
    if not settings.deepgram_api_key:
        return {"ok": False, "detail": "DEEPGRAM_API_KEY not configured"}
    t0 = time.perf_counter()
    first = None
    pcm = bytearray()
    async for chunk in deepgram.tts_stream(PHRASE):
        first = first or time.perf_counter()
        pcm += chunk
    tts_ms = round(((first or time.perf_counter()) - t0) * 1000)

    t1 = time.perf_counter()
    resp = await deepgram.client().post(
        "https://api.deepgram.com/v1/listen",
        params={"model": "nova-3", "encoding": "linear16", "sample_rate": str(deepgram.OUTPUT_RATE)},
        headers={"Authorization": f"Token {settings.deepgram_api_key}", "Content-Type": "application/octet-stream"},
        content=bytes(pcm),
    )
    resp.raise_for_status()
    transcript = resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    return {
        "ok": bool(transcript),
        "tts_first_byte_ms": tts_ms,
        "stt_ms": round((time.perf_counter() - t1) * 1000),
        "transcript": transcript,
    }


async def _live_check(settings: Settings) -> dict:
    if not settings.gemini_api_key:
        return {"ok": False, "detail": "GEMINI_API_KEY not configured"}
    client = Client(api_key=settings.gemini_api_key)
    config = types.LiveConnectConfig(response_modalities=[types.Modality.AUDIO])
    t0 = time.perf_counter()
    async with client.aio.live.connect(model=settings.live_model, config=config) as session:
        connect_ms = round((time.perf_counter() - t0) * 1000)
        t1 = time.perf_counter()
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text="Say hello in three words.")]), turn_complete=True
        )
        first = None
        audio = 0
        async for msg in session.receive():
            sc = msg.server_content
            if sc and sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data and part.inline_data.data:
                        first = first or time.perf_counter()
                        audio += len(part.inline_data.data)
            if sc and sc.turn_complete:
                break
    return {
        "ok": audio > 0,
        "model": settings.live_model,
        "connect_ms": connect_ms,
        "first_audio_ms": round((first - t1) * 1000) if first else None,
    }


@router.get("/selftest", summary="Check both voice engines with this deployment's keys")
async def voice_selftest(settings: Settings = Depends(get_settings)) -> dict:
    report: dict = {}
    for name, check in (("pipeline", _pipeline_check), ("live", _live_check)):
        try:
            report[name] = await check(settings)
        except Exception as exc:  # noqa: BLE001
            report[name] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
    report["verdict"] = "operational" if all(r.get("ok") for r in report.values()) else "degraded"
    return report


@router.get("/events", response_model=list[SessionEvent], summary="Recent voice pipeline events")
async def voice_events() -> list[SessionEvent]:
    return [SessionEvent(**e) for e in session_registry.recent_events()]
