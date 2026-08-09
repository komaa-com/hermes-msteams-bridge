---
title: "Configuration Reference"
description: "Every config key, environment variable, and default the plugin reads."
---

Every setting the plugin reads, with its `config.yaml` key, environment variable,
default, and meaning. All values match the code in `config.py` and
`realtime/openai_client.py`.

## How resolution works

Values are resolved in **priority order**:

1. The `plugins.entries.teams_call.config` block in **`config.yaml`**.
2. **Environment variables** (typically in `~/.hermes/.env`).
3. Safe **defaults**.

The recommended pattern: keep **secrets in `.env`** and reference them from
`config.yaml` with `${VAR}` (Hermes's loader expands them). The plugin ships no
config of its own. Secrets are never logged.

```yaml
plugins:
  enabled:
    - teams_call
  entries:
    teams_call:
      config:
        shared_secret: ${TEAMS_CALL_SHARED_SECRET}
        host: 127.0.0.1
        port: 8443
        # ...bridge keys below...
        realtime:
          # ...realtime keys below...
```

## StandIn Managed Bot (chat lane)

Set these when StandIn provides the Teams bot (installed from the Teams Store) rather than you
running your own Azure bot. The chat lane is off until `chat_secret` is set - there is no separate
enable flag.

| Setting | Env | Default | What it does |
|---|---|---|---|
| `chat_secret` | `TEAMS_CALL_CHAT_SECRET` | - | The connection's CHAT key from the StandIn portal. Separate from `shared_secret` (voice) on purpose: neither key can sign for the other, and they rotate independently. |
| `messages_port` | `TEAMS_CALL_MANAGED_BOT_PORT` | `8444` | HTTP port the StandIn gateway POSTs inbound messages to. Voice uses `calling_port` (8443). |
| `messages_path` | `TEAMS_CALL_MANAGED_BOT_PATH` | `/managed/chat` | Path the gateway posts to. |
| `gateway_reply_endpoint` | `TEAMS_CALL_MANAGED_BOT_GATEWAY_REPLY_URL` | StandIn's `/api/chat/reply` | Where replies are posted back. |
| `host` | `TEAMS_CALL_HOST` | `0.0.0.0` | Shared with the voice lane. Defaults to all interfaces because the gateway must reach it; set it to your tailnet/VPN address (or firewall the port) if you reach the agent privately. |

One agent instance serves ONE StandIn connection - the chat secret belongs to a single tenant
binding. Run a second instance for a second organization; never share a secret across tenants.

## Bridge settings (`TeamsVoiceConfig`)

| config.yaml key | Env var | Default | Meaning |
|---|---|---|---|
| `shared_secret` | `TEAMS_CALL_SHARED_SECRET` | `""` (unset) | HMAC secret shared with StandIn. **Required** - with no secret the bridge won't start. Must equal the value paired in StandIn. |
| `host` | `TEAMS_CALL_HOST` | `127.0.0.1` | Bind address for the local WebSocket server. Non-loopback binds are warned about (they expose the secret). |
| `port` | `TEAMS_CALL_PORT` | `8443` | Bind port. StandIn dials `ws://host:port/voice/msteams/stream/{callId}`. |
| `path` | *(config only)* | `/voice/msteams/stream` | URL path prefix StandIn connects to. Rarely changed. |
| `hmac_window_ms` | `TEAMS_CALL_HMAC_WINDOW_MS` | `60000` | Clock-skew / replay window for the HMAC handshake, in milliseconds (±60 s). |
| `max_call_duration_s` | `TEAMS_CALL_MAX_CALL_DURATION_S` | `0.0` | Hard wall-clock cap on a single call, in seconds. `0` = unlimited. A wedged/never-ending call is torn down once exceeded. |
| `require_recording_status` | `TEAMS_CALL_REQUIRE_RECORDING_STATUS` | `true` | Gate all media processing until Teams recording is `active`. Recommended on for compliance. |
| `worker_base_url` | `TEAMS_CALL_WORKER_BASE_URL` | `http://127.0.0.1:9440` | Loopback HTTP endpoint StandIn exposes for outbound "call me back". See [Outbound Calls](/hermes-msteams-bridge/outbound-calls/). |
| `allow_remote_worker` | `TEAMS_CALL_ALLOW_REMOTE_WORKER` | `false` | Permit an outbound place-call to a **non-loopback** `worker_base_url`. Off by default (SSRF guard - the secret would be sent to that host). |
| `tenant_id` | `TEAMS_CALL_TENANT_ID` (falls back to `TEAMS_TENANT_ID`) | `""` | Default Azure AD tenant for outbound calls. |
| `allowlist` | `TEAMS_CALL_ALLOWLIST` (falls back to `TEAMS_ALLOWED_USERS`) | `()` (empty) | Comma-separated caller **AAD object ids** allowed to call. **Empty = deny ALL inbound callers** unless `allow_all` is set. |
| `allow_all` | `TEAMS_CALL_ALLOW_ALL` | `false` | Explicit opt-in to accept any inbound caller when the allowlist is empty. Deny-by-default otherwise. |
| `allowlist_allow_names` | `TEAMS_CALL_ALLOWLIST_ALLOW_NAMES` | `false` | Also match the allowlist against caller **display names** (weaker / spoofable). Off by default. |
| `session_scope` | `TEAMS_CALL_SESSION_SCOPE` | `per-call` | Agent memory continuity: `per-call` (fresh each call), `per-thread` (keyed by Teams thread), or `per-aad` (keyed by caller AAD id). |
| `wake_phrases` | `TEAMS_CALL_WAKE_PHRASES` | `assistant, hermes` | Group-call wake phrases - in a meeting the agent speaks only when addressed by one of these. |
| `meeting_recap` | `TEAMS_CALL_MEETING_RECAP` | `false` | Post end-of-call meeting minutes to the Teams chat. |
| `share_point_site_id` | `TEAMS_SHAREPOINT_SITE_ID` | `""` | Optional; reserved for a future large-file SharePoint delivery path. The minutes `.docx` file card itself needs no SharePoint: it rides the Bot Framework attachment contract using the bot credentials (`TEAMS_CLIENT_ID`/`SECRET`/`TENANT_ID`). |
| `max_vision_per_minute` | `TEAMS_CALL_MAX_VISION_PER_MINUTE` | `30` | Per-call vision spend cap across `look_at_screen` + ambient push. `0` = unlimited. |

:::note[List values]
`allowlist` and `wake_phrases` accept a YAML list in `config.yaml`
(`allowlist: [id1, id2]`) or a comma-separated string in the env var
(`TEAMS_CALL_ALLOWLIST=id1,id2`). Values are lowercased and trimmed.
:::

### Internal defaults (not currently config-driven)

These have sensible fixed defaults and are not exposed as config keys today:

| Field | Default | Meaning |
|---|---|---|
| `max_connections` | `64` | Global concurrent-connection cap (DoS guard). |
| `max_connections_per_ip` | `8` | Per-IP concurrent-connection cap. |
| `pre_start_timeout_s` | `10.0` | A connection that doesn't send `session.start` within this window is reaped. |

## Realtime settings (`RealtimeConfig`)

These live under `plugins.entries.teams_call.config.realtime` (or the matching env
vars) and configure the OpenAI/Azure Realtime speech-to-speech engine. Only used by
`--handler realtime`.

| config.yaml key (under `realtime:`) | Env var | Default | Meaning |
|---|---|---|---|
| `backend` | `TEAMS_CALL_REALTIME_BACKEND` | *(auto - see below)* | `openai` or `azure`. |
| `api_key` | `TEAMS_CALL_REALTIME_API_KEY` | *(see fallbacks)* | Provider key. OpenAI falls back to `OPENAI_API_KEY`; Azure falls back to `AZURE_OPENAI_API_KEY` then `AZURE_FOUNDRY_API_KEY`. |
| `model` | `TEAMS_CALL_REALTIME_MODEL` | `gpt-realtime` | OpenAI realtime model. (On Azure the **deployment** name is used as the model.) |
| `azure_endpoint` | `TEAMS_CALL_AZURE_ENDPOINT` | `""` | Azure OpenAI resource endpoint. Setting this auto-selects the Azure backend. |
| `azure_deployment` | `TEAMS_CALL_AZURE_DEPLOYMENT` | `""` | Azure realtime deployment name (e.g. `gpt-realtime`). |
| `azure_api_version` | `TEAMS_CALL_AZURE_API_VERSION` | `2024-10-01-preview` | Azure realtime API version. |
| `url` | `TEAMS_CALL_REALTIME_URL` | `""` | Explicit Realtime WebSocket URL override. An `*.azure.com` URL auto-selects Azure. |
| `voice` | `TEAMS_CALL_REALTIME_VOICE` | `alloy` | Realtime voice name (e.g. `cedar`). |
| `instructions` | `TEAMS_CALL_REALTIME_INSTRUCTIONS` | *(built-in prompt)* | System prompt for the voice assistant. The default keeps replies brief and delegates real work to the agent. |
| `vad_threshold` | `TEAMS_CALL_VAD_THRESHOLD` | `0.5` | Server-VAD activation threshold. |
| `prefix_padding_ms` | `TEAMS_CALL_PREFIX_PADDING_MS` | `300` | Audio kept before detected speech start, in ms. |
| `silence_duration_ms` | `TEAMS_CALL_SILENCE_DURATION_MS` | `500` | Trailing silence that ends a turn, in ms. |
| `input_transcribe_model` | `TEAMS_CALL_INPUT_TRANSCRIBE_MODEL` | `whisper-1` | Model that transcribes caller audio (for wake words / verbal interrupts). Set to `none` / `off` / `disabled` (or empty) to turn off - VAD barge-in still works. |
| `bilingual` | `TEAMS_CALL_BILINGUAL` | `false` | Pin the model to detect/mirror the caller's language (Arabic/English) and translate on request. |

### Azure auto-selection

Azure is chosen when **any** of these is true; otherwise OpenAI (bearer auth):

- `backend: azure` (or `TEAMS_CALL_REALTIME_BACKEND=azure`), **or**
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
TEAMS_CALL_SHARED_SECRET=...            # must equal the value paired in StandIn
TEAMS_CALL_HOST=127.0.0.1
TEAMS_CALL_PORT=8443
TEAMS_CALL_SESSION_SCOPE=per-thread
TEAMS_CALL_WAKE_PHRASES=assistant,hermes
# Realtime (Azure):
TEAMS_CALL_REALTIME_BACKEND=azure
TEAMS_CALL_AZURE_ENDPOINT=https://<your-azure-resource>.cognitiveservices.azure.com
TEAMS_CALL_AZURE_DEPLOYMENT=gpt-realtime
TEAMS_CALL_AZURE_API_VERSION=2025-04-01-preview
TEAMS_CALL_REALTIME_VOICE=cedar
AZURE_FOUNDRY_API_KEY=...
```

config.yaml wins wherever both a key and its env var are set.
