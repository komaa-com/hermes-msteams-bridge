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

The plugin (name **`teams_voice`**) hosts the HMAC-authenticated WebSocket bridge that
the hosted **StandIn** media bridge dials into, and drives the call: realtime (OpenAI/Azure
speech-to-speech) **or** streaming (STT→agent→TTS), camera/screen vision, the avatar
driver cues (expression / visemes / show-to-caller), group-call etiquette, DTMF,
bilingual EN/AR, meeting recap/minutes, and SharePoint (OneDrive) file send.

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

Entry-point plugins are **opt-in**: add `teams_voice` to `plugins.enabled` in
**`~/.hermes/config.yaml`** (see [Configure](#configure) below). `hermes plugins enable`
does **not** work for pip-installed plugins (it only sees bundled/user-dir plugins),
so enable it in config:

```yaml
plugins:
  enabled:
    - teams_voice
```

Then run the voice bridge (handlers: `realtime` | `streaming` | `echo` | `logging`):

```bash
hermes teams-voice serve --handler realtime
```

And, separately, the Teams chat plane + cron:

```bash
hermes gateway run
```

## Configure

Config lives in Hermes's own files (this package ships none). Non-secret settings go
in **`config.yaml`**; secrets go in **`.env`** and are referenced with `${VAR}`.

**`~/.hermes/config.yaml`**, under `plugins.entries.teams_voice.config`:

```yaml
plugins:
  enabled:
    - teams_voice                          # entry-point plugins are opt-in
  entries:
    teams_voice:
      config:
        shared_secret: ${TEAMS_VOICE_SHARED_SECRET}   # MUST match the secret paired in StandIn
        host: 127.0.0.1
        port: 8443                         # voice WS StandIn dials: ws://host:port/voice/msteams/stream
        max_call_duration_s: 0             # hard wall-clock cap per call in seconds (0 = unlimited)
        share_point_site_id: ${TEAMS_SHAREPOINT_SITE_ID}  # optional: attach files/minutes to the chat
        meeting_recap: true                # optional: post minutes at call end
        allowlist: []                      # caller AAD object ids (empty = deny all inbound callers)
        allow_all: false                   # explicit opt-in: accept any caller when the allowlist is empty
        allowlist_allow_names: false       # also match the allowlist against display names (weaker; default off)
        session_scope: per-call            # per-call | per-thread | per-aad
        wake_phrases: [assistant, hermes]  # group-call wake phrases (speak only when addressed)
        bilingual: false                   # pin the realtime model to Arabic/English
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
```

> **Public OpenAI** instead of Azure: set `backend: openai`, `model: gpt-realtime`,
> `api_key: ${OPENAI_API_KEY}`, and drop the `azure_*` keys.
> **Streaming** (STT→agent→TTS) instead of realtime: omit the `realtime:` block and run
> `hermes teams-voice serve --handler streaming` (needs `ffmpeg` on PATH).

**`~/.hermes/.env`**, the secrets referenced above (plus Teams chat-plane creds if you
also run `hermes gateway run`):

```bash
# Voice bridge
TEAMS_VOICE_SHARED_SECRET=<same value you set in StandIn>
AZURE_FOUNDRY_API_KEY=<azure-openai-key>                 # or OPENAI_API_KEY for public OpenAI
TEAMS_SHAREPOINT_SITE_ID=<host>,<siteGuid>,<webGuid>     # optional (needs Graph Sites.ReadWrite.All)

# Teams chat plane (platforms/teams) - only if you run the gateway:
TEAMS_CLIENT_ID=<bot-app-id>
TEAMS_CLIENT_SECRET=<bot-app-secret>
TEAMS_TENANT_ID=<azure-ad-tenant-id>
```

`shared_secret` **must match** the secret paired in StandIn or the HMAC
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
teams_voice = "hermes_msteams_bridge"
```

Hermes imports `hermes_msteams_bridge` and calls its `register(ctx)`, registering the
`teams-voice` CLI, the status tool, and the session hook. Entry-point plugins are
opt-in, so `teams_voice` must be in `plugins.enabled` (add it in `config.yaml`;
`hermes plugins enable` does not see pip-installed plugins).

## Requirements

- A working **Hermes Agent** install (the host; not a PyPI package).
- Python ≥ 3.10 and `aiohttp`; `ffmpeg` on PATH for streaming-mode TTS decode.
- **StandIn** ([standin.komaa.com](https://standin.komaa.com)), the hosted media bridge that joins the Teams call and connects to this plugin over the HMAC WebSocket.

## Relationship to the bundled plugin

This is the same code as the in-tree `plugins/teams_voice` plugin, repackaged for pip
distribution so you don't have to fork Hermes. Install it on **vanilla** Hermes; don't
also keep a bundled `teams_voice` (same name → the entry-point would shadow it).

- **Voice/CVI** works fully on vanilla Hermes.
- **Chat-plane governance + SharePoint file attach** depend on the enhanced
  `plugins/platforms/teams` adapter; without it the plugin **degrades gracefully**
  (e.g. meeting minutes post as text instead of a SharePoint file card).

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 Komaa DigiTech. This is an independent plugin; it is
not affiliated with or endorsed by Nous Research. "Hermes" is a project of Nous Research.
Docs at **https://docs.komaa.com/**
