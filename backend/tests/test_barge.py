"""Interruption judge: what counts as "stop and listen" while Haven is speaking."""

from __future__ import annotations

import pytest

from app.voice.barge import judge

BOT = "So one thing you could try tonight is breathing in for four counts and out for six."


@pytest.mark.parametrize("heard", ["Mm-hmm", "yeah", "okay okay", "right", "uh huh", "ja", "genau", "Ja, genau."])
def test_backchannels_keep_haven_talking(heard):
    assert judge(heard, BOT).interrupt is False


@pytest.mark.parametrize("heard", ["Wait", "wait, sorry", "Hold on a second", "Sorry, can I say something",
                                   "Actually", "Warte mal", "Moment", "Stopp"])
def test_stop_phrases_interrupt_immediately(heard):
    verdict = judge(heard, BOT)
    assert verdict.interrupt and verdict.reason == "stop-phrase"


def test_a_real_takeover_interrupts():
    verdict = judge("my manager keeps changing the deadline every week", BOT)
    assert verdict.interrupt and verdict.reason == "takeover"


def test_haven_hearing_itself_is_ignored():
    verdict = judge("breathing in for four counts", BOT)
    assert not verdict.interrupt and verdict.reason == "echo"


def test_one_or_two_words_wait_for_more():
    assert judge("the thing", BOT).interrupt is False


@pytest.mark.parametrize("heard", ["Mhmm.", "mmm-hmm", "Hmmm", "uh-huh"])
def test_hum_spellings_are_backchannels(heard):
    assert judge(heard, BOT).reason == "backchannel"
