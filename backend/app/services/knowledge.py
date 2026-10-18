"""
Coping-technique knowledge base + retrieval (the RAG layer).

A small, curated set of evidence-based techniques (CBT, ACT, breathing,
sleep hygiene, behavioural activation, self-compassion). Each entry is
written to be *spoken*: short steps, no lists the TTS would read literally.

Why in-memory instead of a vector DB: the corpus is tens of entries, not
millions. Embedding it once at startup and doing a numpy cosine per turn
costs microseconds and removes a network hop from the voice turn. The
retrieval interface stays the same if the corpus outgrows memory.

Retrieval is latency-bounded: the query embedding has a hard timeout and
falls back to tag overlap, so a slow embeddings API never stalls a turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import numpy as np

from app.services.embeddings import get_embedder

logger = logging.getLogger(__name__)

EMBED_TIMEOUT_S = 0.6  # on the hot path between STT and LLM — never let it dominate
MIN_SIMILARITY = 0.60  # measured: relevant 0.61-0.69, off-topic small talk <=0.57


@dataclass(frozen=True)
class Technique:
    id: str
    title: str
    when: str  # situations it helps with — this is what gets embedded
    how: str  # spoken-style instructions given to the LLM
    tags: tuple[str, ...]


TECHNIQUES: list[Technique] = [
    Technique(
        "box_breathing", "Box breathing",
        "anxiety, panic, racing heart, stress before a meeting, exam or presentation",
        "Breathe in for four counts, hold for four, out for four, hold for four. Repeat about four rounds.",
        ("anxious", "anxiety", "panic", "nervous", "stress", "heart", "breathe"),
    ),
    Technique(
        "physiological_sigh", "Physiological sigh",
        "acute stress, feeling overwhelmed right now, need to calm down quickly",
        "Two quick inhales through the nose, the second topping up the first, then one long slow exhale through the mouth. Two or three times.",
        ("overwhelmed", "stressed", "calm", "tense", "now"),
    ),
    Technique(
        "grounding_54321", "5-4-3-2-1 grounding",
        "panic attack, dissociation, feeling unreal, spiralling thoughts, losing control",
        "Name five things you can see, four you can feel, three you can hear, two you can smell, one you can taste. Go slowly.",
        ("panic", "spiral", "unreal", "control", "losing", "attack"),
    ),
    Technique(
        "cognitive_reframe", "Catching and reframing a thought",
        "negative self-talk, catastrophising, assuming the worst, harsh thoughts about yourself",
        "Notice the exact thought. Ask what evidence supports it and what doesn't. Then try a more balanced version you'd say to a friend.",
        ("worst", "always", "never", "failure", "stupid", "thought", "think"),
    ),
    Technique(
        "defusion", "Naming the thought",
        "intrusive thoughts, rumination, a thought that keeps looping, overthinking",
        "Put the words 'I'm having the thought that' in front of it. You're noticing a thought, not obeying a fact.",
        ("overthinking", "ruminating", "loop", "can't stop thinking", "intrusive"),
    ),
    Technique(
        "worry_time", "Scheduled worry time",
        "constant worrying all day, worries interrupting work, anxious about many things",
        "Pick fifteen minutes later today as worry time. When a worry shows up before then, jot it down and postpone it.",
        ("worry", "worried", "worrying", "constantly"),
    ),
    Technique(
        "sleep_wind_down", "Wind-down for sleep",
        "can't sleep, insomnia, awake at night, lying in bed for hours, 2am thoughts",
        "If you've been awake over twenty minutes, get up, keep lights low, do something calm and boring, and go back only when sleepy. No clock watching.",
        ("sleep", "insomnia", "awake", "night", "bed", "tired"),
    ),
    Technique(
        "cognitive_shuffle", "Cognitive shuffle",
        "racing thoughts at bedtime, mind won't switch off when trying to fall asleep",
        "Pick a word like 'blanket'. For each letter, slowly picture random things starting with it. B, bicycle, banana. It gives the mind something neutral to hold.",
        ("racing", "switch off", "fall asleep", "mind"),
    ),
    Technique(
        "behavioural_activation", "One small action",
        "low mood, no motivation, feeling flat, stuck in bed, can't get started",
        "Choose one action that takes under five minutes, like opening a window or a short walk. Do it before waiting to feel motivated; mood often follows action.",
        ("motivation", "flat", "low", "depressed", "stuck", "lazy", "nothing"),
    ),
    Technique(
        "self_compassion", "Self-compassion break",
        "self-criticism, shame, feeling like a failure, mistakes, being hard on yourself",
        "Acknowledge this is a hard moment. Remind yourself that struggling is part of being human. Then ask what you'd say to a friend in the same spot.",
        ("ashamed", "shame", "failure", "mistake", "hate myself", "hard on myself"),
    ),
    Technique(
        "pmr", "Progressive muscle relaxation",
        "physical tension, tight shoulders, clenched jaw, body stress, headaches from stress",
        "Tense one muscle group for five seconds, then release for ten and notice the difference. Start with hands, then shoulders, then face.",
        ("tension", "tense", "shoulders", "jaw", "body"),
    ),
    Technique(
        "expressive_writing", "Expressive writing",
        "breakup, grief, loss, a painful event you keep replaying, heartbreak",
        "Write for ten minutes about what happened and how it felt, without editing or rereading. It's for you, not for anyone else.",
        ("breakup", "broke up", "ex", "grief", "lost", "loss", "died", "heartbreak"),
    ),
    Technique(
        "reach_out", "Reaching out to one person",
        "loneliness, isolation, feeling like nobody cares, no one to talk to",
        "Think of one person who'd be glad to hear from you. A short message is enough, like 'thinking of you, how are you?'",
        ("lonely", "alone", "isolated", "nobody", "no one", "friends"),
    ),
    Technique(
        "values_step", "Values and a tiny next step",
        "feeling lost, no direction, personal growth, wanting to change something, stuck in life",
        "Ask what matters most to you in this area of life. Then pick the smallest possible step toward it you could take this week.",
        ("lost", "direction", "purpose", "goal", "change", "grow", "future"),
    ),
    Technique(
        "anxiety_reappraisal", "Reframing nerves as readiness",
        "anxiety before work, interviews, performances, a big day, dread in the morning",
        "The body signals for fear and excitement are almost the same. Try saying 'I'm getting ready' instead of 'I'm anxious', and name one thing you've prepared.",
        ("work", "interview", "tomorrow", "morning", "presentation", "dread", "boss"),
    ),
]

_BY_ID = {t.id: t for t in TECHNIQUES}
_matrix: np.ndarray | None = None  # (n_techniques, dims), L2-normalised


async def warm_knowledge() -> None:
    """Embed the corpus once. Safe to fail — retrieval falls back to tags."""
    global _matrix
    try:
        vectors = await get_embedder().embed([f"{t.title}: {t.when}" for t in TECHNIQUES])
        m = np.asarray(vectors, dtype=np.float32)
        _matrix = m / np.linalg.norm(m, axis=1, keepdims=True)
        logger.info("Knowledge base embedded: %d techniques", len(TECHNIQUES))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Knowledge embedding failed, using tag retrieval: %s", exc)


def is_embedded() -> bool:
    return _matrix is not None


def _tag_retrieve(query: str, k: int) -> list[Technique]:
    lowered = query.lower()
    scored = []
    for t in TECHNIQUES:
        hits = sum(1 for tag in t.tags if re.search(rf"\b{re.escape(tag)}", lowered))
        if hits:
            scored.append((hits, t))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _, t in scored[:k]]


async def retrieve(query: str, k: int = 2) -> list[Technique]:
    """Top-k techniques for this utterance, or [] when nothing fits."""
    if len(query.split()) < 3:
        return []  # "yeah", "okay thanks" — nothing to match on
    if _matrix is None:
        return _tag_retrieve(query, k)
    try:
        vec = await asyncio.wait_for(get_embedder().embed_one(query), timeout=EMBED_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.info("Query embedding unavailable (%s) — tag retrieval", type(exc).__name__)
        return _tag_retrieve(query, k)
    q = np.asarray(vec, dtype=np.float32)
    sims = _matrix @ (q / np.linalg.norm(q))
    order = np.argsort(-sims)[:k]
    return [TECHNIQUES[i] for i in order if sims[i] >= MIN_SIMILARITY]


def get_technique(technique_id: str) -> Technique | None:
    return _BY_ID.get(technique_id)
