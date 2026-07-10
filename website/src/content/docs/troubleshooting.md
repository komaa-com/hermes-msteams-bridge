---
title: "Troubleshooting"
description: "Common problems and fixes: handshake 401s, plugin not loading, silent calls, allowlist rejections."
---

Common problems and how to fix them. `hermes teams-voice status` is your first stop -
it prints the resolved host/port/path and whether a shared secret is configured.

## The handshake is rejected (HTTP 401)

**Symptom:** StandIn can't connect; the log shows
`upgrade rejected call=… : bad signature` (or `missing auth headers` /
`timestamp outside window` / `replayed handshake`).

**Causes & fixes:**

- **Secret mismatch** (most common) - `shared_secret` in your config does **not**
  equal the value paired in StandIn. They must be byte-for-byte identical. Re-copy
  it from the StandIn sandbox page or dashboard into `TEAMS_VOICE_SHARED_SECRET`.
- **Clock skew** - `timestamp outside window` means the two machines' clocks differ
  by more than ±60 s. Sync time (NTP).
- **No secret at all** - `bridge not configured (no shared secret)`: set
  `TEAMS_VOICE_SHARED_SECRET`. The bridge won't even start without it.

## The plugin isn't loading

**Symptom:** `teams_voice` doesn't appear in `hermes plugins list`, or
`hermes teams-voice …` is an unknown command.

Two independent requirements - **both** must hold:

1. **Installed in the Hermes venv.** The plugin is imported in-process, so it must
   live in the *same* Python environment as Hermes. Verify:
   ```bash
   /path/to/hermes/venv/bin/python -c "import hermes_teams_voice; print('ok')"
   ```
   If that fails, reinstall targeting the Hermes interpreter (see
   [Getting Started](/hermes-msteams-bridge/getting-started/#1-install-into-the-same-venv-as-hermes)).
2. **Listed in `plugins.enabled`.** Entry-point plugins are opt-in. Add
   `teams_voice` to `plugins.enabled` in `~/.hermes/config.yaml`.

:::caution[Pip plugins are enabled in config.yaml only]
**`hermes plugins enable teams_voice` does NOT work for pip-installed plugins** -
it only sees bundled/user-dir plugins. Enable it in `config.yaml` instead.
:::

## No audio / silent call

- **Recording gate** - by default nothing is processed until Teams **recording is
  active**. If the meeting isn't being recorded, the agent stays silent. Start
  recording, or (for testing only) set `require_recording_status: false`.
- **Wrong handler** - `--handler logging` sends **no audio back** by design. Use
  `--handler realtime` (or `echo` to smoke-test with your own voice echoed).
- **Realtime key missing** - `realtime` refuses to start without a key
  (`OPENAI_API_KEY`, or `AZURE_FOUNDRY_API_KEY` / `TEAMS_VOICE_REALTIME_API_KEY` for
  Azure). Check `hermes teams-voice status` and your `.env`.
- **Group gate** - in a meeting (2+ people) the agent only speaks when **addressed**
  by a wake phrase (`assistant`, `hermes`, or your `wake_phrases`). Say its name.

## Streaming mode: `ffmpeg` missing

**Symptom:** `warning: streaming mode needs 'ffmpeg' on PATH to decode TTS audio`,
and the bot doesn't speak.

Install `ffmpeg` and ensure it's on PATH, or switch to `--handler realtime` (which
doesn't need it).

## The recording gate is blocking media

If you *intend* to run without recording (e.g. a lab test), set
`require_recording_status: false` (or `TEAMS_VOICE_REQUIRE_RECORDING_STATUS=false`).
Leave it **on** for production/compliance - it prevents processing any
media-derived data before recording is active.

## Callers are rejected (allowlist)

The allowlist is **deny-by-default**:

- An **empty** `allowlist` **denies all** inbound callers - unless `allow_all: true`.
- Callers are matched by **AAD object id**. Display-name matching is off unless
  `allowlist_allow_names: true` (and it's weaker/spoofable).

To let a specific caller in, add their AAD object id to `allowlist`. To open it up
(e.g. sandbox testing), set `allow_all: true`.

## The call drops itself mid-conversation

- **Realtime provider drop** - if the OpenAI/Azure realtime connection is unreachable
  at connect or drops mid-call, the plugin **tears the Teams call down cleanly**
  (rather than leaving dead air). Check provider status/quotas and the log line
  `realtime connect failed` / `realtime-provider-closed`.
- **Max-duration reaper** - if `max_call_duration_s > 0`, the call is closed once it
  exceeds that wall-clock budget. Raise or zero it (`0` = unlimited) if this is
  premature.

## The call ends with a goodbye after a few minutes

That's the **StandIn cutoff**. The **sandbox** and **free** tiers are daily-capped
(about 5 minutes/day); a **subscription** may have a max-minutes governor. StandIn
sends an `assistant.say` goodbye the agent speaks, then ends the call. For longer
calls, use a subscription tier - see [Connecting to StandIn](/hermes-msteams-bridge/connecting-to-standin/).

## Quick liveness check

The server answers `GET /health` with `ok` on the same host/port:

```bash
curl http://127.0.0.1:8443/health   # -> ok
```

If that fails, the plugin's server isn't running (or is bound to a different host/port) -
`hermes teams-voice status` shows the resolved values.

## Still stuck?

Run with info logging and watch the `[teams_voice]` lines, then open an issue on
[GitHub](https://github.com/komaa-com/hermes-msteams-bridge/issues) with the
handler you used, the log around the failure, and your (secret-free) config. Hosted-
service questions belong at [docs.komaa.com](https://docs.komaa.com).
