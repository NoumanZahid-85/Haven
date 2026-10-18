"""Companion engine: prompt assembly, crisis bypass, technique retrieval."""

from __future__ import annotations

import pytest

from app.services import companion, knowledge
from app.services.safety import CRISIS_RESPONSE, Risk


@pytest.mark.asyncio
async def test_crisis_bypasses_the_llm(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("LLM must not be called on a crisis turn")

    monkeypatch.setattr(companion, "llm_stream", boom)
    convo = companion.Conversation(session_id="t-crisis")
    plan = await companion.plan_turn(convo, "I want to end my life")
    assert plan.risk is Risk.CRISIS
    spoken = " ".join([s async for s in companion.reply_stream(plan)])
    assert spoken == CRISIS_RESPONSE


@pytest.mark.asyncio
async def test_elevated_turn_gets_safety_guidance_and_no_techniques():
    convo = companion.Conversation(session_id="t-elevated")
    plan = await companion.plan_turn(convo, "I feel hopeless and I can't sleep at night")
    assert plan.risk is Risk.ELEVATED
    assert plan.techniques == []
    assert "Safety note" in plan.messages[0]["content"]


@pytest.mark.asyncio
async def test_memory_and_techniques_reach_the_prompt():
    convo = companion.Conversation(session_id="t-ctx", facts=["Has a job interview on Friday"])
    plan = await companion.plan_turn(convo, "I can't sleep, I've been awake in bed for hours")
    system = plan.messages[0]["content"]
    assert "Has a job interview on Friday" in system
    assert any(t.id == "sleep_wind_down" for t in plan.techniques)
    assert plan.messages[-1] == {"role": "user", "content": "I can't sleep, I've been awake in bed for hours"}


@pytest.mark.asyncio
async def test_output_guardrail_applies_to_streamed_llm_reply(monkeypatch):
    async def fake_stream(_messages):
        for part in ["That sounds tough. ", "You could take 50 mg ", "of something. ", "How are you now?"]:
            yield part

    monkeypatch.setattr(companion, "llm_stream", fake_stream)
    plan = await companion.plan_turn(companion.Conversation(session_id="t-guard"), "my head hurts so much")
    out = [s async for s in companion.reply_stream(plan)]
    assert out[0] == "That sounds tough."
    assert "mg" not in " ".join(out)
    assert plan.llm_first_token_ms is not None


@pytest.mark.asyncio
async def test_short_utterances_retrieve_nothing():
    assert await knowledge.retrieve("okay thanks") == []
