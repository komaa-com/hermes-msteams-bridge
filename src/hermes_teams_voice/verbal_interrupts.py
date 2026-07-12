"""Deterministic verbal interrupts — "stop" / "hold on" / Arabic equivalents.

A spoken interrupt cuts playback in code (not by
trusting the model), so it works even when the model wouldn't have stopped. The
match is **whole-utterance** with filler and configured wake-phrase stripping:

* ``"stop"`` / ``"⟨name⟩, stop"`` / ``"please stop"`` → interrupt
* ``"stop by the store"`` → NOT an interrupt (substring never triggers)

Unicode-aware (``\\p{L}``-style via Python's default ``\\w``) with tashkeel-safe
Arabic normalization, so Arabic interrupts ("توقف", "خلاص", …) match identically.
"""

from __future__ import annotations

import re
import unicodedata

# Whole-utterance interrupt phrases (normalized). English + Arabic.
_INTERRUPT_PHRASES: frozenset[str] = frozenset(
    {
        # English
        "stop",
        "stop stop",
        "wait",
        "wait wait",
        "hold on",
        "hold up",
        "never mind",
        "nevermind",
        "quiet",
        "be quiet",
        "shut up",
        "enough",
        "cancel",
        "cancel that",
        # Arabic
        "توقف",
        "قف",
        "خلاص",
        "كفى",
        "اسكت",
        "بس",
        "كفاية",
    }
)

# Leading/trailing filler stripped before matching.
_FILLER: frozenset[str] = frozenset(
    {"um", "uh", "er", "ok", "okay", "please", "hey", "yeah", "no", "من", "فضلك", "يا"}
)

# Arabic diacritics (tashkeel) + tatweel to strip for robust matching.
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _TASHKEEL.sub("", text)
    text = text.lower()
    text = _NON_WORD.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_edges(tokens: list[str], wake_lists: list[list[str]]) -> list[str]:
    """Iteratively peel leading/trailing filler and wake phrases until stable."""

    def peel_leading(toks: list[str]) -> bool:
        changed = False
        while toks and toks[0] in _FILLER:
            toks.pop(0)
            changed = True
        for wl in wake_lists:
            if wl and toks[: len(wl)] == wl:
                del toks[: len(wl)]
                return True
        return changed

    def peel_trailing(toks: list[str]) -> bool:
        changed = False
        while toks and toks[-1] in _FILLER:
            toks.pop()
            changed = True
        for wl in wake_lists:
            if wl and len(toks) >= len(wl) and toks[len(toks) - len(wl) :] == wl:
                del toks[len(toks) - len(wl) :]
                return True
        return changed

    while tokens and peel_leading(tokens):
        pass
    while tokens and peel_trailing(tokens):
        pass
    return tokens


def is_verbal_interrupt(transcript: str, wake_phrases: tuple[str, ...] = ()) -> bool:
    """True if ``transcript`` is (just) a verbal interrupt, after stripping
    leading/trailing wake phrases and filler words."""
    norm = _normalize(transcript)
    if not norm:
        return False
    wake_lists = [_normalize(p).split(" ") for p in wake_phrases if _normalize(p)]
    tokens = _strip_edges([t for t in norm.split(" ") if t], wake_lists)
    return " ".join(tokens) in _INTERRUPT_PHRASES
