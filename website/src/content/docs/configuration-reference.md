---
title: "Configuration Reference"
description: "Every config key, environment variable, and default the plugin reads."
---

Every setting the plugin reads, with its `config.yaml` key, environment variable,
default, and meaning. All values match the code in `config.py` and
`realtime/openai_client.py`.

## How resolution works

Values are resolved in **priority order**:

1. The `plugins.entries.msteams_bridge.config` block in **`config.yaml`**.
2. **Environment variables** (typically in `~/.hermes/.env`).
3. Safe **defaults**.

The recommended pattern: keep **secrets in `.env`** and reference them from
`config.yaml` with `${VAR}` (Hermes's loader expands them). The plugin ships no
config of its own. Secrets are never logged.

```yaml
plugins:
  enabled:
    - msteams_bridge
  entries:
    msteams_bridge:
      config:
        # ONE secret from the StandIn portal, covering calling AND messages. `shared_secret` is the
        # per-lane CALLING override, for BYO deployments and split-key setups - not the starting point.
        secret: ${MSTEAMS_BRIDGE_SECRET}
        host: 127.0.0.1
        calling_port: 8443
        messages_port: 8444
        # ...bridge keys below...
        realtime:
          # ...realtime keys below...
```

## StandIn Managed Bot (chat lane)

Set these when StandIn provides the Teams bot (installed from the Teams Store) rather than you
running your own Azure bot. One `secret` covers calls and chat; per-lane `calling_secret`/`messages_secret` override it.
There is no separate enable flag.

| Setting | Env | Default | What it does |
|---|---|---|---|
| `secret` | `MSTEAMS_BRIDGE_SECRET` | - | **The connection secret**, covering BOTH lanes. This is the one value the StandIn portal gives you. |
| `calling_secret` | `MSTEAMS_BRIDGE_CALLING_SECRET` | falls back to `secret` | Per-lane override for CALLING only. `shared_secret` is the older name for it. |
| `messages_secret` | `MSTEAMS_BRIDGE_MESSAGES_SECRET` | falls back to `secret` | Per-lane override for MESSAGES only. Set both per-lane keys for a split-key deployment, where neither key can sign for the other and they rotate independently. |
| `messages_port` | `MSTEAMS_BRIDGE_MANAGED_BOT_PORT` | `8444` | HTTP port the StandIn gateway POSTs inbound messages to. Voice uses `calling_port` (8443). |
| `messages_path` | `MSTEAMS_BRIDGE_MANAGED_BOT_PATH` | `/msteams/messages` | Path the gateway posts to. |
| `gateway_reply_endpoint` | `MSTEAMS_BRIDGE_MANAGED_BOT_GATEWAY_REPLY_URL` | StandIn's `/api/chat/reply` | Where replies are posted back. |
| `host` | `MSTEAMS_BRIDGE_HOST` | `127.0.0.1` | Shared with the voice lane - one machine, one interface. Loopback by default: the documented posture is a tunnel that terminates TLS publicly and proxies to loopback, so no port is exposed on your LAN. Set it to your tailnet/VPN address if the gateway reaches the agent directly. |
| `transcribe_voice_messages` | `MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES` | `false` | Transcribe inbound Teams **voice messages** and fold the words into the agent's turn, so "listen to this and tell me what you think" is a question it can answer instead of a filename it can only read back. **Off by default because it costs money**: every clip is an STT call and a voice note can run for minutes. Uses whatever `stt.provider` you already configured (local Whisper, Groq, OpenAI, Voxtral, Grok, or a custom command). |

One agent instance serves ONE StandIn connection - the chat secret belongs to a single tenant
binding. Run a second instance for a second organization; never share a secret across tenants.

## Bridge settings (`TeamsVoiceConfig`)

| config.yaml key | Env var | Default | Meaning |
|---|---|---|---|
| `shared_secret` | `MSTEAMS_BRIDGE_SHARED_SECRET` | `""` (unset) | HMAC secret shared with StandIn. **Required** - with no secret the bridge won't start. Must equal the value paired in StandIn. |
| `host` | `MSTEAMS_BRIDGE_HOST` | `127.0.0.1` | Bind address for the local WebSocket server. Non-loopback binds are warned about (they expose the secret). |
| `port` | `MSTEAMS_BRIDGE_PORT` | `8443` | Bind port. StandIn dials `ws://host:port/msteams/calling/{callId}`. |
| `path` | *(config only)* | `/msteams/calling` | URL path prefix StandIn connects to. Rarely changed. |
| `hmac_window_ms` | `MSTEAMS_BRIDGE_HMAC_WINDOW_MS` | `60000` | Clock-skew / replay window for the HMAC handshake, in milliseconds (±60 s). |
| `max_call_duration_s` | `MSTEAMS_BRIDGE_MAX_CALL_DURATION_S` | `0.0` | Hard wall-clock cap on a single call, in seconds. `0` = unlimited. A wedged/never-ending call is torn down once exceeded. |
| `require_recording_status` | `MSTEAMS_BRIDGE_REQUIRE_RECORDING_STATUS` | `true` | Gate all media processing until Teams recording is `active`. Recommended on for compliance. |
| `worker_base_url` | `MSTEAMS_BRIDGE_WORKER_BASE_URL` | `http://127.0.0.1:9440` | Loopback HTTP endpoint StandIn exposes for outbound "call me back". See [Outbound Calls](/hermes-msteams-bridge/outbound-calls/). |
| `allow_remote_worker` | `MSTEAMS_BRIDGE_ALLOW_REMOTE_WORKER` | `false` | Permit an outbound place-call to a **non-loopback** `worker_base_url`. Off by default (SSRF guard - the secret would be sent to that host). |
| `tenant_id` | `MSTEAMS_BRIDGE_TENANT_ID` (falls back to `TEAMS_TENANT_ID`) | `""` | Default Azure AD tenant for outbound calls. |
| `allowlist` | `MSTEAMS_BRIDGE_ALLOWLIST` (falls back to `TEAMS_ALLOWED_USERS`) | `()` (empty) | Comma-separated caller **AAD object ids** allowed to call. **Empty = deny ALL inbound callers** unless `allow_all` is set. |
| `allow_all` | `MSTEAMS_BRIDGE_ALLOW_ALL` | `false` | Explicit opt-in to accept any inbound caller when the allowlist is empty. Deny-by-default otherwise. |
| `allowlist_allow_names` | `MSTEAMS_BRIDGE_ALLOWLIST_ALLOW_NAMES` | `false` | Also match the allowlist against caller **display names** (weaker / spoofable). Off by default. |
| `session_scope` | `MSTEAMS_BRIDGE_SESSION_SCOPE` | `per-call` | Agent memory continuity: `per-call` (fresh each call), `per-thread` (keyed by Teams thread), or `per-aad` (keyed by caller AAD id). |
| `wake_phrases` | `MSTEAMS_BRIDGE_WAKE_PHRASES` | `assistant, hermes` | Group-call wake phrases - in a meeting the agent speaks only when addressed by one of these. |
| `meeting_recap` | `MSTEAMS_BRIDGE_MEETING_RECAP` | `false` | Post end-of-call meeting minutes to the Teams chat. |
| `share_point_site_id` | `TEAMS_SHAREPOINT_SITE_ID` | `""` | Optional; reserved for a future large-file SharePoint delivery path. The minutes `.docx` file card itself needs no SharePoint: it rides the Bot Framework attachment contract using the bot credentials (`TEAMS_CLIENT_ID`/`SECRET`/`TENANT_ID`). |
| `max_vision_per_minute` | `MSTEAMS_BRIDGE_MAX_VISION_PER_MINUTE` | `30` | Per-call vision spend cap across `look_at_screen` + ambient push. `0` = unlimited. |

:::note[List values]
`allowlist` and `wake_phrases` accept a YAML list in `config.yaml`
(`allowlist: [id1, id2]`) or a comma-separated string in the env var
(`MSTEAMS_BRIDGE_ALLOWLIST=id1,id2`). Values are lowercased and trimmed.
:::

### Internal defaults (not currently config-driven)

These have sensible fixed defaults and are not exposed as config keys today:

| Field | Default | Meaning |
|---|---|---|
| `max_connections` | `64` | Global concurrent-connection cap (DoS guard). |
| `max_connections_per_ip` | `8` | Per-IP concurrent-connection cap. |
| `pre_start_timeout_s` | `10.0` | A connection that doesn't send `session.start` within this window is reaped. |
| `MAX_CLIP_BYTES` | `16 MiB` | Byte cap per inbound voice message. Larger than an image gets: a voice note is minutes of audio. |
| `MAX_CLIPS_PER_MESSAGE` | `2` | Voice messages transcribed from one inbound message. Deliberately tight - each one is a paid STT call. |
| `FETCH_TIMEOUT_S` | `20.0` | Fetch budget per voice clip. |

Voice-clip fetching is additionally pinned to the origin of `gateway_reply_endpoint`, refuses
redirects, and aborts a body mid-read once it exceeds the cap. These are not tunable: an operator has
no information with which to set an SSRF guard, and every value they could get wrong opens a fetch or
costs money.

## Realtime settings (`RealtimeConfig`)

These live under `plugins.entries.msteams_bridge.config.realtime` (or the matching env
vars) and configure the OpenAI/Azure Realtime speech-to-speech engine. Only used by
`--handler realtime`.

| config.yaml key (under `realtime:`) | Env var | Default | Meaning |
|---|---|---|---|
| `backend` | `MSTEAMS_BRIDGE_REALTIME_BACKEND` | *(auto - see below)* | `openai` or `azure`. |
| `api_key` | `MSTEAMS_BRIDGE_REALTIME_API_KEY` | *(see fallbacks)* | Provider key. OpenAI falls back to `OPENAI_API_KEY`; Azure falls back to `AZURE_OPENAI_API_KEY` then `AZURE_FOUNDRY_API_KEY`. |
| `model` | `MSTEAMS_BRIDGE_REALTIME_MODEL` | `gpt-realtime` | OpenAI realtime model. (On Azure the **deployment** name is used as the model.) |
| `azure_endpoint` | `MSTEAMS_BRIDGE_AZURE_ENDPOINT` | `""` | Azure OpenAI resource endpoint. Setting this auto-selects the Azure backend. |
| `azure_deployment` | `MSTEAMS_BRIDGE_AZURE_DEPLOYMENT` | `""` | Azure realtime deployment name (e.g. `gpt-realtime`). |
| `azure_api_version` | `MSTEAMS_BRIDGE_AZURE_API_VERSION` | `2024-10-01-preview` | Azure realtime API version. |
| `url` | `MSTEAMS_BRIDGE_REALTIME_URL` | `""` | Explicit Realtime WebSocket URL override. An `*.azure.com` URL auto-selects Azure. |
| `voice` | `MSTEAMS_BRIDGE_REALTIME_VOICE` | `alloy` | Realtime voice name (e.g. `cedar`). |
| `instructions` | `MSTEAMS_BRIDGE_REALTIME_INSTRUCTIONS` | *(built-in prompt)* | System prompt for the voice assistant. The default keeps replies brief and delegates real work to the agent. |
| `vad_threshold` | `MSTEAMS_BRIDGE_VAD_THRESHOLD` | `0.5` | Server-VAD activation threshold. |
| `prefix_padding_ms` | `MSTEAMS_BRIDGE_PREFIX_PADDING_MS` | `300` | Audio kept before detected speech start, in ms. |
| `silence_duration_ms` | `MSTEAMS_BRIDGE_SILENCE_DURATION_MS` | `500` | Trailing silence that ends a turn, in ms. |
| `input_transcribe_model` | `MSTEAMS_BRIDGE_INPUT_TRANSCRIBE_MODEL` | `whisper-1` | Model that transcribes caller audio (for wake words / verbal interrupts). Set to `none` / `off` / `disabled` (or empty) to turn off - VAD barge-in still works. |
| `bilingual` | `MSTEAMS_BRIDGE_BILINGUAL` | `false` | Pin the model to detect/mirror the caller's language (Arabic/English) and translate on request. |

### Azure auto-selection

Azure is chosen when **any** of these is true; otherwise OpenAI (bearer auth):

- `backend: azure` (or `MSTEAMS_BRIDGE_REALTIME_BACKEND=azure`), **or**
- an `azure_endpoint` is set, **or**
- the explicit `url` contains `azure.com`.

On Azure, the base URL is built as
`wss://<endpoint>/openai/realtime?api-version=<ver>&deployment=<deployment>` and the
`api-key` header is used instead of bearer auth.

### Example - OpenAI realtime

```yaml
realtime:
  backend: openai
  model: gpt-realtime
  voice: alloy
  api_key: ${OPENAI_API_KEY}
  vad_threshold: 0.5
  prefix_padding_ms: 300
  silence_duration_ms: 500
  bilingual: false
```

### Example - Azure OpenAI realtime

```yaml
realtime:
  backend: azure
  azure_endpoint: https://<your-azure-resource>.cognitiveservices.azure.com
  azure_deployment: gpt-realtime
  azure_api_version: 2025-04-01-preview
  voice: cedar
  api_key: ${AZURE_FOUNDRY_API_KEY}
```

## Env-only example

You can run entirely from environment variables (no `config.yaml` block):

```bash
MSTEAMS_BRIDGE_SHARED_SECRET=...            # must equal the value paired in StandIn
MSTEAMS_BRIDGE_HOST=127.0.0.1
MSTEAMS_BRIDGE_PORT=8443
MSTEAMS_BRIDGE_SESSION_SCOPE=per-thread
MSTEAMS_BRIDGE_WAKE_PHRASES=assistant,hermes
# Realtime (Azure):
MSTEAMS_BRIDGE_REALTIME_BACKEND=azure
MSTEAMS_BRIDGE_AZURE_ENDPOINT=https://<your-azure-resource>.cognitiveservices.azure.com
MSTEAMS_BRIDGE_AZURE_DEPLOYMENT=gpt-realtime
MSTEAMS_BRIDGE_AZURE_API_VERSION=2025-04-01-preview
MSTEAMS_BRIDGE_REALTIME_VOICE=cedar
AZURE_FOUNDRY_API_KEY=...
```

config.yaml wins wherever both a key and its env var are set.
