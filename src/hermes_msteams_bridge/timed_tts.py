"""Provider-agnostic timed TTS — the viseme-timing layer.

Every Hermes TTS provider produces speech (via the host's ``text_to_speech``
tool, operator-configured). *Timing* is a separate, optional capability: a
provider that can return per-character (or per-word) timestamps yields **real**
viseme ``speech.marks``; everything else falls back to the estimator, so every
provider always gets visemes — timing only upgrades their accuracy.

This module owns that upgrade path as a small registry instead of a hardcoded
provider. A timed provider is::

    async def synth(text: str) -> tuple[bytes, list[tuple[str, int]]] | None
        # (encoded audio bytes, [(char, start_ms), ...]) or None to pass

registered with :func:`register_timed_provider` together with a zero-arg
``available()`` gate. Providers are tried in registration order; the first one
that is available and succeeds wins. ElevenLabs (``/with-timestamps``) is the
built-in registration because it is currently the only Hermes-supported
provider whose public API exposes character timestamps; when another provider
grows timing support, it registers here and nothing downstream changes.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# (char, start_ms) pairs — the alignment shape viseme_estimate consumes.
CharTiming = list[tuple[str, int]]
TimedSynth = Callable[[str], Awaitable[Optional[tuple[bytes, CharTiming]]]]

_PROVIDERS: list[tuple[str, Callable[[], bool], TimedSynth]] = []


def register_timed_provider(name: str, available: Callable[[], bool], synth: TimedSynth) -> None:
    """Register a timing-capable TTS provider (replaces same-name entries)."""
    global _PROVIDERS
    _PROVIDERS = [p for p in _PROVIDERS if p[0] != name]
    _PROVIDERS.append((name, available, synth))


def timed_providers() -> list[str]:
    """Names of registered timing-capable providers (in try order)."""
    return [name for name, _a, _s in _PROVIDERS]


async def synth_with_timing(text: str) -> tuple[bytes, CharTiming] | None:
    """First available registered provider's ``(audio_bytes, char_timing)``.

    ``None`` means no timing-capable provider is configured or all failed —
    callers then use the generic multi-provider TTS path + the estimator.
    """
    for name, available, synth in _PROVIDERS:
        try:
            if not available():
                continue
            result = await synth(text)
            if result is not None:
                return result
        except Exception:  # noqa: BLE001 — timing is an upgrade, never a blocker
            logger.warning("[teams_call] timed TTS provider %s failed", name, exc_info=True)
    return None


# ── Built-in registration: ElevenLabs /with-timestamps ───────────────────────


def _elevenlabs_available() -> bool:
    """Only when the operator actually selected ElevenLabs.

    Credentials alone are NOT consent: Hermes ships a default ElevenLabs voice
    id, so a leftover key would otherwise divert speech (cost, voice, and data
    processing) away from the configured provider - Edge, Gemini, OpenAI, a
    local engine - without anyone asking for it.
    """
    from . import elevenlabs_tts
    from .hermes_api import active_tts_provider

    from .config import plugin_env

    provider = active_tts_provider()
    # Consent is explicit: either tts.provider selects elevenlabs, or the
    # operator set the plugin's OWN voice-id env (a deliberate opt-in). An
    # empty/default provider with a leftover key must never divert speech.
    explicitly_opted_in = bool(plugin_env("TEAMS_CALL_ELEVENLABS_VOICE_ID", "").strip())
    if provider != "elevenlabs" and not explicitly_opted_in:
        return False
    return elevenlabs_tts.resolve_config() is not None


async def _elevenlabs_synth(text: str) -> tuple[bytes, CharTiming] | None:
    from . import elevenlabs_tts

    cfg = elevenlabs_tts.resolve_config()
    if not cfg:
        return None
    return await elevenlabs_tts.synth_with_timestamps(text, cfg)


register_timed_provider("elevenlabs", _elevenlabs_available, _elevenlabs_synth)
