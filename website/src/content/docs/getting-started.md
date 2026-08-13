---
title: "Getting Started"
description: "Install the plugin into Hermes, enable it, connect to StandIn, and place your first Teams voice call."
---

This walks you from nothing to a working Teams voice call with your Hermes agent.

:::note[Two ways to connect]
**StandIn Managed Bot (recommended)** - StandIn provides the Teams bot. Install StandIn from the
Teams Store, connect this agent in the StandIn portal, paste the ONE connection secret it gives you. **No
Azure bot registration, no App ID or client secret, and no separate Teams messaging setup** - this
plugin hosts both the voice and chat lanes. See [Connecting to StandIn](/connecting-to-standin/).

**Bring your own Azure bot (advanced)** - you own the Entra app, client secret and Azure Bot
resource. The prerequisites below apply to this path.
:::

## Prerequisites

- **A working Hermes install.** This is a plugin *on top of* Hermes, not a
  standalone app. Set up Hermes first using the
  [official docs](https://hermes-agent.nousresearch.com/docs). For the
  bring-your-own-bot path you also need
  [Microsoft Teams messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams)
  for the chat plane; with the StandIn Managed Bot you do not.
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
```

Or, with the Hermes venv activated:

```bash
pip install hermes-msteams-bridge
```

Optional faster audio resampling:

```bash
uv pip install --python /path/to/hermes/venv/bin/python "hermes-msteams-bridge[numpy]"
```

## 2. Enable the plugin

Entry-point plugins are **opt-in**. Add `msteams_bridge` to `plugins.enabled` in
`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - msteams_bridge
```

:::caution[Pip plugins are enabled in config.yaml only]
**`hermes plugins enable` does NOT work for pip-installed plugins** - it only sees
bundled/user-dir plugins. You must add `msteams_bridge` to `plugins.enabled` in
`config.yaml` as above.
:::

Confirm Hermes now sees it (`msteams_bridge` should appear in the list):

```bash
hermes plugins list
```

Then check the resolved config + readiness:

```bash
hermes msteams-bridge status
```

## 3. Configure the shared secret + provider

Non-secret settings go in `config.yaml`; secrets go in `~/.hermes/.env` and are
referenced with `${VAR}`.

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - msteams_bridge
  entries:
    msteams_bridge:
      config:
        # ONE connection secret from the StandIn portal, covering calling AND messages.
        # (Bring-your-own-bot deployments set `calling_secret` instead.)
        secret: ${MSTEAMS_BRIDGE_SECRET}
        host: 127.0.0.1                 # both lanes; the tunnel terminates TLS and proxies here
        calling_port: 8443
        messages_port: 8444
        # WITHOUT a caller policy the bridge accepts NOTHING: the allowlist IS the policy and an
        # empty one denies every inbound call, so a setup that looks finished answers nothing.
        allow_all: true                 # or list trusted callers under `allowlist`
        realtime:
          backend: openai            # or azure
          model: gpt-realtime
          voice: alloy
          api_key: ${OPENAI_API_KEY}
platforms:
  msteams_bridge:
    enabled: true   # the gateway HOSTS the bridge. Without this the plugin loads and nothing listens.
```

`~/.hermes/.env`:

```bash
MSTEAMS_BRIDGE_SECRET=<the connection secret from StandIn>
OPENAI_API_KEY=<your-openai-key>
```

:::caution[Two switches, not one]
`plugins.enabled` loads the plugin; `platforms.msteams_bridge.enabled` activates it under
`hermes gateway run`. Setting only the first is the most common way to end up with a bridge that
starts cleanly, reports no error, and never listens on either lane.
:::

The full key list is in the [Configuration Reference](/hermes-msteams-bridge/configuration-reference/).

## 4. Try it on the StandIn sandbox

The fastest way to see it working is the **sandbox** tier - no Teams bot of your own
required:

1. Go to [standin.komaa.com/sandbox](https://standin.komaa.com/sandbox).
2. Generate a Teams meeting link; a shared StandIn bot joins that meeting.
3. Copy the **shared secret** the sandbox gives you into
   `MSTEAMS_BRIDGE_SHARED_SECRET`.

The sandbox is time-limited (about 5 minutes/day per session) - perfect for a first
run. See [Connecting to StandIn](/hermes-msteams-bridge/connecting-to-standin/) for all three tiers.

## 5. Run the plugin

```bash
hermes msteams-bridge serve --handler realtime
```

You should see it bind:

```text
[msteams_bridge] bridge listening host=127.0.0.1 port=8443 path=/msteams/calling/{call_id}
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
