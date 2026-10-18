"""Token stream → speakable sentences."""

from __future__ import annotations

import pytest

from app.services.companion import sentences


async def _stream(*parts: str):
    for p in parts:
        yield p


async def _collect(*parts: str) -> list[str]:
    return [s async for s in sentences(_stream(*parts))]


@pytest.mark.asyncio
async def test_sentences_emitted_as_soon_as_complete():
    out = await _collect("That sounds ", "hard. What ", "happened", " today? I'm ", "here.")
    assert out == ["That sounds hard.", "What happened today?", "I'm here."]


@pytest.mark.asyncio
async def test_markdown_is_stripped_for_tts():
    out = await _collect("**Try this:**\n- breathe in for four.\n")
    assert out == ["Try this:", "breathe in for four."]


@pytest.mark.asyncio
async def test_run_on_sentence_is_cut_without_waiting_for_a_period():
    long = "and then " * 40
    out = await _collect(long)
    assert len(out) > 1
    assert all(len(s) <= 171 for s in out)
