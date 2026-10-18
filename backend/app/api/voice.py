"""
Voice diagnostics.

  GET /api/v1/voice/selftest  — server-side TTS → STT round trip with this
                                deployment's keys (no browser needed)
  GET /api/v1/voice/events    — ring buffer of recent pipeline events
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.voice import session_registry
from app.voice.ws_session import STT_MODEL, TTS_MODEL, TTS_VOICE

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


class SessionEvent(BaseModel):
    ts: str
    session_id: str
    event: str
    detail: str = ""


@router.get("/selftest", summary="Server-side Groq TTS → STT round trip using this deployment's keys")
async def voice_selftest(settings: Settings = Depends(get_settings)) -> dict:
    """Synthesize a phrase with Orpheus, transcribe it back with Whisper, and
    report per-stage status and latency — isolates provider problems from
    browser/socket problems in one call."""
    report: dict = {"tts_model": TTS_MODEL, "stt_model": STT_MODEL, "tts": None, "stt": None}
    if not settings.groq_api_key:
        report["error"] = "GROQ_API_KEY not configured on this deployment"
        return report

    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            t0 = time.perf_counter()
            tts_resp = await client.post(
                "https://api.groq.com/openai/v1/audio/speech",
                headers=headers,
                json={
                    "model": TTS_MODEL,
                    "voice": TTS_VOICE,
                    "input": "Hi, this is Haven. Can you hear me clearly?",
                    "response_format": "wav",
                },
            )
            tts_ok = tts_resp.status_code == 200 and len(tts_resp.content) > 1000
            report["tts"] = {
                "ok": tts_ok,
                "status": tts_resp.status_code,
                "bytes": len(tts_resp.content),
                "ms": round((time.perf_counter() - t0) * 1000),
                "detail": "" if tts_ok else tts_resp.text[:300],
            }
            if not tts_ok:
                return report

            t1 = time.perf_counter()
            stt_resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers,
                files={"file": ("selftest.wav", tts_resp.content, "audio/wav")},
                data={"model": STT_MODEL},
            )
            stt_ok = stt_resp.status_code == 200
            report["stt"] = {
                "ok": stt_ok,
                "status": stt_resp.status_code,
                "ms": round((time.perf_counter() - t1) * 1000),
                "transcript": stt_resp.json().get("text", "") if stt_ok else None,
                "detail": "" if stt_ok else stt_resp.text[:300],
            }
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"

    report["verdict"] = (
        "voice services operational"
        if report["stt"] and report["stt"]["ok"]
        else "voice service failure — see tts/stt details"
    )
    return report


@router.get("/events", response_model=list[SessionEvent], summary="Recent voice pipeline events")
async def voice_events() -> list[SessionEvent]:
    return [SessionEvent(**e) for e in session_registry.recent_events()]
