---
title: "Voice Modes & Providers"
description: "Realtime speech-to-speech vs streaming STT to agent to TTS, provider selection, and VAD tuning."
---

The plugin runs one **call handler** per call - the "brain" that turns caller audio
into agent responses. You pick it with `--handler` on `serve`:

```bash
hermes teams-voice serve --handler realtime    # or: streaming | echo | logging
```

## The handlers at a glance

| `--handler` | What it is | Needs |
|---|---|---|
| `logging` (default) | Logs frames, sends no audio back. Confirms the handshake + lifecycle. | nothing |
| `echo` | Smiles on connect and echoes the caller's audio back. A smoke test. | nothing |
| `realtime` | Full **speech-to-speech** brain (OpenAI/Azure Realtime). Lowest latency. | a realtime API key |
| `streaming` | Half-duplex **STT → agent → TTS**. Works with any STT/TTS provider. | `ffmpeg` on PATH |

## Realtime (OpenAI / Azure speech-to-speech)

The realtime handler bridges the call to a provider Realtime WebSocket. The caller's
audio streams straight to the model and the model's audio streams straight back,
giving natural, low-latency conversation with **barge-in** (the caller can interrupt
mid-reply and the bot stops immediately).

Under the hood: caller PCM 16 kHz is resampled to 24 kHz for the model, and the
model's 24 kHz audio is resampled back to 16 kHz and chopped into 20 ms / 640-byte
frames for StandIn. Transcript deltas drive **expression** cues and estimated
**visemes** for lip-sync. See [Wire Protocol](/hermes-plugin-teams-voice/wire-protocol/) for the frame format.

Provider selection is automatic (see [Configuration Reference](/hermes-plugin-teams-voice/configuration-reference/#azure-auto-selection)):

- **OpenAI** - the default. Set `backend: openai`, `model: gpt-realtime`, and
  `api_key: ${OPENAI_API_KEY}`.
- **Azure OpenAI** - chosen when `backend: azure`, an `azure_endpoint` is set, or an
  explicit `*.azure.com` URL is given. Provide `azure_endpoint`, `azure_deployment`,
  `azure_api_version`, and a key (`AZURE_OPENAI_API_KEY` / `AZURE_FOUNDRY_API_KEY`
  are used as fallbacks so you can reuse the gateway key).

```yaml
realtime:
  backend: azure
  azure_endpoint: https://<your-azure-resource>.cognitiveservices.azure.com
  azure_deployment: gpt-realtime
  azure_api_version: 2025-04-01-preview
  voice: cedar
  api_key: ${AZURE_FOUNDRY_API_KEY}
```

If the realtime provider is unreachable at connect time, or drops mid-call, the
plugin **tears the Teams call down cleanly** rather than leaving the caller in silent
dead air.

### Voice, model, and instructions

- `voice` - the realtime voice name (default `alloy`; e.g. `cedar`).
- `model` / `azure_deployment` - the realtime model or Azure deployment.
- `instructions` - the system prompt. The built-in default keeps replies brief and
  conversational and tells the model to **delegate real work to the agent** rather
  than guessing. The plugin also augments your instructions per-call with the
  caller's first name, the group-gate etiquette, and (if enabled) the bilingual
  directive.

### VAD tuning

Server-VAD decides when the caller has started/stopped talking:

- `vad_threshold` (default `0.5`) - higher is less sensitive (ignores quieter
  speech / more background noise); lower is more eager.
- `prefix_padding_ms` (default `300`) - audio kept *before* detected speech start,
  so the first syllable isn't clipped.
- `silence_duration_ms` (default `500`) - trailing silence that ends a turn. Lower
  = snappier turn-taking but more risk of cutting the caller off; higher = more
  patient.

### Caller transcription

`input_transcribe_model` (default `whisper-1`) transcribes the caller's audio so the
handler can detect **wake words** and **verbal interrupts**. Set it to `none` /
`off` / `disabled` (or empty) if your deployment doesn't support it - VAD barge-in
still works, but wake-word / verbal-interrupt logic degrades gracefully.

## Streaming (STT → agent → TTS)

The streaming handler is **half-duplex and turn-based**: it segments the caller's
audio into utterances with VAD, transcribes each, applies the verbal-interrupt and
group-call gates on the transcript, runs the **Hermes agent**, then speaks the reply
via TTS with expression and estimated visemes. It is simpler than realtime but works
with **any STT/TTS provider** and needs no realtime model.

- **Requires `ffmpeg` on PATH** to decode TTS audio to PCM. If it's missing, `serve`
  warns you.
- STT and TTS come from your Hermes install's configured transcription/TTS tools.
- If **ElevenLabs** is configured, the streaming path uses its `/with-timestamps`
  endpoint for real per-character viseme timing; otherwise it falls back to the
  configured TTS plus the viseme estimator.

Use streaming when you want provider flexibility, don't have realtime access, or
want to reuse an existing STT/TTS stack.

## Echo and logging (diagnostics)

- **`echo`** - the quickest proof the whole media path works: it smiles on connect
  and echoes your audio back, so you hear yourself and see the avatar animate. No
  provider key needed.
- **`logging`** - the base handler: it logs every frame and sends nothing back.
  Ideal for confirming the HMAC handshake and call lifecycle before you wire up a
  provider.
