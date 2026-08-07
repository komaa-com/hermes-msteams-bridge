"""ElevenLabs ``/with-timestamps`` — the *viseme-timing upgrade*, not the TTS engine.

Provider support: the streaming voice path supports **every Hermes TTS provider**
(Edge, OpenAI, ElevenLabs, MiniMax, Mistral/Voxtral, Google Gemini, xAI, DeepInfra,
NeuTTS, KittenTTS, Piper, plus ``tts.providers.<name>`` command templates) through
the host's registered ``text_to_speech`` tool — see ``hermes_api.text_to_speech``;
the operator's ``tts.provider`` config decides, exactly per the official Hermes TTS
feature docs. This module is an *optional layer on top*: only ElevenLabs exposes a
``/with-timestamps`` endpoint returning per-character start times
(`POST /v1/text-to-speech/{voice_id}/with-timestamps`, response
``audio_base64`` + ``alignment.character_start_times_seconds``), which we turn into
real viseme ``speech.marks`` instead of estimator output. When ElevenLabs is not
configured we fall back silently (``None``) and the generic multi-provider path
speaks with estimated visemes — audio never depends on timing.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"


def resolve_config() -> dict | None:
    """Resolve ``{api_key, voice_id, model_id}`` from env or the documented
    Hermes TTS schema (``tts.elevenlabs.*`` in config.yaml + ``ELEVENLABS_API_KEY``)."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("TEAMS_VOICE_ELEVENLABS_VOICE_ID", "").strip()
    model_id = os.getenv("TEAMS_VOICE_ELEVENLABS_MODEL", "").strip() or "eleven_multilingual_v2"
    if not (api_key and voice_id):
        try:
            from .hermes_api import elevenlabs_tts_config

            el = elevenlabs_tts_config()
            api_key = api_key or el.get("api_key", "")
            voice_id = voice_id or el.get("voice_id", "")
            model_id = (el.get("model_id") or model_id).strip()
        except Exception:  # noqa: BLE001
            pass
    if not (api_key and voice_id):
        return None
    return {"api_key": api_key, "voice_id": voice_id, "model_id": model_id}


async def synth_with_timestamps(text: str, config: dict) -> tuple[bytes, list[tuple[str, int]]] | None:
    """Return ``(mp3_bytes, [(char, start_ms), …])`` or ``None`` on failure."""
    import aiohttp

    # Per the official API reference: output_format rides the query string;
    # the body carries text + model_id (default eleven_multilingual_v2).
    url = ENDPOINT.format(voice_id=config["voice_id"]) + "?output_format=mp3_44100_128"
    headers = {"xi-api-key": config["api_key"], "Content-Type": "application/json"}
    body = {"text": text, "model_id": config.get("model_id") or "eleven_multilingual_v2"}
    MAX_BODY = 24 * 1024 * 1024  # base64 audio for a long turn stays well under this
    try:
        timeout = aiohttp.ClientTimeout(total=30)  # a stalled request must not hang the turn
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("[teams_voice] elevenlabs with-timestamps %s", resp.status)
                    return None
                raw_body = await resp.content.read(MAX_BODY + 1)
                if len(raw_body) > MAX_BODY:
                    logger.warning("[teams_voice] elevenlabs response exceeded the body cap")
                    return None
                import json as _json

                data = _json.loads(raw_body)
    except (aiohttp.ClientError, ValueError) as exc:
        logger.warning("[teams_voice] elevenlabs with-timestamps failed: %s", exc)
        return None

    audio_b64 = data.get("audio_base64")
    if not audio_b64:
        return None
    align = data.get("alignment") or {}
    chars = align.get("characters") or []
    starts = align.get("character_start_times_seconds") or []
    timing = [(c, int(float(st) * 1000)) for c, st in zip(chars, starts)]
    return base64.b64decode(audio_b64), timing
