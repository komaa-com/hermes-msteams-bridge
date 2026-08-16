# Microsoft Teams Bridge for Hermes Agent

[![CI](https://github.com/komaa-com/hermes-msteams-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/komaa-com/hermes-msteams-bridge/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/hermes-msteams-bridge.svg)](https://pypi.org/project/hermes-msteams-bridge/)
[![downloads](https://img.shields.io/pypi/dm/hermes-msteams-bridge.svg)](https://pypi.org/project/hermes-msteams-bridge/)
[![Python](https://img.shields.io/pypi/pyversions/hermes-msteams-bridge.svg)](https://pypi.org/project/hermes-msteams-bridge/)
[![docs](https://img.shields.io/badge/docs-komaa--com.github.io-1f8acb.svg)](https://komaa-com.github.io/hermes-msteams-bridge/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

Microsoft Teams **voice/video (Conversational Video Interface)** for **Hermes Agent**,
packaged as a standalone, pip-installable plugin: install it *on top of* a normal
Hermes install, no fork required.

The plugin (name **`msteams_bridge`**) hosts the HMAC-authenticated WebSocket bridge that
the hosted **StandIn** media bridge dials into, and drives the call: realtime (OpenAI/Azure
speech-to-speech) **or** streaming (STT→agent→TTS), camera/screen vision, the avatar
driver cues (expression / visemes / show-to-caller), group-call etiquette, DTMF,
bilingual EN/AR, and meeting recap/minutes (posted to the chat, with a local
`.docx` artifact).


### Two ways to connect to Teams

**StandIn Managed Bot (recommended).** StandIn provides the Teams bot: install **StandIn** from the
Teams Store, connect this agent in the StandIn portal, and paste one secret. No Azure bot
registration, no App ID or client secret, no separate chat plane to run. Voice and chat are two lanes of
the SAME StandIn connection - a WebSocket on the calling port and HTTP on the messages port - hosted
by one process, whether that is `msteams-bridge serve` or the gateway-resident platform.

```yaml
plugins:
  entries:
    msteams_bridge:
      config:
        # ONE connection secret from the StandIn portal - covers calls AND chat.
        secret: ${MSTEAMS_BRIDGE_SECRET}
```

The chat listener defaults to `127.0.0.1:9444` (the same host as the voice lane) and the StandIn
gateway must reach it through the same tunnel or reverse proxy. If you instead bind it to a
private-network interface (Tailscale, VPN), firewall the port - the HMAC keeps unauthenticated
callers out, but an open port is still an open port.

One agent instance serves **one** StandIn connection: the secret is a single value scoped to one
tenant binding. Serving several tenants means several instances, each with its own secret. Never
share one secret across tenants.

Teams **voice messages** on that chat lane can be transcribed into the agent's turn, so "listen to
this and tell me what you think" is a question it can answer instead of a filename it can only read
back. It is off by default because every clip is a paid STT call and a voice note can run for
minutes - turn it on with `transcribe_voice_messages: true` (or
`MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES=1`), and it uses whichever `stt.provider` Hermes is already
configured with.

**Bring your own Azure bot (advanced).** You own the Entra app, client secret and Azure Bot resource,
and the Teams *chat* plane is handled by Hermes's own `platforms/teams` adapter rather than here.
Choose this when the bot must live entirely inside your tenant.

## Getting started

This plugin adds **voice and video (CVI)** on top of Hermes Agent's Microsoft Teams
**messaging**. Set those up first:

1. **Install Hermes Agent** using the official docs at
   [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com/docs).
2. **Set up Microsoft Teams messaging** in Hermes (bot app + credentials):
   [Teams messaging docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams).
3. **Subscribe to StandIn** ([standin.komaa.com](https://standin.komaa.com), free tier), the hosted
   media bridge that joins the Teams call and connects to this plugin.
4. **Add this plugin.** The one-line installer detects your Hermes venv and walks you through the
   config (mode, shared secret, provider key):

   ```bash
   curl -fsSL https://standin.komaa.com/install.sh | bash
   ```

   Prefer to do it by hand? See [Install on Hermes](#install-on-hermes) and [Configure](#configure).

## Install on Hermes

Install into the **same Python environment as Hermes**: it discovers the plugin via
the `hermes_agent.plugins` entry-point and imports it in-process.

First locate the Hermes venv (the installer puts it under `~/.hermes/.../venv`):

```bash
find ~ -path "*/.hermes/*/venv" -type d 2>/dev/null
```

Then install into that venv, targeting its interpreter (Linux/macOS
`<venv>/bin/python`, Windows `<venv>\Scripts\python.exe`), or activate the venv
first and drop `--python`.

**A. from PyPI (recommended):**

```bash
uv pip install --python /path/to/hermes/venv/bin/python hermes-msteams-bridge
```

Or, with the Hermes venv activated:

```bash
pip install hermes-msteams-bridge
```

**B. from GitHub (latest / pre-release):**

```bash
uv pip install --python /path/to/hermes/venv/bin/python \
  "git+https://github.com/komaa-com/hermes-msteams-bridge.git"
```

**C. from a local checkout (development):**

```bash
git clone https://github.com/komaa-com/hermes-msteams-bridge.git
uv pip install --python /path/to/hermes/venv/bin/python -e ./hermes-msteams-bridge
```

> Installing into the wrong environment means Hermes won't see the plugin.
> Faster audio (optional): add the `numpy` extra, e.g. `hermes-msteams-bridge[numpy]`.

## Enable + run

Entry-point plugins are **opt-in**: add `msteams_bridge` to `plugins.enabled` in
**`~/.hermes/config.yaml`** (see [Configure](#configure) below). `hermes plugins enable`
does **not** work for pip-installed plugins (it only sees bundled/user-dir plugins),
so enable it in config:

```yaml
plugins:
  enabled:
    - msteams_bridge
  entries:
    msteams_bridge:
      config:
        secret: ${MSTEAMS_BRIDGE_SECRET}   # the StandIn connection secret - covers calling AND messages
        host: 127.0.0.1                  # both lanes; the tunnel terminates TLS and proxies to loopback
        # WITHOUT a caller policy the bridge accepts NOTHING: the allowlist IS the policy and an empty
        # one denies every inbound call, so a setup that otherwise looks finished answers nothing.
        # Name trusted callers here, or set allow_all: true to take whatever StandIn routes to you.
        allow_all: true
platforms:
  msteams_bridge:
    enabled: true                        # the gateway hosts the bridge; without this nothing listens
```

Then run the bridge (handlers: `realtime` | `streaming` | `echo` | `logging`):

```bash
hermes msteams-bridge serve --handler realtime
```

And, separately, the Teams chat plane + cron:

```bash
hermes gateway run
```

## Configure

Config lives in Hermes's own files (this package ships none). Non-secret settings go
in **`config.yaml`**; secrets go in **`.env`** and are referenced with `${VAR}`.

### StandIn Managed Bot (recommended)

StandIn provides the Teams bot. You install **StandIn** from the Teams Store, connect this agent in
the StandIn portal, and paste **one secret** here. No Azure bot registration, no App ID, no client
secret, no endpoint configuration.

That one secret covers **both lanes**: calls arrive on the calling WebSocket and Teams messages on
the messages endpoint. They are two lanes of a single StandIn binding, which is why there is one
value to paste and no enable flag to remember.

This is the whole configuration - a working install, with the tuning knobs left out. Every other
setting has a default that is already correct.

**`~/.hermes/config.yaml`**:

```yaml
plugins:
  enabled:
    - msteams_bridge          # entry-point plugins are opt-in; without this it never loads
  entries:
    msteams_bridge:
      config:
        # The connection secret from the StandIn portal. ONE value, BOTH lanes: calling
        # (ws://host:9442/msteams/calling) and messages (http://host:9444/msteams/messages).
        # A per-lane `chat_secret` is accepted as an override; you do not need one.
        secret: ${MSTEAMS_BRIDGE_SECRET}

        # 127.0.0.1 on purpose: your tunnel (Tailscale Funnel, ngrok, a reverse proxy) terminates
        # TLS publicly and forwards to loopback, so no port is exposed on the LAN.
        host: 127.0.0.1
        calling_port: 9442
        messages_port: 9444
        gateway_reply_endpoint: https://teams.standin.komaa.com/api/chat/reply

        # Accept inbound callers. With this false and an empty allowlist, every caller is denied
        # and the call simply never connects - the most common "it does nothing" first install.
        allow_all: true

        # REQUIRED for voice. Without a realtime block the plugin still starts and still connects,
        # then cannot answer. Azure serves realtime from <resource>.cognitiveservices.azure.com -
        # NOT <resource>.openai.azure.com, which 404s the websocket handshake - on its own
        # api-version. `gpt-realtime` is speech-to-speech, so it is the only deployment needed:
        # no whisper, no tts-1.
        realtime:
          backend: azure
          azure_endpoint: https://<your-resource>.cognitiveservices.azure.com
          azure_deployment: gpt-realtime
          azure_api_version: 2025-04-01-preview
          voice: cedar
          api_key: ${AZURE_FOUNDRY_API_KEY}
```

**`~/.hermes/.env`** - only the two values referenced above:

```bash
MSTEAMS_BRIDGE_SECRET=<paste from the StandIn portal>
AZURE_FOUNDRY_API_KEY=<your Azure OpenAI key>
```

> **Public OpenAI** instead of Azure: `backend: openai`, `model: gpt-realtime`,
> `api_key: ${OPENAI_API_KEY}`, and drop the `azure_*` keys.

**Check it before you call.** Restart after any config change (`hermes gateway restart`), then:

```bash
hermes msteams-bridge status
```

The startup log should show both lanes listening:

```
[msteams_bridge] bridge listening host=127.0.0.1 port=9442 path=/msteams/calling/{call_id}
managed chat: listening on 127.0.0.1:9444/msteams/messages
```

Run one mode or the other, never both: `hermes msteams-bridge serve` (standalone) **or**
`hermes gateway run` with the plugin enabled. Two copies fight over the same port.

### Full key reference

Every option, including the self-hosted Teams bot path:

```yaml
plugins:
  enabled:
    - msteams_bridge                          # entry-point plugins are opt-in
  entries:
    msteams_bridge:
      config:
        secret: ${MSTEAMS_BRIDGE_SECRET}   # MUST match the secret StandIn gave you
        host: 127.0.0.1                    # shared by both lanes
        calling_port: 9442                 # voice WS StandIn dials: ws://host:port/msteams/calling/{callId}
        messages_port: 9444                # managed chat lane: http://host:port/msteams/messages
        # chat_secret: ${MSTEAMS_BRIDGE_CHAT_SECRET}  # optional: a distinct key per lane; defaults to `secret`
        max_call_duration_s: 0             # hard wall-clock cap per call in seconds (0 = unlimited)
        meeting_recap: true                # optional: post minutes at call end
        # share_point_site_id: ${TEAMS_SHAREPOINT_SITE_ID}  # optional: future large-file path (file card itself needs only the bot creds)
        allowlist: []                      # caller AAD object ids (empty = deny all inbound callers)
        allow_all: false                   # explicit opt-in: accept any caller when the allowlist is empty
        allowlist_allow_names: false       # also match the allowlist against display names (weaker; default off)
        session_scope: per-call            # per-call | per-thread | per-aad
        wake_phrases: [assistant, hermes]  # group-call wake phrases (speak only when addressed)
        show_file_root: ""                 # show_file containment root (default <hermes home>/workspace/msteams_bridge_show)
        # Outbound "call me back" (StandIn places the return call over its loopback endpoint):
        worker_base_url: http://127.0.0.1:9440   # loopback endpoint StandIn exposes for place-call
        allow_remote_worker: false         # refuse a non-loopback place-call target unless set
        # Realtime (speech-to-speech) brain - Azure OpenAI Realtime:
        realtime:
          backend: azure                   # azure | openai
          azure_endpoint: https://<your-azure-resource>.cognitiveservices.azure.com
          azure_deployment: gpt-realtime
          azure_api_version: 2025-04-01-preview
          voice: cedar
          api_key: ${AZURE_FOUNDRY_API_KEY}
          vad_threshold: 0.5
          prefix_padding_ms: 300
          silence_duration_ms: 500
          languages: []                  # e.g. [en, fr, de, ar]; empty = auto-detect and mirror
```

> **Public OpenAI** instead of Azure: set `backend: openai`, `model: gpt-realtime`,
> `api_key: ${OPENAI_API_KEY}`, and drop the `azure_*` keys.
> **Streaming** (STT→agent→TTS) instead of realtime: omit the `realtime:` block and run
> `hermes msteams-bridge serve --handler streaming` (needs `ffmpeg` on PATH).

**`~/.hermes/.env`**, the secrets referenced above (plus Teams chat-plane creds if you
also run `hermes gateway run`):

```bash
# Voice bridge
MSTEAMS_BRIDGE_SECRET=<same value you set in StandIn>
AZURE_FOUNDRY_API_KEY=<azure-openai-key>                 # or OPENAI_API_KEY for public OpenAI

# Teams chat plane (platforms/teams) - only if you run the gateway:
TEAMS_CLIENT_ID=<bot-app-id>
TEAMS_CLIENT_SECRET=<bot-app-secret>
TEAMS_TENANT_ID=<azure-ad-tenant-id>
```

`secret` **must match** the secret StandIn gave you or the HMAC
handshake fails. Full key reference (every option, defaults, env vars, streaming
mode, the wire protocol): the
[**Configuration Reference**](https://komaa-com.github.io/hermes-msteams-bridge/configuration-reference/)
and [**Wire Protocol**](https://komaa-com.github.io/hermes-msteams-bridge/wire-protocol/)
docs pages. Contributor architecture notes live in
[`DESIGN.md`](DESIGN.md); the module-level guide is in
[`src/hermes_msteams_bridge/README.md`](src/hermes_msteams_bridge/README.md).

## Upgrade / uninstall

Upgrade:

```bash
uv pip install --upgrade hermes-msteams-bridge
```

Uninstall (it then disappears from `hermes plugins list`):

```bash
uv pip uninstall hermes-msteams-bridge
```

## How it loads

Hermes discovers pip plugins via the `hermes_agent.plugins` entry-point group. This
package exposes:

```toml
[project.entry-points."hermes_agent.plugins"]
msteams_bridge = "hermes_msteams_bridge"
```

Hermes imports `hermes_msteams_bridge` and calls its `register(ctx)`, registering the
`msteams-bridge` CLI, the status tool, and the session hook. Entry-point plugins are
opt-in, so `msteams_bridge` must be in `plugins.enabled` (add it in `config.yaml`;
`hermes plugins enable` does not see pip-installed plugins).

## Requirements

- A working **Hermes Agent** install (the host; not a PyPI package).
- Python ≥ 3.10 and `aiohttp`; `ffmpeg` on PATH for streaming-mode TTS decode.
- **StandIn** ([standin.komaa.com](https://standin.komaa.com)), the hosted media bridge that joins the Teams call and connects to this plugin over the HMAC WebSocket.

## Relationship to the bundled plugin

This is the same code as the original in-tree plugin, repackaged for pip
distribution so you don't have to fork Hermes. Install it on **vanilla** Hermes; don't
also keep a bundled `msteams_bridge` (same name → the entry-point would shadow it).

- **Voice/CVI** works fully on vanilla Hermes.
- **Meeting minutes** post to the chat with the Word `.docx` attached as a
  native file card (the same Bot Framework attachment contract the Hermes
  Teams adapter uses; needs the chat plane's `TEAMS_CLIENT_ID`/`SECRET`/
  `TENANT_ID`), degrading to text when creds are absent; a Word-openable
  copy is always kept under the Hermes workspace.

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 Komaa DigiTech. This is an independent plugin; it is
not affiliated with or endorsed by Nous Research. "Hermes" is a project of Nous Research.
Docs at **https://docs.komaa.com/**
