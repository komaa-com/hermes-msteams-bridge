"""Text cleanup before speech synthesis (D8).

Agent replies arrive as chat Markdown; speaking them raw reads the syntax
aloud ("asterisk asterisk bold asterisk asterisk"). Hermes's own voice
pipeline strips markdown and ``<think>`` blocks before TTS; its helper is
private, so this is our equivalent, tuned for spoken delivery on a call.
Applied by the streaming handler's ``_speak`` on every path (timed providers
and the generic fallback alike). Realtime mode is unaffected (model-native
audio).
"""

from __future__ import annotations

import re

_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1")
_STRIKE = re.compile(r"~~(.+?)~~")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HRULE = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_MULTI_BLANK = re.compile(r"\n{2,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def strip_for_speech(text: str) -> str:
    """Markdown/chat syntax → plain speakable text.

    Keeps the words, drops the notation: think blocks and code fences go
    entirely (code read aloud is noise), inline code and emphasis keep their
    content, links keep their label, bare URLs are summarised as "a link".
    """
    t = text or ""
    t = _THINK_BLOCK.sub(" ", t)
    t = _CODE_FENCE.sub(" ", t)
    t = _IMAGE.sub(" ", t)
    t = _LINK.sub(r"\1", t)
    t = _BARE_URL.sub("a link", t)
    t = _TABLE_ROW.sub(" ", t)
    t = _HRULE.sub(" ", t)
    t = _HEADING.sub("", t)
    t = _BLOCKQUOTE.sub("", t)
    t = _INLINE_CODE.sub(r"\1", t)
    # Emphasis can nest (***bold italic***); two passes cover the practical cases.
    t = _EMPHASIS.sub(r"\2", t)
    t = _EMPHASIS.sub(r"\2", t)
    t = _STRIKE.sub(r"\1", t)
    t = _BULLET.sub("", t)
    t = _NUMBERED.sub("", t)
    t = _MULTI_BLANK.sub(". ", t)
    t = t.replace("\n", " ")
    t = _MULTI_SPACE.sub(" ", t)
    return t.strip()
