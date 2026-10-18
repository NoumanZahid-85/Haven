"""
Deepgram clients for Pipeline mode (and the safety voice in Live mode).

TTS  Aura-2 over plain HTTP streaming on a persistent connection. Measured:
     ~290 ms to first audio byte with a warm connection versus ~550 ms over
     the speak WebSocket and ~1.9 s with a cold TLS handshake — so one
     shared, pre-warmed client is kept for the whole process.

STT  Flux (/v2/listen): streaming recognition with turn detection built in.
     It emits StartOfTurn / EagerEndOfTurn / TurnResumed / EndOfTurn, which
     replaces both a VAD model and a silence timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator

import httpx
import websockets

from app.core.config import get_settings

logger = logging.getLogger(__name__)

OUTPUT_RATE = 24000  # PCM16 mono — the same rate Gemini Live outputs, so the client plays one format
TTS_URL = "https://api.deepgram.com/v1/speak"
FLUX_URL = "wss://api.deepgram.com/v2/listen"
# Tuned on real phrases. Flux defaults took 2.4 s to close a plain statement
# ("I get this dread every morning before work."); 0.6/0.4 closes it in ~2.0 s
# with the eager signal ~0.5 s earlier, and the 1.5 s timeout bounds the worst
# case. 0.5/0.3 was faster still but closed turns on a breath ("Honestly, …"),
# which for a companion is worse than half a second of waiting.
FLUX_PARAMS = (
    "model=flux-general-en&encoding=linear16&sample_rate=16000"
    "&eot_threshold=0.6&eager_eot_threshold=0.4&eot_timeout_ms=1500"
)

_client: httpx.AsyncClient | None = None

# Fixed phrases (greeting, crisis script) are synthesized once per process.
_CACHE_MAX = 64
_cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Token {get_settings().deepgram_api_key}"}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    return _client


async def tts_stream(text: str, *, cache: bool = False) -> AsyncIterator[bytes]:
    """Yield PCM16 @24 kHz chunks for `text` as Deepgram produces them.

    Chunks are always an even number of bytes, so the client can view them
    as Int16 samples without a carry-over byte.
    """
    voice = get_settings().tts_voice
    if cache and (hit := _cache.get((voice, text))) is not None:
        _cache.move_to_end((voice, text))
        yield hit
        return

    params = {"model": voice, "encoding": "linear16", "sample_rate": str(OUTPUT_RATE), "container": "none"}
    collected = bytearray()
    carry = b""
    async with client().stream("POST", TTS_URL, params=params, headers=_headers(), json={"text": text}) as resp:
        if resp.status_code != 200:
            detail = (await resp.aread())[:200]
            raise RuntimeError(f"Deepgram TTS {resp.status_code}: {detail!r}")
        async for chunk in resp.aiter_bytes():
            data = carry + chunk
            cut = len(data) - (len(data) % 2)
            carry = data[cut:]
            if cut:
                if cache:
                    collected += data[:cut]
                yield data[:cut]
    if cache and collected:
        _cache[(voice, text)] = bytes(collected)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


_last_touch = 0.0


def touch() -> None:
    """Re-open the pooled TLS connection before it's needed.

    Deepgram drops idle keep-alive connections, and a fresh handshake costs
    ~1 s from far regions. Called when the user starts speaking, so the
    connection is hot by the time the first sentence is ready. An
    unauthenticated GET is rejected instantly and never billed.
    """
    global _last_touch
    now = time.monotonic()
    if now - _last_touch < 5:
        return
    _last_touch = now

    async def ping() -> None:
        with contextlib.suppress(Exception):
            await client().get(TTS_URL)

    task = asyncio.create_task(ping())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


_pending: set[asyncio.Task] = set()


async def warm() -> None:
    """Open the TLS connection at boot so the first user doesn't pay ~1.5 s for it."""
    if not get_settings().deepgram_api_key:
        return
    try:
        async for _ in tts_stream("Hi."):
            pass
        logger.info("Deepgram TTS connection warm")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deepgram warm-up failed: %s", exc)


class FluxSTT:
    """One Flux streaming-recognition socket per voice session."""

    def __init__(self) -> None:
        self._ws: websockets.ClientConnection | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            f"{FLUX_URL}?{FLUX_PARAMS}", additional_headers=_headers(), max_size=None, open_timeout=10
        )

    async def send(self, pcm16: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm16)

    async def events(self) -> AsyncIterator[dict]:
        assert self._ws is not None
        async for raw in self._ws:
            if isinstance(raw, str):
                yield json.loads(raw)

    async def close(self) -> None:
        if self._ws is None:
            return
        with contextlib.suppress(Exception):  # already closed
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        await self._ws.close()
