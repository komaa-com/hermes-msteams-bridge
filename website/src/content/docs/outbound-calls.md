---
title: "Outbound Calls (call me back)"
description: "How the agent places outbound Teams call-backs to deliver results, and the guards around it."
---

Sometimes the work outlasts the conversation - a long lookup, a background job, or a
result that isn't ready before the caller hangs up. The agent can **place an
outbound Teams call back** to deliver the result when it's ready.

## How it works

1. During a call, the model calls the **`call_me_back`** tool (or kicks off a
   background job with `hermes_agent_task`) with the message to deliver.
2. The plugin asks the **StandIn media bridge** to place an outbound 1:1 Teams call
   to the caller, via a small loopback HTTP endpoint StandIn exposes.
3. When the callee **answers** (recording active), the agent speaks the pending
   result and says goodbye - greet/deliver **on answer**, not while ringing.

The inbound call that requested the callback and the outbound leg that delivers it
are **different WebSocket connections**, so the pending result is correlated by
`callId` in a process-global registry with a TTL (see below).

## Configuration

| Key | Env var | Default | Meaning |
|---|---|---|---|
| `worker_base_url` | `TEAMS_VOICE_WORKER_BASE_URL` | `http://127.0.0.1:9440` | The loopback HTTP endpoint StandIn exposes for place-call. |
| `allow_remote_worker` | `TEAMS_VOICE_ALLOW_REMOTE_WORKER` | `false` | Permit a **non-loopback** `worker_base_url`. Off by default. |
| `tenant_id` | `TEAMS_VOICE_TENANT_ID` (falls back to `TEAMS_TENANT_ID`) | `""` | Default Azure AD tenant for the outbound call. |

## The outbound HMAC scheme

The place-call request is authenticated with the **same shared secret and header
names** as the WebSocket upgrade, but the **signed payload differs** - it signs the
callee's identity, not a `callId`:

```
POST {worker_base_url}/api/calls
X-OpenClawTeamsBridge-Timestamp: {ts}
X-OpenClawTeamsBridge-Signature: HMAC-SHA256(shared_secret, "{ts}.{userObjectId}")   # lowercase hex
Content-Type: application/json

{ "userObjectId": "<callee AAD object id>", "tenantId": "<tenant>" }
```

On success StandIn returns `{"callId": ..., "scenarioId": ...}`.

### SSRF guard

The request carries the HMAC-signed shared secret, so the plugin **refuses a
non-loopback `worker_base_url`** unless `allow_remote_worker` is explicitly set -
otherwise a misconfigured host would receive the signed request. Both
`userObjectId` and `tenantId` are required, and a missing shared secret is rejected
before any request is made.

## Pending-result correlation (TTL)

Because the delivery leg is a separate connection, the plugin stores the pending
spoken result keyed by `callId` in a process-global registry. Entries carry a
**TTL of 600 s (10 minutes)** and are pruned on access, so a never-answered call-back
can't leak its result string indefinitely. When the outbound leg's `session.start`
arrives with `direction: "outbound"`, the plugin pops the pending result and the
agent delivers it on answer.

## Requirements

Outbound "call me back" additionally needs the Microsoft Graph
`Calls.InitiateGroupCall.All` **application** permission on your bot app (admin-
consented). Skip it if you don't use outbound calls. Pairing and tenant setup are in
the StandIn dashboard - [standin.komaa.com](https://standin.komaa.com),
[docs.komaa.com](https://docs.komaa.com).
