"""ElevenLabs TTS with real per-character timing (the viseme "full pass").

When ElevenLabs is configured, the streaming path synthesizes via the
``/with-timestamps`` endpoint, which returns the audio **and** per-character start
times. We turn those into real viseme ``speech.marks`` (vs the text estimator), so
the avatar mouth changes shape on the actual sound timing. Falls back silently
(``None``) when unconfigured or on any failure — audio never depends on timing.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"


def resolve_config() -> dict | None:
    """Resolve ``{api_key, voice_id, model_id}`` from env or the Hermes TTS config."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("TEAMS_VOICE_ELEVENLABS_VOICE_ID", "").strip()
    model_id = os.getenv("TEAMS_VOICE_ELEVENLABS_MODEL", "").strip() or "eleven_multilingual_v2"
    if not (api_key and voice_id):
        try:
            from tools.tts_tool import _load_tts_config

            el = (_load_tts_config().get("providers") or {}).get("elevenlabs") or {}
            api_key = api_key or str(el.get("apiKey") or el.get("api_key") or "").strip()
            voice_id = voice_id or str(el.get("voiceId") or el.get("voice_id") or el.get("voice") or "").strip()
            model_id = str(el.get("modelId") or el.get("model_id") or model_id).strip()
        except Exception:  # noqa: BLE001
            pass
    if not (api_key and voice_id):
        return None
    return {"api_key": api_key, "voice_id": voice_id, "model_id": model_id}


async def synth_with_timestamps(text: str, config: dict) -> tuple[bytes, list[tuple[str, int]]] | None:
    """Return ``(mp3_bytes, [(char, start_ms), …])`` or ``None`` on failure."""
    import aiohttp

    url = ENDPOINT.format(voice_id=config["voice_id"])
    headers = {"xi-api-key": config["api_key"], "Content-Type": "application/json"}
    body = {"text": text, "model_id": config.get("model_id") or "eleven_multilingual_v2",
            "output_format": "mp3_44100_128"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("[teams_voice] elevenlabs with-timestamps %s", resp.status)
                    return None
                data = await resp.json()
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
