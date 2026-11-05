"""Safety layer: input risk tiers and the per-sentence output guardrail."""

from __future__ import annotations

import pytest

from app.services.safety import SAFE_REPLACEMENT, Risk, screen_input, screen_output


@pytest.mark.parametrize("text", [
    "I want to kill myself",
    "I've been thinking about ending my life",
    "everyone would be better off if I was dead",
    "I don't want to be alive anymore",
    "I cut myself again last night",
    "I'm thinking about taking all my pills",
    "there's no reason to live",
])
def test_crisis_language_is_caught(text):
    assert screen_input(text) is Risk.CRISIS


@pytest.mark.parametrize("text", [
    "I'd never kill myself, I'm just exhausted",
    "I feel hopeless",
    "I'm a burden to everyone",
    "I can't take this anymore",
    "I think I'm having a panic attack",
])
def test_distress_is_elevated_not_crisis(text):
    assert screen_input(text) is Risk.ELEVATED


@pytest.mark.parametrize("text", [
    "I can't sleep, I keep thinking about my exam",
    "my boss was rude to me today",
    "this traffic is killing me",
    "hey, how's it going?",
])
def test_everyday_language_is_not_flagged(text):
    assert screen_input(text) is Risk.NONE


@pytest.mark.parametrize("sentence", [
    "You could try taking 50 mg before bed.",
    "It sounds like you have clinical depression.",
    "I'm your therapist, so trust me.",
    "Maybe stop taking your medication for a while.",
])
def test_output_guardrail_replaces_unsafe_sentences(sentence):
    assert screen_output(sentence) == (SAFE_REPLACEMENT, True)


def test_output_guardrail_passes_normal_sentences():
    sentence = "That sounds really heavy. What's been on your mind most tonight?"
    assert screen_output(sentence) == (sentence, False)


@pytest.mark.parametrize("text", [
    "Ich will nicht mehr leben",
    "Ich will mich umbringen",
    "Ohne mich wären alle besser dran",
    "Ich denke an Suizid",
])
def test_german_crisis_language_is_caught(text):
    assert screen_input(text) is Risk.CRISIS


@pytest.mark.parametrize("text", ["Ich würde mich nie umbringen", "Ich kann nicht mehr", "Ich fühle mich so wertlos"])
def test_german_distress_is_elevated(text):
    assert screen_input(text) is Risk.ELEVATED


@pytest.mark.parametrize("text", [
    "I wanna die",
    "I don't wanna live anymore",
    "I don't wanna be alive anymore",
    "Honestly, I don't wanna wake up tomorrow.",
    "I don’t want to live anymore",  # speech-to-text can write a curly apostrophe
    "I do not want to live anymore",
    "I'm gonna kill myself",
])
def test_spoken_forms_are_caught(text):
    assert screen_input(text) is Risk.CRISIS


@pytest.mark.parametrize("text", [
    "I'm not okay and I want to die",
    "I'm not okay and I don't want to live anymore",
    "I can't sleep, I'm not fine, but I want to kill myself",
    "Ich kann nicht schlafen und ich will sterben",
])
def test_negation_does_not_cross_a_clause(text):
    assert screen_input(text) is Risk.CRISIS


@pytest.mark.parametrize("text", ["I'm not gonna kill myself", "I would never wanna hurt myself"])
def test_negated_spoken_forms_stay_elevated(text):
    assert screen_input(text) is Risk.ELEVATED


@pytest.mark.parametrize("text", ["I wanna sleep", "I don't wanna go to work tomorrow", "I'm gonna be late", "my wannabe boss"])
def test_spoken_forms_alone_are_not_flagged(text):
    assert screen_input(text) is Risk.NONE


def test_german_output_guardrail_uses_german_line():
    safe, replaced = screen_output("Du hast wahrscheinlich eine Depression.", "de")
    assert replaced and "Ärztin" in safe
