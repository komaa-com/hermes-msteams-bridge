---
title: "Features"
description: "A full tour of dialogue, vision, avatar cues, meetings, tools, telephony, and reliability features."
---

A tour of what the plugin can do on a Teams call. Everything here is implemented in
this repo; the hosted StandIn media bridge handles the Teams media so these features
"just work" once you're connected.

## Dialogue

- **Realtime speech-to-speech** (`--handler realtime`) - OpenAI/Azure Realtime; low
  latency, full-duplex feel.
- **Streaming STT → agent → TTS** (`--handler streaming`) - half-duplex, works with
  any STT/TTS provider (needs `ffmpeg`).
- **Barge-in** - the caller can interrupt the bot mid-reply; playback is flushed
  (`assistant.cancel`) and the model response is cancelled immediately.
- **Verbal interrupts (EN/AR)** - deterministic "stop" / "توقف" / "⟨name⟩, stop"
  detection that cuts playback even without VAD.
- **Recording gate** - unless `require_recording_status` is off, no media-derived
  data is processed until Teams recording is `active`. Greetings fire **on answer**,
  not while ringing.
- **Echo guard** - suppresses the bot hearing its own output (the self-answer fix),
  using an RMS + playout-clock heuristic.
- **Greet on answer** - the caller is greeted by first name once they answer.

## Group / meeting etiquette

- **Speak only when addressed** - in a call with 2+ humans, the agent stays silent
  unless someone addresses it by a **wake phrase** (`wake_phrases`, default
  `assistant, hermes`), then a short follow-up window lets the exchange continue
  without repeating the name. 1:1 calls always respond.
- **Race-free on realtime** - server-VAD auto-response stays **OFF** until a 1:1 is
  confirmed (via the `participants` count), so no audio can leak into a meeting
  before the gate decides. Addressed turns are triggered manually.
- **Response-active reset** - a rejected/failed model response clears the
  response-active latch so the next turn can always speak (no permanent muting).
- **Per-speaker attribution** - unmixed-audio `speakerName` attributes each turn in
  the minutes.

## Vision

- **`look_at_screen`** - the agent looks at the caller's shared **screen** or
  **camera** to answer a question; `scope: "live"` (current frame) or
  `scope: "history"` (recent keyframes, a 16-frame ring, to answer about something
  shown earlier).
- **Continuous / ambient vision (realtime)** - the latest *changed* frame per source
  is pushed to the model about every 6 s (no forced response), so the model stays
  visually aware between explicit looks.
- **Per-call vision budget** - `max_vision_per_minute` (default 30) caps spend across
  `look_at_screen` + ambient push; over budget, ambient pushes back off.

## Avatar rendering cues

- **Expression** - a cheap lexical classifier infers `neutral` / `happy` / `sad` /
  `surprised` from the reply text and sends an `expression` cue; a `thinking` face
  shows while a tool runs.
- **Visemes** - a viseme timeline (`speech.marks`) drives lip-sync; the streaming
  path uses **real ElevenLabs `/with-timestamps`** timing when available, otherwise
  an estimator.
- **`show_to_caller`** - generate an image and render it on the bot's own video tile
  (`display.image`), fullscreen or PiP, with an optional caption and a paced
  slideshow.

## Realtime tools

The realtime model is given these function tools (dispatched by the handler):

| Tool | What it does |
|---|---|
| `hermes_agent_consult` | Delegate a question/action to the Hermes agent inline; returns a short spoken result. |
| `hermes_agent_task` | Run a long background job; acknowledge now, deliver the result via a call-back. |
| `look_at_screen` | Look at the shared screen/camera (live or history) and answer. |
| `show_to_caller` | Generate an image and show it on the bot's tile. |
| `call_me_back` | Place an outbound Teams call back to deliver a pending result. |
| `post_meeting_minutes` | Summarize the meeting and post minutes to the Teams chat. |

## Meetings & productivity

- **End-of-call recap** (`meeting_recap`) - post minutes (key points, decisions,
  action items) to the Teams chat when the call ends.
- **On-demand minutes** - `post_meeting_minutes` or "summarize the meeting" posts
  minutes mid-call.
- **`.docx` to SharePoint** - with `share_point_site_id` set, minutes are uploaded to
  SharePoint (OneDrive) and attached to the chat as a native file card; text-only
  otherwise.

## Telephony & languages

- **DTMF / IVR** - keypad presses are surfaced to the model so it can run "press 1
  to…" flows.
- **Bilingual EN/AR** (`bilingual`) - the model detects and mirrors the caller's
  language and translates on request.

## Sessions

- **`session_scope`** - memory continuity: `per-call` (fresh each call),
  `per-thread` (keyed by Teams thread), or `per-aad` (keyed by caller identity).

## Reliability & safety

- **Cutoff goodbye** - on a StandIn limit, the agent speaks a goodbye
  (`assistant.say`) before StandIn ends the call gracefully.
- **Provider-drop teardown** - if the realtime provider is unreachable or drops
  mid-call, the Teams call is torn down cleanly instead of leaving dead air.
- **Max-duration reaper** - `max_call_duration_s` bounds a call's wall-clock time so
  a wedged call can't run forever and leak a socket.
- **Caller allowlist** - deny-by-default by AAD id (`allowlist` / `allow_all`;
  optional weaker display-name matching via `allowlist_allow_names`).
- **DoS guards** - global + per-IP connection caps, a max frame size, the pre-start
  timeout, and the single-use HMAC **replay guard**.
- **Outbound SSRF guard** - "call me back" refuses a non-loopback target unless
  `allow_remote_worker` is set (the shared secret would otherwise be sent to that
  host).
