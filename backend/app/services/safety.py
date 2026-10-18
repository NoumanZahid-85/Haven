"""
Safety layer — runs on BOTH sides of the LLM.

Input screen (before the LLM):
  crisis    explicit self-harm / suicide language. The LLM is bypassed and a
            fixed, clinically-reviewed-style response with helpline routing
            is spoken instead. Deterministic on purpose: the one turn that
            matters most must never depend on sampling temperature.
  elevated  hopelessness / distress markers. The LLM still answers, but the
            persona prompt is told to slow down and gently check on safety.
  none      normal conversation.

The screen is tuned for recall over precision — a false positive costs one
gentle check-in, a false negative can cost much more. Negated statements
("I would never hurt myself") are downgraded to `elevated`, not ignored.

Output screen (per sentence, before TTS):
  The reply is streamed sentence by sentence into speech, so each sentence is
  checked before it is synthesized: no diagnoses, no medication dosing, no
  claiming to be a human clinician. A violating sentence is swapped for a
  safe line instead of aborting the whole reply.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Risk(StrEnum):
    NONE = "none"
    ELEVATED = "elevated"
    CRISIS = "crisis"


_CRISIS_PATTERNS = [
    r"\bkill(ing)? my ?self\b",
    r"\bend(ing)? (my|it) (life|all)\b",
    r"\bend it all\b",
    r"\bsuicid",
    r"\b(want|wanna|going) to die\b",
    r"\bwish i (was|were) dead\b",
    r"\bbetter off dead\b",
    r"\bbetter off (without me|if i (was|were) (dead|gone))\b",
    r"\bif i (was|were) dead\b",
    r"\bdon'?t want to (be alive|live|exist|wake up)\b",
    r"\bno reason to (live|go on|be here)\b",
    r"\b(hurt|harm|cut|cutting) my ?self\b",
    r"\bself[- ]?harm",
    r"\boverdose\b",
    r"\btak(e|ing) all (of )?(my|the|these) (pills|tablets|meds)\b",
    # German
    r"\b(mich|mir das leben) (umbringen|nehmen)\b",
    r"\bumbringen\b",
    r"\bselbstmord",
    r"\bsuizid",
    # German word order moves the verb ("ehrlich gesagt will ich nicht mehr leben"),
    # so match the phrase itself rather than a fixed verb position.
    r"\bnicht mehr (leben|da sein|aufwachen)\b",
    r"\b(will|möchte) (\w+ ){0,3}(sterben|tot sein)\b",
    r"\bmein leben beenden\b",
    r"\bmich (ritzen|selbst verletzen)\b",
    r"\b(alle|ohne mich) besser dran\b",
]

_ELEVATED_PATTERNS = [
    r"\bhopeless\b",
    r"\bworthless\b",
    r"\bcan'?t (go on|do this|take (it|this)) any ?more\b",
    r"\bcan'?t go on\b",
    r"\bno point( in)? (anything|living|trying)\b",
    r"\bwhat'?s the point\b",
    r"\b(nobody|no one) would (care|miss me|notice)\b",
    r"\b(i'?m|i am) a burden\b",
    r"\bempty inside\b",
    r"\bgive up on everything\b",
    r"\bdisappear\b",
    r"\bpanic attack\b",
    # German
    r"\bhoffnungslos\b",
    r"\bwertlos\b",
    r"\bich kann nicht mehr\b",
    r"\b(alles|es ist alles) sinnlos\b",
    r"\beine last\b",
    r"\bleer (innen|in mir)\b",
    r"\bpanikattacke\b",
    r"\b(keiner|niemand) würde mich vermissen\b",
]

_NEGATION = re.compile(r"\b(not|never|n'?t|no longer|wouldn'?t|won'?t|nie|niemals|nicht|kein)\b[\w\s']{0,20}$")

_CRISIS_RE = [re.compile(p, re.IGNORECASE) for p in _CRISIS_PATTERNS]
_ELEVATED_RE = [re.compile(p, re.IGNORECASE) for p in _ELEVATED_PATTERNS]


def screen_input(text: str) -> Risk:
    """Classify a user utterance into a risk tier."""
    lowered = text.lower()
    for pattern in _CRISIS_RE:
        match = pattern.search(lowered)
        if match is None:
            continue
        # "I'd never kill myself" — still worth a check-in, not the crisis script.
        if _NEGATION.search(lowered[: match.start()]):
            return Risk.ELEVATED
        return Risk.CRISIS
    if any(p.search(lowered) for p in _ELEVATED_RE):
        return Risk.ELEVATED
    return Risk.NONE


CRISIS_RESPONSE = (
    "I'm really glad you told me, and I'm taking what you said seriously. "
    "You deserve support from a real person right now. "
    "If you might act on these thoughts, please call your local emergency number. "
    "In the US you can call or text 988, and in the UK or Ireland you can call Samaritans on 116 123. "
    "Anywhere else, findahelpline.com lists free, confidential lines in your country. "
    "I'm here with you while you reach out. Are you safe right now?"
)

CRISIS_RESPONSE_DE = (
    "Ich bin wirklich froh, dass du mir das erzählst, und ich nehme es ernst. "
    "Du verdienst jetzt Unterstützung von einem echten Menschen. "
    "Wenn du in Gefahr bist, ruf bitte sofort den Notruf 112 an. "
    "Die TelefonSeelsorge erreichst du rund um die Uhr kostenlos unter 0800 111 0 111 oder 0800 111 0 222. "
    "In anderen Ländern findest du auf findahelpline.com eine kostenlose Nummer. "
    "Ich bin hier bei dir, während du anrufst. Bist du gerade in Sicherheit?"
)


def crisis_response(lang: str = "en") -> str:
    return CRISIS_RESPONSE_DE if lang == "de" else CRISIS_RESPONSE


ELEVATED_GUIDANCE = (
    "The user may be in significant distress. Slow down. Reflect what you heard "
    "with warmth, do not suggest techniques yet, and gently ask whether they are "
    "safe right now. If they mention any intent to harm themselves, encourage "
    "them to contact a local crisis line or emergency services."
)

_OUTPUT_BLOCKLIST = [
    re.compile(r"\b\d+\s?(mg|milligrams?|ml)\b", re.IGNORECASE),  # dosing
    re.compile(r"\byou (have|are suffering from|might have|probably have) (clinical )?"
               r"(depression|bipolar|ptsd|ocd|adhd|an? (anxiety|personality|eating) disorder)\b", re.IGNORECASE),
    re.compile(r"\bi(?:'m| am) (a|your) (therapist|psychologist|psychiatrist|doctor)\b", re.IGNORECASE),
    re.compile(r"\b(stop|increase|double) (taking )?your (medication|meds|pills)\b", re.IGNORECASE),
    # German
    re.compile(r"\bdu hast (wahrscheinlich |vermutlich )?(eine )?(depression|angststörung|ptbs|bipolare störung)\b",
               re.IGNORECASE),
    re.compile(r"\bich bin (dein|deine) (therapeut|therapeutin|psychologe|psychologin|arzt|ärztin)\b", re.IGNORECASE),
    re.compile(r"\b(medikamente|tabletten) (absetzen|verdoppeln)\b", re.IGNORECASE),
]

SAFE_REPLACEMENT = "That's something a doctor or therapist is the right person to help with."
SAFE_REPLACEMENT_DE = "Dafür ist eine Ärztin, ein Arzt oder eine Therapeutin die richtige Ansprechperson."


def screen_output(sentence: str, lang: str = "en") -> tuple[str, bool]:
    """Return (sentence to speak, was_replaced)."""
    if any(p.search(sentence) for p in _OUTPUT_BLOCKLIST):
        return (SAFE_REPLACEMENT_DE if lang == "de" else SAFE_REPLACEMENT), True
    return sentence, False
