"""Viseme lip-shape estimation for the avatar mouth.

Produces a timeline of ``{tMs, visemeId}`` marks for the avatar mouth, using the
Microsoft/Azure SSML viseme id set (0-21). Two timing sources:

* :func:`visemes_from_alignment` — real per-character timing (e.g. ElevenLabs
  ``/with-timestamps``); preferred when available.
* :func:`estimate_visemes` — an even-spread estimate from text + audio duration;
  the always-available fallback.

The worker blends these coarse shapes over its RMS-driven mouth openness, so a
missing/empty timeline simply degrades to RMS-only lip-sync. Covers Latin; Arabic
graphemes map to a neutral-open shape (full parity is a follow-up — see TODO).
"""

from __future__ import annotations

from dataclasses import dataclass

# A representative subset of Azure viseme ids. The worker's ``ShapeForViseme``
# maps the full 0-21 range; here we emit the ids the estimator can resolve from
# text. 0 = silence.
VISEME_SILENCE = 0
VISEME_AA = 2  # open vowel (a)
VISEME_EE = 4  # wide (e/i)
VISEME_OH = 8  # round (o)
VISEME_OO = 7  # tight round (u/w)
VISEME_FV = 18  # lip-teeth (f/v)
VISEME_L = 14  # dental-ish (l/d/t/n)
VISEME_MBP = 21  # closed (m/b/p)
VISEME_SS = 15  # wide fricative (s/z)
VISEME_NEUTRAL = 1  # mid-open default

# Grapheme -> viseme id. Lowercase Latin only; everything else -> neutral/open.
_CHAR_VISEME: dict[str, int] = {
    "a": VISEME_AA,
    "e": VISEME_EE,
    "i": VISEME_EE,
    "o": VISEME_OH,
    "u": VISEME_OO,
    "w": VISEME_OO,
    "f": VISEME_FV,
    "v": VISEME_FV,
    "m": VISEME_MBP,
    "b": VISEME_MBP,
    "p": VISEME_MBP,
    "s": VISEME_SS,
    "z": VISEME_SS,
    "l": VISEME_L,
    "d": VISEME_L,
    "t": VISEME_L,
    "n": VISEME_L,
    "r": VISEME_OO,
}

# Arabic grapheme -> viseme id (so Arabic lip-sync isn't neutral-only). Maps the
# common letters to their nearest mouth shape; diacritics/short vowels are handled
# by the surrounding consonants.
_ARABIC_VISEME: dict[str, int] = {
    "ا": VISEME_AA, "أ": VISEME_AA, "إ": VISEME_AA, "آ": VISEME_AA, "ء": VISEME_AA,  # alif/hamza (open)
    "و": VISEME_OO, "ؤ": VISEME_OO,  # waw (round)
    "ي": VISEME_EE, "ى": VISEME_EE, "ئ": VISEME_EE,  # ya (wide)
    "ب": VISEME_MBP, "م": VISEME_MBP,  # bilabial (closed)
    "ف": VISEME_FV,  # fa (lip-teeth)
    "س": VISEME_SS, "ص": VISEME_SS, "ز": VISEME_SS, "ش": VISEME_SS,  # sibilants (wide)
    "ت": VISEME_L, "ث": VISEME_L, "د": VISEME_L, "ذ": VISEME_L, "ط": VISEME_L,
    "ظ": VISEME_L, "ل": VISEME_L, "ن": VISEME_L, "ر": VISEME_L, "ة": VISEME_L,  # dental/alveolar
    "ج": VISEME_AA, "ك": VISEME_AA, "ق": VISEME_AA, "غ": VISEME_AA, "خ": VISEME_AA,
    "ح": VISEME_AA, "ع": VISEME_AA, "ه": VISEME_AA,  # velar/guttural (mouth open)
}


@dataclass(frozen=True)
class VisemeMark:
    """One mouth-shape change at ``t_ms`` (relative to utterance start)."""

    t_ms: int
    viseme_id: int

    def to_dict(self) -> dict[str, int]:
        return {"tMs": self.t_ms, "visemeId": self.viseme_id}


def viseme_for_char(ch: str) -> int:
    """Map a single character to a viseme id (whitespace/punct -> silence)."""
    if not ch or ch.isspace():
        return VISEME_SILENCE
    lowered = ch.lower()
    if lowered in _CHAR_VISEME:
        return _CHAR_VISEME[lowered]
    if ch in _ARABIC_VISEME:
        return _ARABIC_VISEME[ch]
    if lowered.isalpha():
        return VISEME_NEUTRAL  # other scripts: mid-open default
    return VISEME_SILENCE


def _collapse(marks: list[VisemeMark]) -> list[VisemeMark]:
    """Drop consecutive marks with the same viseme id (only emit changes)."""
    out: list[VisemeMark] = []
    last_id: int | None = None
    for m in marks:
        if m.viseme_id != last_id:
            out.append(m)
            last_id = m.viseme_id
    return out


def estimate_visemes(text: str, duration_ms: int) -> list[VisemeMark]:
    """Even-spread viseme timeline across ``duration_ms`` from ``text`` shape.

    Used when the TTS provider returns no per-character timing. Returns an empty
    list for empty text or non-positive duration (worker falls back to RMS-only).
    """
    if not text or duration_ms <= 0:
        return []
    chars = list(text)
    n = len(chars)
    step = duration_ms / n
    marks = [VisemeMark(int(i * step), viseme_for_char(chars[i])) for i in range(n)]
    return _collapse(marks)


def visemes_from_alignment(chars: list[tuple[str, int]]) -> list[VisemeMark]:
    """Build a timeline from real per-character timing.

    ``chars`` is ``[(character, start_ms), ...]`` as surfaced by a TTS provider's
    alignment endpoint. Preferred over :func:`estimate_visemes` when present.
    """
    marks = [VisemeMark(int(start_ms), viseme_for_char(ch)) for ch, start_ms in chars]
    marks.sort(key=lambda m: m.t_ms)
    return _collapse(marks)


def marks_to_payload(marks: list[VisemeMark]) -> list[dict[str, int]]:
    """Convert marks to the wire shape consumed by ``protocol.speech_marks``."""
    return [m.to_dict() for m in marks]
