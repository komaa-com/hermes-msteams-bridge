"""Tests for Voice UX completion: verbal interrupts + expression states."""

from __future__ import annotations

from hermes_msteams_bridge import expression, verbal_interrupts
from hermes_msteams_bridge.verbal_interrupts import is_verbal_interrupt

WAKE = ("assistant", "hermes", "aria")


def test_bare_interrupts():
    assert is_verbal_interrupt("stop")
    assert is_verbal_interrupt("STOP!")
    assert is_verbal_interrupt("hold on")
    assert is_verbal_interrupt("never mind")
    assert is_verbal_interrupt("cancel that")


def test_filler_and_wake_phrase_stripping():
    assert is_verbal_interrupt("please stop")
    assert is_verbal_interrupt("um, stop")
    assert is_verbal_interrupt("assistant, stop", WAKE)  # leading wake phrase
    assert is_verbal_interrupt("stop aria", WAKE)  # trailing wake phrase
    assert is_verbal_interrupt("hey hermes, hold on", WAKE)


def test_not_an_interrupt_substring():
    assert not is_verbal_interrupt("stop by the store")
    assert not is_verbal_interrupt("what time is it in tokyo")
    assert not is_verbal_interrupt("can you wait for the report")  # 'wait' mid-sentence
    assert not is_verbal_interrupt("")


def test_arabic_interrupts_and_tashkeel():
    assert is_verbal_interrupt("توقف")
    assert is_verbal_interrupt("خلاص")
    assert is_verbal_interrupt("خَلَاص")  # with tashkeel diacritics
    assert is_verbal_interrupt("يا مساعد، توقف", ("مساعد",))  # Arabic wake-phrase strip
    assert not is_verbal_interrupt("ما هو الطقس اليوم")  # normal Arabic sentence


def test_thinking_expression_constant():
    assert expression.THINKING == "thinking"
    # the heuristic never infers 'thinking' from text (it's a state cue)
    assert expression.infer_emotion("I am thinking about it") != expression.THINKING
