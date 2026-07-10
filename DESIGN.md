# DESIGN - hermes-plugin-teams-voice

Contributor-facing architecture notes for the `teams_voice` Hermes plugin. If you
want to *use* the plugin, start with the [README](README.md) and the
[community wiki](https://github.com/komaa-com/hermes-plugin-teams-voice/wiki).
This document is for people changing the code.

## What the plugin is

`teams_voice` adds **Microsoft Teams voice/video** to a Hermes AI agent. It is a
pure-Python Hermes plugin, installed into the same environment as Hermes and
discovered through the `hermes_agent.plugins` entry point.

The plugin does **not** join Teams calls itself. That is done by **StandIn** (the
hosted "StandIn media bridge") - a subscription service that joins the Teams
meeting, handles the real-time Teams media, and speaks to this plugin over a
WebSocket. The plugin is the **local WebSocket server**; the StandIn media bridge
is the **client that dials in**. The plugin owns the *brain* of the call
(dialogue, perception, and the avatar driver cues); StandIn owns the Teams media.

```
 Teams call ⇄  StandIn media bridge  ──HMAC WebSocket──▶  teams_voice plugin (Hermes)
   (hosted)      dials into the plugin        • bridge_server.py  - the WS server
                                              • handlers.py        - the call brain
                                              • realtime/          - speech-to-speech client
                                              • the Hermes agent   - real work / tools
```

Everything below the WebSocket line lives in this repo. Everything above it is
StandIn and is out of scope for this plugin - we document only the **wire
protocol** StandIn speaks with us, never its internals.

## The local-WS-server model

Unlike a typical client/gateway split, here **the plugin binds and waits** and the
StandIn media bridge is the WebSocket client:

- The plugin binds `127.0.0.1:8443` by default (loopback - the shared secret must
  never be exposed on a public interface).
- One Teams call opens **one WebSocket connection** to
  `/voice/msteams/stream/{callId}`.
- StandIn authenticates the WebSocket upgrade with two HMAC headers, then sends a
  `session.start` frame, then streams inbound media (audio/video/DTMF/control)
  while the plugin streams TTS/realtime audio and avatar driver cues back.

`bridge_server.py` owns **transport concerns only**: the HMAC handshake, connection
caps, the pre-start timeout, the read/dispatch loop, ping/pong, and the
max-duration reaper. All dialogue/perception logic lives behind a handler
interface so the wire layer never has to change when the "brain" changes.

## The handler abstraction: `CallSessionHandler`

`CallSessionHandler` (in `bridge_server.py`) is the interface the dialogue/
perception brain implements. Every method is `async` and best-effort - a handler
exception is logged and the call continues, so a single bad frame never tears down
the socket. Its callbacks mirror the inbound protocol:

| Callback | Fired on |
|---|---|
| `on_session_start` | the `session.start` frame (after callId is authenticated) |
| `on_audio_frame` | each inbound caller-audio frame |
| `on_video_frame` | each inbound camera/screen-share frame |
| `on_recording_status` | a `recording.status` change |
| `on_participants` | a participant-count update |
| `on_dtmf` | a keypad press |
| `on_assistant_say` | a bridge-requested spoken line (e.g. a cutoff goodbye) |
| `on_session_end` | an explicit `session.end` **or** an abrupt socket close |

The server passes each session a `CallSession` object with typed `send_*` helpers
(`send_audio_frame`, `send_expression`, `send_speech_marks`, `send_display_image`,
`send_assistant_cancel`) that serialize the outbound protocol builders.

`BridgeServer` takes a `handler_factory` and builds **one handler per call**. The
`--handler` CLI flag selects the factory:

- **`logging`** (default) - the base `CallSessionHandler`: logs frames, sends no
  audio back. Useful to confirm the handshake and lifecycle without a provider.
- **`echo`** - `EchoCallSessionHandler`: smiles on connect and echoes the caller's
  audio back. A dependency-light smoke test that proves the full media path works.
- **`realtime`** - `RealtimeCallSessionHandler`: the full speech-to-speech brain
  (OpenAI/Azure Realtime). Needs a realtime API key.
- **`streaming`** - `StreamingCallSessionHandler`: a half-duplex STT → agent → TTS
  loop that works with any STT/TTS provider. Needs `ffmpeg` on PATH.

The realtime and streaming handlers share `BaseTeamsCallHandler`
(`call_session_base.py`), which holds the common session policy: caller allowlist,
session-scope key, meeting transcript, agent consult, the group-call gate, and the
greeting/outbound state. The two subclasses only implement what actually differs.

## The call lifecycle

1. **Upgrade + auth.** StandIn opens a WebSocket to `/voice/msteams/stream/{callId}`
   with the two HMAC headers. `hmac_auth.verify_upgrade` checks presence, the
   timestamp window (±60 s), a constant-time signature compare, and a single-use
   replay guard. A failure returns `401`.
2. **Connection caps.** Global (`max_connections`) and per-IP
   (`max_connections_per_ip`) caps are enforced; a duplicate live `callId` closes
   the new socket rather than clobbering the running call.
3. **Pre-start timeout.** A connection that does not send `session.start` within
   `pre_start_timeout_s` (10 s) is reaped.
4. **`session.start`.** The body's `callId` is cross-checked against the
   authenticated URL `callId` (mismatch closes the call). The handler receives
   caller identity, thread id, direction, and initial recording status.
5. **Recording gate.** Unless `require_recording_status` is off, the brain does not
   process media-derived data until `recording.status` is `active`. Greetings fire
   on answer (recording active), not while ringing.
6. **Turns.** Audio/video/DTMF frames flow to the handler; the brain streams audio
   and avatar cues back. Barge-in, the group gate, echo guard, and tools all run
   here.
7. **Teardown.** On an explicit `session.end` - or an abrupt socket close - the
   server runs `on_session_end` exactly once (idempotent via `session.ended`) so
   realtime sockets and ambient tasks are always cleaned up.
8. **Max-duration reaper.** If `max_call_duration_s > 0`, a wall-clock deadline is
   fixed at `session.start`; a wedged call is torn down once it is exceeded so it
   can't run forever and leak a live socket.

### The cutoff goodbye

When a StandIn limit is reached (a sandbox/free daily cap, or a subscription
max-minutes governor), StandIn sends an `assistant.say` frame carrying a goodbye
line. The handler injects it as an instruction, the agent speaks it in its own
voice, and StandIn then ends the call gracefully.

## The realtime pipeline

`realtime/openai_client.py` (`RealtimeSession`) is a thin async wrapper over the
provider Realtime WebSocket. It is **provider-pure**: it deals only in the model's
native PCM 24 kHz audio and fires callbacks. All resampling, framing, expression/
viseme emission, and barge-in live in the handler.

The end-to-end audio path for a realtime call:

```
caller audio  ── PCM 16 kHz ──▶  echo guard  ──▶  resample 16 kHz → 24 kHz  ──▶  provider Realtime WS
                                                                                        │
                                                                                   model audio
                                                                                        │
back to StandIn  ◀── 640-byte / 20 ms frames  ◀── resample 24 kHz → 16 kHz  ◀── PCM 24 kHz deltas
```

`audio.py` owns the conversions: linear resampling (numpy fast path, pure-stdlib
fallback), 20 ms / 640-byte framing with a carried residual so frame boundaries
stay aligned across streamed deltas, and RMS for the echo guard. Model transcript
deltas drive the coarse `expression` cue (`expression.py`) and estimated visemes
(`viseme_estimate.py`), which ride back as `speech.marks`.

The **streaming** handler is simpler and half-duplex: it segments caller audio into
utterances (VAD), transcribes each, applies the verbal-interrupt and group gates on
the transcript, runs the Hermes agent, and speaks the reply via TTS (ElevenLabs
`/with-timestamps` when available for real viseme timing, otherwise the configured
TTS plus the estimator). `ffmpeg` is used to decode TTS audio to PCM 16 kHz.

## How it talks to the StandIn media bridge (the wire)

`protocol.py` models the wire the StandIn media bridge speaks with the plugin:
newline-free JSON text frames over one WebSocket per call, discriminated on a
`type` field, camelCase keys, additive/forward-compatible (unknown fields ignored,
unknown types degrade gracefully). It provides typed **inbound** dataclasses with a
single `decode()` entry point, and **outbound** builders. `hmac_auth.py` implements
the handshake and the single-use replay guard. `outbound.py` places a "call me
back" over StandIn's loopback HTTP endpoint using the same HMAC scheme, signing
`{ts}.{userObjectId}` (SSRF-guarded to loopback unless `allow_remote_worker`).

The full message tables live in the
[Wire Protocol](https://github.com/komaa-com/hermes-plugin-teams-voice/wiki/Wire-Protocol)
wiki page. **Do not drift** the header names, HMAC payload shape, or default path -
they are the contract both sides agree on.

## Python module layout

| Module | Responsibility |
|---|---|
| `__init__.py` | `register(ctx)` - registers the status tool, CLI, and session hook |
| `cli.py` | `hermes teams-voice {status,serve}`; `--handler` selects the brain |
| `config.py` | `TeamsVoiceConfig`; resolves config.yaml + env + defaults; allowlist check |
| `bridge_server.py` | the WS server, `CallSession`, `CallSessionHandler`, lifecycle |
| `protocol.py` | inbound decode + outbound builders (the wire contract) |
| `hmac_auth.py` | HMAC-SHA256 handshake verify + single-use replay guard |
| `handlers.py` | `Echo` / `Realtime` / `Streaming` call handlers |
| `call_session_base.py` | `BaseTeamsCallHandler` - shared session policy + pending-outbound registry |
| `realtime/openai_client.py` | `RealtimeConfig`, `RealtimeSession` - provider Realtime WS client |
| `realtime_tools.py` | realtime function-tool schemas exposed to the model |
| `call_tools.py` | dispatch/execution for the realtime tools |
| `agent_consult.py` | bridge into the Hermes agent (consult / background task) |
| `audio.py` | PCM16 resample, 20 ms framing, RMS |
| `streaming_audio.py` | VAD utterance buffer, WAV write, TTS decode helpers |
| `echo_guard.py` | self-answer / echo suppression |
| `group_call_gate.py` | "speak only when addressed" gate |
| `verbal_interrupts.py` | deterministic EN/AR verbal interrupts |
| `vision_store.py` / `vision_budget.py` | vision keyframe ring + per-call spend cap |
| `expression.py` / `viseme_estimate.py` | avatar emotion + viseme cues |
| `meeting.py` / `meeting_docx.py` | meeting transcript, minutes, `.docx` |
| `elevenlabs_tts.py` | ElevenLabs TTS with timestamp alignment |
| `outbound.py` | outbound "call me back" place-call |
| `tools.py` | the `teams_voice_status` agent tool |

## Design invariants worth preserving

- **The wire layer stays dumb.** Keep dialogue logic out of `bridge_server.py`.
- **Handlers never crash the call.** Wrap best-effort work; log and continue.
- **Deny by default.** An empty allowlist denies all callers unless `allow_all`.
- **Loopback by default.** The shared secret rides loopback; non-loopback binds and
  place-call targets are opt-in and warned about.
- **Forward-compatible frames.** Add fields, don't rename; ignore unknowns.
- **Never document StandIn's internals.** This repo documents only the protocol it
  speaks with StandIn.
