"""
Interruption judge — decides whether words heard while Haven is speaking
mean "stop and listen" or not.

Plain voice-activity barge-in reacts to every sound: a cough, a "mm-hmm",
or Haven's own voice leaking from laptop speakers back into the mic. A
companion that stops every time you say "yeah" feels like it isn't
listening. So while Haven speaks, the mic keeps being transcribed and each
partial transcript is judged on what was actually said:

  echo         the words match what Haven just said → speaker leak, ignore
  backchannel  only "mm-hmm", "yeah", "okay", "genau"… → keep talking
  stop phrase  starts with "wait", "hold on", "sorry", "warte"… → stop now
  takeover     three or more real words → the person has something to say

Inspired by Cue (github.com/ioteverythin/cue), which makes the same
STOP-vs-backchannel call with an acoustic model. Cue's weights aren't
public, so this judge works on the streaming transcript instead — free,
language-aware, and it doubles as an echo guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-zäöüß']+")

BACKCHANNELS = {
    # English
    "mm", "mmm", "mhm", "mmhmm", "hmm", "hm", "uh", "huh", "uhhuh", "um", "ah", "oh",
    "yeah", "yes", "yep", "yup", "ok", "okay", "right", "sure", "true", "cool", "nice",
    "i", "see", "got", "it", "totally", "exactly", "alright",
    # German
    "ja", "jo", "genau", "stimmt", "aha", "achso", "klar", "gut", "richtig",
}

STOP_PHRASES = (
    # English
    "wait", "stop", "hold on", "hang on", "sorry", "excuse me", "actually", "no", "but",
    "can i", "let me", "one sec", "pause",
    # German
    "warte", "stopp", "halt", "moment", "entschuldigung", "sorry", "eigentlich", "nein",
    "aber", "kann ich", "lass mich",
)

TAKEOVER_WORDS = 3
ECHO_OVERLAP = 0.6


# Hums are spelled every way by recognizers ("mhmm", "mmm-hmm", "uh-huh",
# "hmmm"), so recognise them by shape instead of listing every spelling.
_FILLER_RE = re.compile(r"^(m+h*m+|h+m+|u+h+(h+u+h+)?|a+h+|o+h+|e+r+m*)$")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("-", ""))


def _is_backchannel(word: str) -> bool:
    return word in BACKCHANNELS or bool(_FILLER_RE.match(word))


@dataclass(frozen=True)
class Verdict:
    interrupt: bool
    reason: str  # echo | backchannel | stop-phrase | takeover | too-short


def judge(heard: str, bot_said: str = "") -> Verdict:
    """Judge one (partial) transcript heard while Haven was speaking."""
    words = _words(heard)
    if not words:
        return Verdict(False, "too-short")

    if bot_said:
        bot_words = set(_words(bot_said))
        overlap = sum(1 for w in words if w in bot_words) / len(words)
        if len(words) >= 2 and overlap >= ECHO_OVERLAP:
            return Verdict(False, "echo")

    padded = " ".join(words) + " "
    if any(padded.startswith(p.strip() + " ") for p in STOP_PHRASES):
        return Verdict(True, "stop-phrase")

    content = [w for w in words if not _is_backchannel(w)]
    if not content:
        return Verdict(False, "backchannel")
    if len(content) >= TAKEOVER_WORDS:
        return Verdict(True, "takeover")
    return Verdict(False, "too-short")
