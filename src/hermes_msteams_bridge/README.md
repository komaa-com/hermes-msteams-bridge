# `hermes_msteams_bridge` - internal module guide

This is the developer-facing map of the Python package. For install/usage, see the
[repo README](../README.md); for architecture, see [DESIGN.md](../DESIGN.md); for the
full config and wire reference, see the
[documentation site](https://komaa-com.github.io/hermes-msteams-bridge/).

## What this package does

`msteams_bridge` is a Hermes plugin that adds **Microsoft Teams voice/video** to a
Hermes agent. It hosts a local, HMAC-authenticated WebSocket server. The hosted
**StandIn media bridge** joins the Teams call and dials into that server; this
package runs the *brain* of the call - dialogue (realtime speech-to-speech or
streaming STT → agent → TTS), perception (camera/screen vision), and the avatar
driver cues (expression / visemes / show-to-caller) - and sends them back over the
WebSocket. StandIn renders in the Teams call; this package drives.

```
StandIn media bridge ──HMAC WebSocket──▶ msteams_bridge (this package)
  (joins the Teams call)                   • bridge_server.py - WS server
                                           • handlers.py       - call brain
                                           • realtime/         - speech-to-speech
                                           • protocol.py       - the wire contract
```

The plugin is the **WebSocket server** (binds `127.0.0.1:8443` by default); StandIn
is the **client** that dials in.

Chat depends on the connection mode. In **StandIn Managed Bot** mode this package
also hosts the chat lane itself (`managed_chat.py`, HTTP on `messages_port`), so one
process carries both planes of one binding. In **bring-your-own-Azure-bot** mode the
chat plane is the separate `plugins/platforms/teams` adapter and this package is the
*media/voice* half only.

## Module map

| Layer | Module(s) |
|---|---|
| Bridge WS server (HMAC, replay guard, lifecycle, ping/pong, conn caps, reaper) | `bridge_server.py` |
| Wire protocol (inbound decode + outbound builders) | `protocol.py` |
| HMAC handshake + single-use replay guard | `hmac_auth.py` |
| Config (config.yaml + env, allowlist, vision cap, recap, session scope) | `config.py` |
| Realtime mode (OpenAI/Azure speech-to-speech) | `realtime/openai_client.py`, `handlers.py` |
| Streaming mode (STT → agent → TTS; needs `ffmpeg`) | `streaming_audio.py`, `handlers.py` |
| Shared session policy (allowlist, scope, greeting, group gate, pending-outbound) | `call_session_base.py` |
| Echo guard · group gate · verbal interrupts (EN/AR) | `echo_guard.py`, `group_call_gate.py`, `verbal_interrupts.py` |
| Vision keyframe ring + per-call spend cap | `vision_store.py`, `vision_budget.py` |
| Realtime tool schemas + dispatch | `realtime_tools.py`, `call_tools.py` |
| Managed chat lane (HMAC endpoint, ordered turns, reply leg, turn-query builder) | `managed_chat.py` |
| Inbound voice messages: origin-pinned fetch + STT into the chat turn (opt-in) | `voice_messages.py` |
| Agent bridge (consult / background task) | `agent_consult.py` |
| Avatar emotion + viseme cues | `expression.py`, `viseme_estimate.py` |
| Meeting transcript / minutes; delivery as a Teams **file card** (Word `.docx` via the Bot Framework attachment contract, same as the Hermes Teams adapter's `send_document`) with text fallback; local `.docx` artifact | `meeting.py`, `meeting_docx.py` |
| Audio (resample, 20 ms framing, RMS) | `audio.py` |
| ElevenLabs TTS (timestamp alignment) | `elevenlabs_tts.py` |
| Outbound "call me back" place-call | `outbound.py` |
| Agent-facing status tool | `tools.py` |
| CLI (`hermes msteams-bridge {status,serve}`) | `cli.py` |
| Plugin registration | `__init__.py` |

## Call handlers

`bridge_server.py` dispatches inbound frames into a `CallSessionHandler`. The
`--handler` flag on `serve` picks one:

- `logging` (default `CallSessionHandler`) - logs frames, no audio back.
- `echo` (`EchoCallSessionHandler`) - smiles on connect, echoes caller audio; a
  dependency-light smoke test.
- `realtime` (`RealtimeCallSessionHandler`) - full speech-to-speech brain over the
  provider Realtime WebSocket.
- `streaming` (`StreamingCallSessionHandler`) - half-duplex STT → agent → TTS;
  works with any STT/TTS provider.

`realtime` and `streaming` extend `BaseTeamsCallHandler` (`call_session_base.py`),
which owns the shared session policy.

## Wire contract (the protocol StandIn speaks with the plugin)

- **Handshake:** `HMAC-SHA256(sharedSecret, "{timestampMs}.{callId}")`, lowercase
  hex, sent as the `X-StandIn-Timestamp` / `X-StandIn-Signature` headers on the
  WebSocket upgrade (the only accepted names). ±60 s window; accepted `(callId, ts, sig)`
  tuples are single-use.
- **Path:** `/voice/msteams/stream/{callId}` (the URL `callId` is authenticated and
  cross-checked against the `session.start` body).
- **Audio:** PCM 16 kHz, 16-bit, mono, little-endian; 20 ms / 640-byte frames,
  base64.
- **Messages** (camelCase JSON, additive - unknown fields/types degrade gracefully):
  - inbound: `session.start`, `session.end`, `recording.status`, `audio.frame`,
    `video.frame`, `participants`, `dtmf`, `ping`, `assistant.say`
  - outbound: `audio.frame`, `assistant.cancel`, `expression`, `speech.marks`,
    `display.image`, `pong`

The `sharedSecret` here **must equal** the value paired in StandIn or the handshake
fails. The full field-level tables are on the
[Wire Protocol](https://komaa-com.github.io/hermes-msteams-bridge/wire-protocol/)
wiki page.

## Configuration

`TeamsVoiceConfig` (`config.py`) resolves values in priority order: the
`plugins.entries.msteams_bridge.config` block in `config.yaml`, then environment
variables, then safe defaults. `RealtimeConfig` (`realtime/openai_client.py`)
resolves the realtime provider (OpenAI or Azure) the same way. Secrets are never
logged. Every key, env var, and default is documented on the
[Configuration Reference](https://komaa-com.github.io/hermes-msteams-bridge/configuration-reference/)
wiki page.

Example `config.yaml`:

```yaml
plugins:
  enabled:
    - msteams_bridge
  entries:
    msteams_bridge:
      config:
        shared_secret: ${MSTEAMS_BRIDGE_SHARED_SECRET}   # secret stays in .env
        host: 127.0.0.1
        port: 8443
        realtime:
          backend: azure                # azure | openai
          azure_endpoint: https://<your-azure-resource>.cognitiveservices.azure.com
          azure_deployment: gpt-realtime
          azure_api_version: 2025-04-01-preview
          voice: cedar
          api_key: ${AZURE_FOUNDRY_API_KEY}   # secret stays in .env
          vad_threshold: 0.5
          prefix_padding_ms: 300
          silence_duration_ms: 500
```

Each config.yaml key has a matching env var (e.g. `realtime.azure_endpoint` ↔
`MSTEAMS_BRIDGE_AZURE_ENDPOINT`); config.yaml wins where both are set.

## Microsoft Graph permissions (the bot app)

Your Teams bot's Azure AD app (paired in StandIn) needs these **application**
permissions, admin-consented, for full functionality:

| Permission | Enables |
|---|---|
| `Calls.JoinGroupCall.All` | answer / join Teams calls and meetings |
| `Calls.AccessMedia.All` | access the call's real-time audio/video media |
| `Chat.Read.All` | resolve chat / thread ids and read message context |
| `ChatMessage.Read.Chat` | read messages in chats the bot is installed in |

Outbound "call me back" additionally needs `Calls.InitiateGroupCall.All` (skip if
unused). Pairing your own bot with StandIn is done in the StandIn dashboard - see
[standin.komaa.com](https://standin.komaa.com) and [docs.komaa.com](https://docs.komaa.com).

## Run

```bash
hermes msteams-bridge status      # show resolved config + readiness
hermes msteams-bridge serve --handler realtime   # run the bridge server (foreground)
# or standalone:
python -m hermes_msteams_bridge.bridge_server
```

## Test

```bash
pytest hermes_msteams_bridge/tests/ -v
```
