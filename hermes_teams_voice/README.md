# teams_voice — Microsoft Teams real-time voice/video (CVI) bridge driver

The **Python driver** half of the Conversational Video Interface (CVI) for
Microsoft Teams in Hermes — the cross-platform orchestration for Teams voice/
video calls.

> **Two processes, one bridge.** Teams real-time call media
> (`Microsoft.Skype.Bots.Media`) is **Windows/.NET-only**, so the avatar tile and
> RTP media are rendered by a separate **C# media worker**. This plugin is the
> cross-platform *brain*: it hosts the
> WebSocket the worker dials into, runs dialogue + perception, and sends the
> avatar **driver cues**. The worker renders; this plugin drives.

```
Hermes (this plugin) ──HMAC WebSocket──▶ AzureBot C# media worker ──▶ Teams call
  • bridge_server.py (WS server)            • renders NV12 avatar tile
  • dialogue (realtime / streaming)         • samples inbound A/V, forwards DTMF
  • perception (vision ring)                • recording-status compliance gate
  • emits expression / visemes / image
```

The worker is the **WebSocket client**; this plugin is the **server**
(binds `127.0.0.1:8443` by default). Chat-plane features (messages, message
actions, meeting-recap posting) are handled by the existing
`plugins/platforms/teams` adapter (the `microsoft-teams-apps` SDK), **not** here.

## Status — implemented

| Layer | File | Status |
|---|---|---|
| Bridge WS server (HMAC, replay guard, lifecycle, ping/pong, conn caps) | `bridge_server.py` | ✅ |
| Wire protocol (mirrors worker `Protocol.cs`) | `protocol.py` | ✅ |
| Config (config.yaml + env, allowlist, vision cap, recap, …) | `config.py` | ✅ |
| **Realtime mode** (OpenAI/Azure speech-to-speech) | `realtime/openai_client.py`, `handlers.py` | ✅ |
| **Streaming mode** (STT→agent→TTS; needs `ffmpeg` on PATH) | `streaming_audio.py`, `handlers.py` | ✅ |
| Echo guard · group gate · verbal interrupts (EN/AR) · DTMF · bilingual | `echo_guard.py`, `group_call_gate.py`, `verbal_interrupts.py` | ✅ + tests |
| Vision (`look_at_screen` live/history) · ambient push · budget cap | `vision_store.py`, `vision_budget.py` | ✅ + tests |
| Tools: consult, agent_task, show_to_caller, call_me_back, post_meeting_minutes | `realtime_tools.py`, `handlers.py` | ✅ |
| Visemes (estimator; Latin) | `viseme_estimate.py` | ✅ + tests · Arabic + real ElevenLabs alignment = follow-up |
| Meeting recap/minutes (text; posts via Bot Framework REST) | `meeting.py` | ✅ · DOCX attach = follow-up |
| CLI: `hermes teams-voice serve --handler {logging,echo,realtime,streaming}` | `cli.py` | ✅ |

Validated on a live Teams call (realtime): connect, recording gate, expression
cues, avatar rendered, audio out, clean teardown. The Windows .NET media worker
renders the avatar tile and is paired over the HMAC bridge.

## Features

**Voice** — `msteams` over **realtime** (OpenAI/Azure speech-to-speech) **and streaming** (STT→agent→TTS), with barge-in; caller allowlist (AAD-id, closed when configured); recording-status gate before any media-derived data; realtime delegation (`hermes_agent_consult` inline + `hermes_agent_task` background); deterministic verbal interrupts (EN + Arabic, wake-phrase + filler stripping, whole-utterance); **DTMF / IVR**; **bilingual Arabic/English**; roster greeting by first name; "thinking" expression.

**Inbound video vision** — `video.frame` ingestion + `look_at_screen` (camera + screen-share); **realtime continuous vision** (latest changed frame pushed ~6s, no forced response); scene-change ambient (per source) + **retroactive** (`scope:"history"`, 16-keyframe ring, attributed); per-call **vision spend cap** (`maxVisionPerMinute`).

**CVI rendering drivers** — **expression cues** (neutral/happy/sad/surprised + thinking); **viseme `speech.marks`** lip-sync; **`show_to_caller`** → `display.image` fullscreen or PiP overlay, captions, paced slideshow.

**Group / meeting** — per-participant attribution; **speak only when addressed** (2+ humans, wake phrases, follow-up window; 1:1 always responds), race-free on realtime (auto-response off in meetings).

**Outbound (call me back)** — `call_me_back` via the worker's HMAC, SSRF-guarded endpoint; **greet-on-answer**; pending-result correlation with TTL.

**Chat & governance** (`plugins/platforms/teams`) — **"Ask Hermes about this"** message action; **voice-message transcription** (opt-in); **audit-log channel** (opt-in, loop-guarded); **DLP outbound redaction** (opt-in) on text, adaptive cards, and captions.

**Meeting productivity** — **end-of-meeting recap** (opt-in) + on-demand **`post_meeting_minutes`** / "summarize the meeting", posted to the Teams chat; per-speaker attribution from unmixed audio.

**Sessions** — `session_scope` per-call / per-thread / per-aad.

Recent: real ElevenLabs `/with-timestamps` viseme alignment (streaming path; estimator fallback), Arabic visemes, and Word-openable `.docx` minutes — uploaded to SharePoint (OneDrive) and attached to the Teams chat as a native file card when `sharePointSiteId` is configured (text-only otherwise).

## Worker-owned (inherited from the media worker — not in this driver)

These are Microsoft Graph Calling / `Skype.Bots.Media` concerns. The driver bridges
audio/video/control but is not connected to Graph Calling and does not drive media
subscription, so it **cannot** implement them — they are inherited from the reused
.NET worker, and the driver must not try to reimplement them:

* **Outbound voicemail fallback** — the worker places the Graph call; voicemail +
  its transcription is Teams platform behavior. The driver only supplies the spoken
  result text.
* **Outbound auto-hangup / no-answer timeout** — the *policy* (when to give up) can
  live here, but terminating a Teams call is a Graph action only the worker can do.
* **Active-speaker camera follow / re-select** — the worker does dominant-speaker
  matching + MSI/VBSS subscription; the driver only receives `video.frame`.
* **Roster `displayName`** — Graph leaves `DisplayName` null on the incoming-call
  identity; the worker resolves it from the live participant roster before sending
  `session.start`. **Fixed worker-side** (`multi-identity-per-ip @ 2cb86e6`+); the
  driver already reads `caller.displayName`, so the by-name greeting works with no
  driver change. Run that worker build (or cherry-pick the commit) for named
  greetings; PSTN / anonymous / unmatched callers fall through to a generic greeting.

## Wire contract (fixed by the worker — do not drift)

* **Handshake:** `HMAC-SHA256(sharedSecret, "{timestampMs}.{callId}")`, lowercase
  hex, sent as the worker's `X-OpenClawTeamsBridge-Timestamp` / `-Signature` headers on the WS
  upgrade. ±60 s window; accepted `(callId, ts, sig)` tuples are single-use.
* **Path:** `/voice/msteams/stream/{callId}`.
* **Audio:** PCM 16 kHz, 16-bit, mono, little-endian; 20 ms / 640-byte frames, base64.
* **Messages** (camelCase JSON, additive): inbound `session.start` / `session.end`
  / `recording.status` / `audio.frame` / `video.frame` / `participants` / `dtmf`
  / `ping`; outbound `audio.frame` / `expression` / `speech.marks` /
  `display.image` / `assistant.cancel` / `pong`.

The `sharedSecret` here **must equal** the worker's shared-secret setting.

## Microsoft Graph permissions

The bot's Azure AD app needs these **application** permissions (admin-consented):

| Permission | Enables |
|---|---|
| `Calls.JoinGroupCall.All` | answer / join Teams calls and meetings |
| `Calls.AccessMedia.All` | access the call's real-time audio/video media (`Skype.Bots.Media`) |
| `Chat.Read.All` | resolve chat / thread ids and read message context |
| `ChatMessage.Read.Chat` | read messages in chats the bot is installed in (resource-specific consent) |
| `Sites.ReadWrite.All` | upload files / minutes to SharePoint (OneDrive) for chat attachments |

Outbound "call me back" additionally needs `Calls.InitiateGroupCall.All` (skip if unused).

## Configure

Two sources are supported (per the Hermes docs); **config.yaml takes precedence, `.env`
is the fallback**. The recommended pattern keeps **secrets in `.env`** and references
them from config.yaml with `${VAR}` (the loader expands them), so config lives in one
declarative file without copying secrets around.

**config.yaml** (`%LOCALAPPDATA%\hermes\config.yaml`):

```yaml
plugins:
  enabled:
    - teams_voice
  entries:
    teams_voice:
      config:
        shared_secret: ${TEAMS_VOICE_SHARED_SECRET}   # secret stays in .env
        host: 127.0.0.1
        port: 8443
        # Attach meeting-minutes .docx to the Teams chat (needs Graph
        # Sites.ReadWrite.All on the bot app); omit for text-only minutes.
        share_point_site_id: ${TEAMS_SHAREPOINT_SITE_ID}
        realtime:
          backend: azure
          azure_endpoint: https://pcfcaoai2.cognitiveservices.azure.com
          azure_deployment: gpt-realtime
          azure_api_version: 2025-04-01-preview
          voice: cedar
          api_key: ${AZURE_FOUNDRY_API_KEY}           # secret stays in .env
          vad_threshold: 0.5
          prefix_padding_ms: 300
          silence_duration_ms: 500
```

**`.env`** (`%LOCALAPPDATA%\hermes\.env`) — the secret store (used directly, or referenced above):

```bash
TEAMS_VOICE_SHARED_SECRET=...        # must equal the worker's shared-secret setting
AZURE_FOUNDRY_API_KEY=...            # realtime key (also used by the gateway)
# SharePoint (OneDrive) site for attaching files/minutes to chats — host,siteGuid,webGuid
# (the bot AAD app — TEAMS_CLIENT_ID — needs Graph Sites.ReadWrite.All, admin-consented):
TEAMS_SHAREPOINT_SITE_ID=contoso.sharepoint.com,<siteGuid>,<webGuid>
# fully env-only is fine too:
TEAMS_VOICE_HOST=127.0.0.1
TEAMS_VOICE_PORT=8443
TEAMS_VOICE_REALTIME_BACKEND=azure
TEAMS_VOICE_AZURE_ENDPOINT=https://pcfcaoai2.cognitiveservices.azure.com
TEAMS_VOICE_AZURE_DEPLOYMENT=gpt-realtime
TEAMS_VOICE_AZURE_API_VERSION=2025-04-01-preview
TEAMS_VOICE_REALTIME_VOICE=cedar
```

Each config.yaml key has a matching env var (e.g. `realtime.azure_endpoint` ↔
`TEAMS_VOICE_AZURE_ENDPOINT`); config.yaml wins where both are set.

## Run

```bash
hermes teams-voice status      # show config + readiness
hermes teams-voice serve       # run the bridge server (foreground)
# or, standalone:
python -m hermes_teams_voice.bridge_server
```

Point the worker's WebSocket base-URL setting at this server
(`ws://<host>:8443/voice/msteams/stream`) with a matching shared secret. One
worker identity per gateway — the worker's multi-identity config lets one host
serve multiple gateways.

## Test

```bash
pytest plugins/teams_voice/tests/ -v
```

## Roadmap (next increments)

1. **Realtime client** (`realtime/openai_client.py`): OpenAI/Azure realtime over
   WS, 16k↔24k resampling, `expression`/`speech.marks` emission, barge-in.
2. **Dialogue handler**: a `CallSessionHandler` that owns the recording gate,
   echo guard, group-gate enforcement, and delegates real work to `run_agent`.
3. **Perception**: a 16-frame vision ring + `look_at_screen` / ambient push.
4. **Avatar tools**: `show_to_caller` → `display.image`.
5. **Outbound**: "call me back" via the worker's authenticated place-call endpoint.
6. **Arabic visemes** + bilingual parity.

See `C:\AzureBot\docs\CVI-STUDY-91438-92081.md` for the full feature/architecture study.
