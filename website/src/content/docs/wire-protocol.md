---
title: "Wire Protocol"
description: "The WebSocket contract with the StandIn media bridge: HMAC handshake, audio format, and every message type."
---

This is the contract the **StandIn media bridge** speaks with the plugin over the
WebSocket. It's useful if you're debugging a connection, writing tests, or extending
a handler. Everything here matches `protocol.py`, `hmac_auth.py`, and
`bridge_server.py`.

The design goal is **forward compatibility**: messages are camelCase JSON, additive,
and tolerant - **unknown fields are ignored** and **unknown message types degrade
gracefully**, so older and newer peers interoperate.

## The upgrade

For each Teams call, StandIn opens **one WebSocket** to:

```text
ws://<host>:<port>/voice/msteams/stream/{callId}
```

`{callId}` in the URL is authenticated by the HMAC headers and later
**cross-checked** against the `callId` in the `session.start` body - a mismatch
closes the call.

### The two HMAC headers

On the upgrade request, StandIn sends:

| Header | Value |
|---|---|
| `X-OpenClawTeamsBridge-Timestamp` | the signing timestamp, in **milliseconds** |
| `X-OpenClawTeamsBridge-Signature` | the signature (lowercase hex) |

The signature is:

```text
HMAC-SHA256(shared_secret, "{timestampMs}.{callId}")   # lowercase hex
```

Example:

```text
X-OpenClawTeamsBridge-Timestamp: 1720598400000
X-OpenClawTeamsBridge-Signature: 9f8c...   (hex digest of "1720598400000.abc123")
```

Verification order (`verify_upgrade`): header presence → timestamp parse + window →
constant-time signature compare → single-use replay check. On success the WebSocket
is accepted; any failure returns **HTTP 401**.

- **Window:** the timestamp must be within **±60 s** of now (`hmac_window_ms`).
- **Replay guard:** each accepted `(callId, timestamp, signature)` tuple is
  **single-use**; a captured handshake can't be replayed. Entries expire at the
  timestamp's own horizon (`ts + window`).

### Connection guards

After auth: a global `max_connections` (64) and per-IP `max_connections_per_ip` (8)
cap return **503** when exceeded; a **duplicate live `callId`** closes the new
socket (POLICY_VIOLATION) rather than clobbering the running call; and a connection
that doesn't send `session.start` within `pre_start_timeout_s` (10 s) is reaped.

The same server also answers a plain HTTP `GET /health` with `ok` - a liveness
probe, not part of the call protocol.

## Audio format

All audio on the wire is **PCM, 16 kHz, 16-bit signed, mono, little-endian**, carried
as **20 ms / 640-byte frames**, base64-encoded in the `payloadBase64` field. (The
realtime model internally uses 24 kHz; the plugin resamples on both sides.)

## Inbound messages (StandIn → plugin)

All frames are JSON text with a `type` discriminator. Fields below are camelCase.

### `session.start`

Opens the call. `callId` and `threadId` are required.

```json
{
  "type": "session.start",
  "callId": "abc123",
  "threadId": "19:meeting_xyz@thread.v2",
  "caller": { "aadId": "00000000-...", "displayName": "Ada Lovelace", "tenantId": "..." },
  "recordingStatus": "active",
  "direction": "inbound"
}
```

| Field | Type | Notes |
|---|---|---|
| `callId` | string (required) | Must match the URL `callId`. |
| `threadId` | string (required) | Teams chat/thread id. |
| `caller` | object | `aadId`, `displayName`, `tenantId` - all best-effort/nullable (blank → null). |
| `recordingStatus` | string | `active` \| `inactive` \| `unknown`. |
| `direction` | string | `inbound` \| `outbound` (outbound = a call-back delivery leg). |

### `session.end`

```json
{ "type": "session.end", "reason": "hangup" }
```

`reason` is a free-form string.

### `recording.status`

```json
{ "type": "recording.status", "status": "active" }
```

`status` (required): `active` \| `inactive` \| `unknown`. Media processing is gated
until `active` unless `require_recording_status` is off.

### `audio.frame`

```json
{ "type": "audio.frame", "seq": 42, "timestampMs": 840, "payloadBase64": "…", "speakerName": "Ada" }
```

| Field | Type | Notes |
|---|---|---|
| `seq` | int | Frame sequence number. |
| `timestampMs` | int | Playout timestamp in ms. |
| `payloadBase64` | string (required) | PCM 16 kHz / 20 ms / 640 bytes, base64. |
| `speakerName` | string | Optional - unmixed-audio speaker attribution for the minutes. |

### `video.frame`

```json
{ "type": "video.frame", "source": "screenshare", "ts": 1234, "width": 1280,
  "height": 720, "mime": "image/jpeg", "dataBase64": "…",
  "participantId": "…", "participantName": "Ada" }
```

| Field | Type | Notes |
|---|---|---|
| `source` | string (required) | `camera` \| `screenshare`. |
| `ts` | int | Frame timestamp (a new `ts` = a new scene). |
| `width`, `height` | int | Pixel dimensions. |
| `mime` | string | Defaults to `image/jpeg`. |
| `dataBase64` | string (required) | The encoded image. |
| `participantId`, `participantName` | string | Optional attribution. |

### `participants`

```json
{ "type": "participants", "count": 3 }
```

Drives the group-call gate (2+ humans = meeting mode).

### `dtmf`

```json
{ "type": "dtmf", "digit": "1" }
```

`digit` (required): `0`-`9`, `*`, or `#`.

### `ping`

```json
{ "type": "ping", "ts": 1720598400000 }
```

The plugin replies with a `pong` echoing `ts`.

### `assistant.say`

```json
{ "type": "assistant.say", "text": "We're at time - thanks for calling, goodbye!" }
```

`text` (required). StandIn asks the agent to speak this line - e.g. a brief goodbye
right before a limit cutoff. Not recording-gated (StandIn explicitly requested it).

## Outbound messages (plugin → StandIn)

### `audio.frame`

TTS / realtime audio back to the caller. Same shape as inbound `audio.frame`
(`seq`, `timestampMs`, `payloadBase64`).

### `assistant.cancel`

```json
{ "type": "assistant.cancel", "turnId": 7 }
```

Barge-in - flush playback for `turnId`.

### `expression`

```json
{ "type": "expression", "emotion": "happy" }
```

Avatar emotion cue: `neutral` \| `happy` \| `sad` \| `surprised` \| `thinking`.
Cosmetic/best-effort.

### `speech.marks`

```json
{ "type": "speech.marks", "ts": 840, "marks": [ { "tMs": 0, "visemeId": 12 }, { "tMs": 60, "visemeId": 3 } ] }
```

Viseme timeline for lip-sync (`marks` = `[{tMs, visemeId}]`).

### `display.image`

```json
{ "type": "display.image", "dataBase64": "…", "mime": "image/png", "ts": 0,
  "durationMs": 5000, "mode": "fullscreen", "caption": "Here's the chart" }
```

`show_to_caller` - render an image on the bot's tile. `durationMs`, `mode`, and
`caption` are optional.

### `pong`

```json
{ "type": "pong", "ts": 1720598400000 }
```

Keepalive reply echoing the inbound `ping` timestamp.

## Message ↔ handler callback map

The server routes each inbound frame to a `CallSessionHandler.on_*` callback:

| Inbound message | Handler callback |
|---|---|
| `session.start` | `on_session_start` |
| `audio.frame` | `on_audio_frame` |
| `video.frame` | `on_video_frame` |
| `recording.status` | `on_recording_status` |
| `participants` | `on_participants` |
| `dtmf` | `on_dtmf` |
| `assistant.say` | `on_assistant_say` |
| `session.end` (or abrupt close) | `on_session_end` |
| `ping` | *(answered by the server with `pong`)* |

Handlers drive audio and avatar cues back via `CallSession.send_*` helpers, which
serialize the outbound builders above.
