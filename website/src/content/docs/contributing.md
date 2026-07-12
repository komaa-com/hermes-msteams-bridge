---
title: "Contributing"
description: "Dev setup, tests, conventions, and a contributor-level module map."
---

Contributions are welcome! This page is a quick orientation; the authoritative guide
is [`CONTRIBUTING.md`](https://github.com/komaa-com/hermes-msteams-bridge/blob/main/CONTRIBUTING.md)
in the repo, and the architecture is in
[`DESIGN.md`](https://github.com/komaa-com/hermes-msteams-bridge/blob/main/DESIGN.md).

## Dev setup (short version)

- **Python ≥ 3.10.**
- Editable install: `uv pip install -e .` (or plain `pip install -e .`).
  Optional fast audio: `uv pip install -e ".[numpy]"`.
- Run the tests: `pytest src/hermes_teams_voice/tests/ -v`. They cover the wire protocol,
  HMAC handshake + replay guard, echo guard, group-call gate, verbal interrupts,
  viseme estimation, vision budget, and audio helpers - no network, no Hermes needed.
- Exercise the plugin locally with `TEAMS_VOICE_SHARED_SECRET=dev hermes teams-voice
  serve --handler echo` (or `logging`) - neither needs a provider key.

## Branch + PR conventions

- Branch off `main`; prefix with `feat/`, `fix/`, `docs/`, or `chore/`.
- One logical change per PR; add/update tests with behavior changes.
- CI runs the suite on pull requests. Never commit secrets.

## Module map (contributor level)

| Area | Where |
|---|---|
| WS server, lifecycle, `CallSessionHandler` | `bridge_server.py` |
| Wire protocol (decode + builders) | `protocol.py` |
| HMAC handshake + replay guard | `hmac_auth.py` |
| Config resolution | `config.py`, `realtime/openai_client.py` |
| Call brains (echo / realtime / streaming) | `handlers.py`, `call_session_base.py` |
| Realtime provider client | `realtime/openai_client.py` |
| Audio (resample / frame / RMS) | `audio.py`, `streaming_audio.py` |
| Gates & guards | `echo_guard.py`, `group_call_gate.py`, `verbal_interrupts.py` |
| Vision | `vision_store.py`, `vision_budget.py` |
| Avatar cues | `expression.py`, `viseme_estimate.py` |
| Tools | `realtime_tools.py`, `call_tools.py`, `agent_consult.py` |
| Meetings | `meeting.py`, `meeting_docx.py` |
| Outbound call-back | `outbound.py` |
| CLI / registration | `cli.py`, `__init__.py` |

See [DESIGN.md](https://github.com/komaa-com/hermes-msteams-bridge/blob/main/DESIGN.md)
for how these fit together and the design invariants to preserve.

## One rule to remember

This repo documents only the **wire protocol** the plugin speaks with the hosted
StandIn media bridge - **never StandIn's internal implementation**. Keep that
boundary in code comments and docs alike.
