"""StandIn MANAGED chat mode (MANAGED-BOT-TIER.md 4.8, ``protocol/chat-schema.yaml``).

On the managed tier the customer does not own a Teams bot: StandIn's gateway
terminates Bot Framework and speaks the normalized chat protocol to this agent
instead. This module is that endpoint - an aiohttp server accepting
``InboundMessage`` signed with the binding's CHAT key (the bridge HMAC over
``"{timestampMs}.{rawBody}"``, distinct from the voice handshake's
``"{timestampMs}.{callId}"`` payload), ACKing immediately (the gateway's durable
relay owns retry and ordering - agent latency must never sit on its HTTP
response), consulting the Hermes agent async, and POSTing the reply back to the
gateway's ``/api/chat/reply`` signed with the SAME key. The agent never holds a
Bot Framework credential (D5).

The voice WebSocket is unchanged by managed mode; chat is a new lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

#: chat-schema.yaml SCHEMA_VERSION.
SCHEMA_VERSION = 1
#: chat-schema.yaml REPLAY_WINDOW_MS (+/- 5 minutes, both directions).
REPLAY_WINDOW_MS = 300_000

HEADER_TIMESTAMP = "X-StandIn-Timestamp"
HEADER_SIGNATURE = "X-StandIn-Signature"


def compute_signature(secret: str, timestamp: str, body: str) -> str:
    """Lowercase-hex HMAC-SHA256 over ``"{timestamp}.{body}"`` - the bridge HMAC
    (KAT-shared with @standin/bridge-hmac, the gateway, and the media bridge)."""
    payload = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign(secret: str, body: str, now_ms: int | None = None) -> tuple[str, str]:
    """Return ``(timestamp, signature)`` for an outbound reply."""
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    return ts, compute_signature(secret, ts, body)


def verify(
    secret: str,
    timestamp: str | None,
    body: str,
    signature: str | None,
    now_ms: int | None = None,
) -> bool:
    """Constant-time verify inside the replay window. Empty secret fails CLOSED."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now - ts) > REPLAY_WINDOW_MS:
        return False
    expected = compute_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, signature.lower())


@dataclass
class InboundChat:
    """The subset of chat-schema.yaml ``InboundMessage`` this agent consumes
    (subsets are legal; unknown fields are ignored by contract)."""

    tenant_id: str
    conversation_id: str
    activity_id: str
    scope: str
    text: str
    sender_display_name: str | None = None
    sender_is_linked_owner: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    locale: str | None = None


def parse_inbound(body: str) -> InboundChat:
    """Parse + validate an inbound message. Raises ``ValueError`` naming the
    missing routing key - the caller maps that to HTTP 400."""
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed json") from exc
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    for key in ("tenantId", "conversationId", "activityId"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"{key} is required")
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    return InboundChat(
        tenant_id=raw["tenantId"],
        conversation_id=raw["conversationId"],
        activity_id=raw["activityId"],
        scope=raw.get("scope") if isinstance(raw.get("scope"), str) else "personal",
        text=raw.get("text") if isinstance(raw.get("text"), str) else "",
        sender_display_name=sender.get("displayName"),
        sender_is_linked_owner=bool(sender.get("isLinkedOwner", False)),
        attachments=raw.get("attachments") if isinstance(raw.get("attachments"), list) else [],
        locale=raw.get("locale") if isinstance(raw.get("locale"), str) else None,
    )


def build_reply(message: InboundChat, text: str, kind: str = "message") -> dict[str, Any]:
    """The gateway-bound reply. tenantId/conversationId echo the inbound EXACTLY -
    the gateway rejects a mismatch (its cross-tenant guard, the load-bearing check
    of the whole relay)."""
    reply: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "tenantId": message.tenant_id,
        "conversationId": message.conversation_id,
        "replyToId": message.activity_id,
        "kind": kind,
        "idempotencyKey": f"{message.activity_id}:{kind}",
    }
    if kind != "typing":
        reply["text"] = text
    return reply


class SeenActivities:
    """At-least-once dedupe on the schema's activityId idempotency key. Bounded
    LRU: an aged-out redelivery re-running is acceptable at-least-once behavior;
    a fresh double-run is not."""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def mark_first(self, activity_id: str) -> bool:
        if activity_id in self._seen:
            return False
        self._seen[activity_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


def attachments_note(attachments: list[dict[str, Any]]) -> str:
    """Fold by-reference attachments into the consult text so the agent can at
    least NAME what arrived (fetching into the turn is a follow-up)."""
    lines = []
    for a in attachments:
        if a.get("relayable") is False:
            lines.append(f"[attachment not relayed: {a.get('name') or a.get('kind', 'file')}]")
        else:
            lines.append(f"[attachment {a.get('kind', 'file')}: {a.get('name') or 'unnamed'} at {a.get('url')}]")
    return "\n".join(lines)


@dataclass
class ManagedChatConfig:
    """Config for the managed chat lane. Disabled unless the chat secret is set
    (an endpoint with no verifiable key would accept nothing anyway)."""

    chat_secret: str
    gateway_reply_url: str = "https://teams.standin.komaa.com/api/chat/reply"
    host: str = "0.0.0.0"
    port: int = 8444
    path: str = "/managed/chat"

    @property
    def enabled(self) -> bool:
        return bool(self.chat_secret)


class ManagedChatServer:
    """The endpoint + reply client. ``respond`` runs one Hermes agent turn for an
    inbound message and returns the reply text (wired to ``AgentConsult`` by the
    CLI; injectable for tests)."""

    def __init__(
        self,
        config: ManagedChatConfig,
        respond: Callable[[InboundChat], Awaitable[str]],
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._respond = respond
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._seen = SeenActivities()
        self._runner: Any = None
        self._session: Any = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        import aiohttp
        from aiohttp import web

        self._session = aiohttp.ClientSession()
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post(self._config.path, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "managed chat: listening on %s:%s%s", self._config.host, self._config.port, self._config.path
        )

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown must not raise
                pass
        self._tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _handle(self, request: Any):
        from aiohttp import web

        body = (await request.read()).decode("utf-8")
        ts = request.headers.get(HEADER_TIMESTAMP)
        sig = request.headers.get(HEADER_SIGNATURE)
        if not verify(self._config.chat_secret, ts, body, sig, self._now_ms()):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            message = parse_inbound(body)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        # ACK first; the gateway's durable relay owns retry/ordering. A redelivered
        # activity ACKs and does nothing - the first delivery's turn is running.
        if self._seen.mark_first(message.activity_id):
            task = asyncio.get_running_loop().create_task(self._process(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return web.json_response({"ok": True})

    async def _process(self, message: InboundChat) -> None:
        await self._post_reply(build_reply(message, "", "typing"))
        try:
            text = await self._respond(message)
            if text and text.strip():
                await self._post_reply(build_reply(message, text))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the user must hear SOMETHING
            logger.exception("managed chat: agent turn failed")
            await self._post_reply(
                build_reply(message, "Something went wrong answering that - please try again.", "error")
            )

    async def _post_reply(self, reply: dict[str, Any]) -> None:
        body = json.dumps(reply, separators=(",", ":"))
        ts, sig = sign(self._config.chat_secret, body, self._now_ms())
        try:
            async with self._session.post(
                self._config.gateway_reply_url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    HEADER_TIMESTAMP: ts,
                    HEADER_SIGNATURE: sig,
                },
            ) as resp:
                if resp.status >= 400:
                    logger.warning("managed chat: gateway reply -> HTTP %s", resp.status)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reply delivery is best-effort per attempt
            logger.warning("managed chat: gateway reply failed", exc_info=True)
