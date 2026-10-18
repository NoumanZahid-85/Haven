"""
Recent voice-pipeline events — crash and latency visibility without log access.

Render's free plan exposes no logs, so pipeline failures used to be
invisible. Every session transition (connect, VAD start, STT text, barge-in,
TTS failure, …) is appended to a small ring buffer served by
GET /api/v1/voice/events, so a silent bot can be diagnosed from outside.
"""

from __future__ import annotations

import time
from collections import deque

_MAX_EVENTS = 60

_events: deque[dict] = deque(maxlen=_MAX_EVENTS)


def record_event(session_id: str, event: str, detail: str = "") -> None:
    _events.append({
        "ts": time.strftime("%H:%M:%S"),
        "session_id": session_id[:8],
        "event": event,
        "detail": detail[:500],
    })


def recent_events() -> list[dict]:
    return list(_events)
