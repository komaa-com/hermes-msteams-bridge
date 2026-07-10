---
title: "Connecting to StandIn"
description: "The connection model, the sandbox / free / subscription tiers, and how the shared secret works."
---

This plugin does not join Teams calls itself. That is the job of **StandIn** (the
"StandIn media bridge") - a hosted service that joins your Teams meeting, handles
the Teams media, and dials into this plugin. This page explains the connection
model, the three tiers, and how the shared secret works.

## The connection model

- **The plugin is a local WebSocket server.** When you run
  `hermes teams-voice serve`, it binds `127.0.0.1:8443` by default and waits.
- **The StandIn media bridge is the client.** For each Teams call it opens **one
  WebSocket** to `/voice/msteams/stream/{callId}` on your server.
- **Authentication is HMAC over a shared secret.** Both sides hold the same secret.
  On the WebSocket upgrade, StandIn sends two headers whose signature is
  `HMAC-SHA256(shared_secret, "{timestampMs}.{callId}")`. The plugin verifies it
  (constant-time, with a ±60 s window and a single-use replay guard) before
  accepting the connection. A mismatch returns `401`.

From the plugin's point of view **the connection is identical across all tiers** -
same local server, same HMAC WebSocket, same protocol. The tier only decides which
StandIn identity and which limits apply.

```
 Teams call ⇄  StandIn media bridge  ──HMAC WS──▶  127.0.0.1:8443  (this plugin)
   (hosted)     dials in with X-OpenClawTeamsBridge-Timestamp / -Signature headers
```

Because the shared secret rides this connection, the plugin binds **loopback** by
default. Binding to a non-loopback host is possible but the plugin warns you,
because it exposes the secret to that interface.

## The three tiers

Pick the tier that matches where you are:

### Sandbox - instant trial

The quickest way to try it. You generate a Teams meeting link and a **shared StandIn
bot** joins it - **no Azure/Teams bot of your own required**. It is time-limited
(about **5 minutes/day per session**). Start at
[standin.komaa.com/sandbox](https://standin.komaa.com/sandbox).

Use it to: confirm your install works and hear your agent on a real call in minutes.

### Free - developer tier

**Bring your own Microsoft Teams bot** (an Azure Bot) and **pair it in the StandIn
dashboard**. Pairing issues the shared secret. The free tier is **daily-capped
(5 minutes/day)** and gets its own slot.

Use it to: develop against your own bot identity and tenant.

### Subscription - production

Your own Teams bot, **no daily cap**, managed in the StandIn dashboard.

Use it to: run the assistant in production for your users.

## Where the shared secret comes from

- **Sandbox:** the sandbox page issues a secret for the session - copy it into
  `TEAMS_VOICE_SHARED_SECRET`.
- **Free / Subscription:** **pairing your bot in the StandIn dashboard issues the
  secret.** Copy it into `TEAMS_VOICE_SHARED_SECRET` (keep it in `~/.hermes/.env`,
  referenced from `config.yaml` as `${TEAMS_VOICE_SHARED_SECRET}`).

The value in your config **must equal** the value StandIn holds, or the HMAC
handshake fails with `401`.

## Pairing your own Teams bot

For the free and subscription tiers you register a Microsoft Teams bot (Azure Bot)
and pair it with StandIn. The bot's Azure AD app needs Microsoft Graph calling
permissions (`Calls.JoinGroupCall.All`, `Calls.AccessMedia.All`, and - for
outbound call-back - `Calls.InitiateGroupCall.All`; plus `Chat.Read.All` /
`ChatMessage.Read.Chat` for chat context and `Sites.ReadWrite.All` for SharePoint
file attach). The exact pairing steps, tenant setup, and dashboard walkthrough live
in the StandIn docs:

- Dashboard & pairing: [standin.komaa.com](https://standin.komaa.com)
- Full hosted-service docs: [docs.komaa.com](https://docs.komaa.com)

## The cutoff goodbye

When a limit is reached - a **sandbox/free daily cap**, or a **subscription
max-minutes governor** - StandIn sends an `assistant.say` frame carrying a short
goodbye line. Your agent **speaks that line in its own voice**, and StandIn then
ends the call gracefully. So the caller hears a clean sign-off rather than a
sudden drop. (You can also set a local hard cap with `max_call_duration_s`; see the
[Configuration Reference](/hermes-plugin-teams-voice/configuration-reference/).)
