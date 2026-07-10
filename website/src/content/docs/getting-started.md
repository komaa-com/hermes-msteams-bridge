---
title: "Getting Started"
description: "Install the plugin into Hermes, enable it, connect to StandIn, and place your first Teams voice call."
---

This walks you from nothing to a working Teams voice call with your Hermes agent.

## Prerequisites

- **A working Hermes install.** This is a plugin *on top of* Hermes, not a
  standalone app. Set up Hermes first using the
  [official docs](https://hermes-agent.nousresearch.com/docs), including
  [Microsoft Teams messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams)
  if you want the chat plane too.
- **Python ≥ 3.10** (the same interpreter your Hermes install uses).
- **A realtime provider key** for the realtime engine - an **OpenAI** key or an
  **Azure OpenAI** key with a realtime deployment. (Only needed for
  `--handler realtime`.)
- **`ffmpeg` on PATH** - only for the streaming engine (`--handler streaming`),
  which uses it to decode TTS audio.
- **A StandIn account** - the hosted media bridge that joins the Teams call.
  Start free at [standin.komaa.com](https://standin.komaa.com).

## 1. Install into the *same* venv as Hermes

The plugin is discovered in-process through Hermes's `hermes_agent.plugins`
entry point, so it **must** live in the same Python environment as Hermes.
Installing it anywhere else means Hermes will not see it.

Find the Hermes venv (the installer puts it under `~/.hermes/.../venv`):

```bash
find ~ -path "*/.hermes/*/venv" -type d 2>/dev/null
```

Install into that interpreter:

```bash
uv pip install --python /path/to/hermes/venv/bin/python hermes-msteams-bridge
# or, with the Hermes venv activated:
#   pip install hermes-msteams-bridge
```

Optional faster audio resampling:

```bash
uv pip install --python /path/to/hermes/venv/bin/python "hermes-msteams-bridge[numpy]"
```

## 2. Enable the plugin

Entry-point plugins are **opt-in**. Add `teams_voice` to `plugins.enabled` in
`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - teams_voice
```

:::caution[Pip plugins are enabled in config.yaml only]
**`hermes plugins enable` does NOT work for pip-installed plugins** - it only sees
bundled/user-dir plugins. You must add `teams_voice` to `plugins.enabled` in
`config.yaml` as above.
:::

Confirm Hermes now sees it:

```bash
hermes plugins list        # teams_voice should appear
hermes teams-voice status  # prints resolved config + readiness
```

## 3. Configure the shared secret + provider

Non-secret settings go in `config.yaml`; secrets go in `~/.hermes/.env` and are
referenced with `${VAR}`.

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - teams_voice
  entries:
    teams_voice:
      config:
        shared_secret: ${TEAMS_VOICE_SHARED_SECRET}   # must match StandIn
        host: 127.0.0.1
        port: 8443
        realtime:
          backend: openai            # or azure
          model: gpt-realtime
          voice: alloy
          api_key: ${OPENAI_API_KEY}
```

`~/.hermes/.env`:

```bash
TEAMS_VOICE_SHARED_SECRET=<the value from StandIn>
OPENAI_API_KEY=<your-openai-key>
```

The full key list is in the [Configuration Reference](/hermes-msteams-bridge/configuration-reference/).

## 4. Try it on the StandIn sandbox

The fastest way to see it working is the **sandbox** tier - no Teams bot of your own
required:

1. Go to [standin.komaa.com/sandbox](https://standin.komaa.com/sandbox).
2. Generate a Teams meeting link; a shared StandIn bot joins that meeting.
3. Copy the **shared secret** the sandbox gives you into
   `TEAMS_VOICE_SHARED_SECRET`.

The sandbox is time-limited (about 5 minutes/day per session) - perfect for a first
run. See [Connecting to StandIn](/hermes-msteams-bridge/connecting-to-standin/) for all three tiers.

## 5. Run the plugin

```bash
hermes teams-voice serve --handler realtime
```

You should see it bind:

```text
[teams_voice] bridge listening host=127.0.0.1 port=8443 path=/voice/msteams/stream/{call_id}
```

Other handlers: `--handler streaming` (STT→agent→TTS, needs `ffmpeg`),
`--handler echo` (smoke test - echoes your audio), `--handler logging` (default -
logs frames, no audio back).

## 6. Place your first call

Join the Teams meeting (sandbox link, or a meeting your paired bot is invited to).
Once **recording is active**, the agent greets you on answer and you can start
talking. Try:

- "What time is it in Tokyo?" - the agent consults and speaks the answer.
- Share your screen and ask "What am I looking at?" - vision in action.
- "Call me back in a minute with the summary." - an outbound call-back.

That's it - you have a Hermes agent on a live Teams call. Next: read
[Features](/hermes-msteams-bridge/features/) for everything it can do, or
[Troubleshooting](/hermes-msteams-bridge/troubleshooting/) if something didn't connect.
