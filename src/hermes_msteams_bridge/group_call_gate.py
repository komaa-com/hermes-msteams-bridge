"""Group-call "speak only when addressed" gate — port of ``group-call-gate.ts``.

In a group/meeting call (2+ humans) the assistant stays silent until addressed by
name (a configured wake phrase), then a short follow-up window lets the
back-and-forth continue without repeating the name. 1:1 calls always respond
(gate off). The streaming path enforces this deterministically; the realtime
path uses it to build an instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_WAKE_PHRASES: tuple[str, ...] = ("assistant",)
DEFAULT_FOLLOW_UP_WINDOW_MS = 12_000


@dataclass(frozen=True)
class GroupCallGateConfig:
    """Resolved gate settings; the single source of the defaults."""

    require_address: bool = True
    wake_phrases: tuple[str, ...] = DEFAULT_WAKE_PHRASES
    follow_up_window_ms: int = DEFAULT_FOLLOW_UP_WINDOW_MS


@dataclass(frozen=True)
class GateDecision:
    respond: bool
    addressed: bool


def resolve_group_call_gate_config(raw: dict | None) -> GroupCallGateConfig:
    """Merge a raw config block onto the defaults (one place owns the defaults)."""
    raw = raw or {}
    phrases = raw.get("wakePhrases") or raw.get("wake_phrases")
    if phrases:
        wake = tuple(str(p).strip().lower() for p in phrases if str(p).strip())
    else:
        wake = DEFAULT_WAKE_PHRASES
    return GroupCallGateConfig(
        require_address=bool(raw.get("requireAddress", raw.get("require_address", True))),
        wake_phrases=wake or DEFAULT_WAKE_PHRASES,
        follow_up_window_ms=int(
            raw.get("followUpWindowMs", raw.get("follow_up_window_ms", DEFAULT_FOLLOW_UP_WINDOW_MS))
        ),
    )


def is_addressed(transcript: str, wake_phrases: tuple[str, ...]) -> bool:
    """Case-insensitive, word-boundary match of any wake phrase in ``transcript``.

    ``\\w`` boundaries are Unicode-aware in Python by default, so Arabic/Latin
    wake phrases both work. An empty phrase list never matches (gate inert).
    """
    if not transcript or not wake_phrases:
        return False
    lowered = transcript.lower()
    for phrase in wake_phrases:
        phrase = phrase.strip().lower()
        if not phrase:
            continue
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            return True
    return False


def should_respond_to_group_turn(
    *,
    transcript: str,
    is_group: bool,
    config: GroupCallGateConfig,
    last_addressed_at_ms: int | None,
    now_ms: int,
) -> GateDecision:
    """Decide whether to respond to a finished caller turn.

    1:1 (``is_group=False``) or ``require_address=False`` always responds. In a
    group with the gate on, respond when addressed by name OR still inside the
    follow-up window after the last addressed turn.
    """
    addressed = is_addressed(transcript, config.wake_phrases)
    # The gate is active only for a real group call with require_address AND at
    # least one non-empty wake phrase. An empty wake_phrases tuple makes is_addressed
    # always False, so an active gate would mute the bot forever — treat "no trigger
    # configured" as gate-off (mirror group-call-gate.ts:102).
    gate_active = (
        is_group
        and config.require_address
        and any(p.strip() for p in config.wake_phrases)
    )
    if not gate_active:
        return GateDecision(respond=True, addressed=addressed)

    if addressed:
        return GateDecision(respond=True, addressed=True)

    if (
        last_addressed_at_ms is not None
        and now_ms - last_addressed_at_ms <= config.follow_up_window_ms
    ):
        return GateDecision(respond=True, addressed=False)

    return GateDecision(respond=False, addressed=False)
