"""StandIn managed chat mode (wire contract: ``protocol/chat-schema.yaml``).

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
    #: Submit payload of an agent card's Action.Submit (additive v1; text is empty on these).
    card_action: dict[str, Any] | None = None


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
    # schemaVersion is a MAJOR version: additive/minor evolution does NOT bump it (the schema says
    # receivers must ignore unknown fields, which is exactly how minors arrive), so an integer above
    # ours means incompatible semantics and is refused rather than misread as v1. A missing or
    # non-integer value is treated as v1 - the field only became mandatory after v1 shipped.
    version = raw.get("schemaVersion", SCHEMA_VERSION)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schemaVersion {version} (this bridge speaks {SCHEMA_VERSION}) - upgrade the bridge"
        )
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
        card_action=raw.get("cardAction") if isinstance(raw.get("cardAction"), dict) else None,
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


async def post_message(
    *,
    chat_secret: str,
    gateway_reply_url: str,
    tenant_id: str,
    conversation_id: str,
    text: str,
    idempotency_key: str | None = None,
) -> bool:
    """Post a message into a Teams conversation through the gateway, with no inbound message to reply to.

    This is the SAME hop an ordinary chat reply takes - the gateway's /api/chat/reply, signed with this
    binding's connection secret - just addressed explicitly instead of derived from something that arrived.
    The agent never holds a Bot Framework credential; the gateway performs the Teams send.

    It exists because in-call chat had no route at all on a managed connection. The minutes/summary path
    posts through the HOST's Teams platform, which needs the customer's own bot credentials - a managed
    customer has none, so "post this to the chat" during a call could only fail. The plugin already holds
    both sockets; this uses the one built for exactly this direction.

    Best-effort: returns False rather than raising, because a failed post must never break a live call.
    """
    import aiohttp

    body = json.dumps(
        {
            # REQUIRED by chat-schema.yaml, and build_reply has always sent it - this path did not, so
            # in-call and proactive posts were the one shape on the wire missing the version field the
            # gateway uses to decide how to read them.
            "schemaVersion": SCHEMA_VERSION,
            "tenantId": tenant_id,
            "conversationId": conversation_id,
            "text": text,
            "kind": "message",
            **({"idempotencyKey": idempotency_key} if idempotency_key else {}),
        },
        separators=(",", ":"),
    )
    ts, sig = sign(chat_secret, body)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                gateway_reply_url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    HEADER_TIMESTAMP: ts,
                    HEADER_SIGNATURE: sig,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status < 400:
                    return True
                logger.warning("managed chat: in-call post -> HTTP %s", resp.status)
                return False
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a failed post must not break the call
        logger.warning("managed chat: in-call post failed", exc_info=True)
        return False


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
        # Per-conversation processing chains: the schema promises per-conversation
        # ORDERING, and independent tasks per message let replies overtake each other. Each
        # conversation's turns run strictly sequentially; different conversations stay concurrent.
        self._chains: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Bind the chat lane. TRANSACTIONAL: either the server is listening when this returns, or
        nothing of it survives. The previous version created the ClientSession first and let a bind
        failure (port taken, bad host) propagate with the session still open and the AppRunner
        half-set-up - one leaked connector per failed start, and callers that had already started
        OTHER services never got to unwind them."""
        import aiohttp
        from aiohttp import web

        session = aiohttp.ClientSession()
        runner = None
        try:
            app = web.Application(client_max_size=1024 * 1024)
            app.router.add_post(self._config.path, self._handle)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self._config.host, self._config.port)
            await site.start()
        except BaseException:
            # Unwind in reverse. Cleanup itself must not mask the original error.
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:  # noqa: BLE001
                    logger.debug("managed chat: runner cleanup failed during startup unwind", exc_info=True)
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                logger.debug("managed chat: session close failed during startup unwind", exc_info=True)
            raise

        self._session = session
        self._runner = runner
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

        # ACK first; the gateway's durable relay owns retry between US and IT. A redelivered
        # activity ACKs and does nothing - the first delivery's turn is running (or queued).
        if self._seen.mark_first(f"{message.tenant_id}:{message.conversation_id}:{message.activity_id}"):
            self._enqueue_turn(message)
        return web.json_response({"ok": True})

    def _enqueue_turn(self, message: InboundChat) -> None:
        # Chain the turn behind the conversation's previous one (ordering).
        key = f"{message.tenant_id}:{message.conversation_id}"
        prev = self._chains.get(key)

        async def _run() -> None:
            if prev is not None:
                try:
                    await prev
                except asyncio.CancelledError:
                    # Distinguish PREV being cancelled from THIS task being cancelled while
                    # parked on prev. Swallowing our own cancellation here would let a queued turn run its
                    # full agent turn (typing + respond + reply) DURING shutdown. asyncio delivers our own
                    # cancellation as CancelledError at this await - re-raise when it is ours.
                    current = asyncio.current_task()
                    # Task.cancelling() is 3.11+ and requires-python allows 3.10 - there,
                    # fall back to re-raising unconditionally (the pre-P2 behavior was to swallow;
                    # over-raising during shutdown is the safe direction).
                    if current is None or not hasattr(current, "cancelling") or current.cancelling() > 0:
                        raise
                except Exception:  # noqa: BLE001 - a failed turn must not dam the chain
                    pass
            await self._process(message)

        task = asyncio.get_running_loop().create_task(_run())
        self._chains[key] = task
        self._tasks.add(task)

        def _cleanup(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if self._chains.get(key) is t:
                del self._chains[key]

        task.add_done_callback(_cleanup)

    #: Serialization means a HUNG turn would wedge its whole conversation forever (every later
    #: message chains behind it). Every turn is bounded; a timed-out turn fails like any error, the user
    #: hears about it, and the chain moves on. Generous - agent turns legitimately run long.
    TURN_TIMEOUT_S = 300.0

    async def _process(self, message: InboundChat) -> None:
        # Typing is a COURTESY, so it must not sit in FRONT of the agent turn: awaiting it delayed every
        # answer by up to the outbound timeout on a slow gateway. It is started here and runs while the
        # agent thinks, then awaited before the reply goes out - which keeps the indicator ahead of the
        # answer (a "typing..." that lands after the reply is worse than a slow one) at no real cost,
        # because the agent turn is always the longer of the two.
        typing = asyncio.create_task(self._post_reply(build_reply(message, "", "typing")))
        try:
            text = await asyncio.wait_for(self._respond(message), timeout=self.TURN_TIMEOUT_S)
            await asyncio.gather(typing, return_exceptions=True)  # ordering: indicator before answer
            if text and text.strip():
                await self._post_reply(build_reply(message, text))
            else:
                # Parity with the OpenClaw A6 fix: after the typing indicator, silence looks exactly
                # like a hang - an empty answer is said out loud as an error-kind reply.
                logger.warning("managed chat: agent returned an empty answer")
                await self._post_reply(
                    build_reply(
                        message,
                        "I couldn't come up with an answer to that - try rephrasing, or ask something else.",
                        "error",
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the user must hear SOMETHING
            logger.exception("managed chat: agent turn failed")
            await asyncio.gather(typing, return_exceptions=True)  # same ordering on the failure path
            await self._post_reply(
                build_reply(message, "Something went wrong answering that - please try again.", "error")
            )

    #: The reply leg retries a bounded number of times - the idempotencyKey
    #: (activityId:kind) makes a duplicate arrival a silent gateway-side drop, so retrying is safe,
    #: and without it one transient gateway blip ate a finished agent turn.
    REPLY_ATTEMPTS = 3

    async def _post_reply(self, reply: dict[str, Any]) -> None:
        import aiohttp

        body = json.dumps(reply, separators=(",", ":"))
        # Typing indicators never retry (ephemeral by nature).
        attempts = 1 if reply.get("kind") == "typing" else self.REPLY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            # Fresh signature per attempt: a retry after backoff must not replay a stale timestamp
            # into the gateway's +/-5min window edge.
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
                    # The 300s turn budget covers the AGENT, not delivery. Without this the reply POST
                    # inherits aiohttp's very generous default and can hold the conversation's ordered
                    # chain for minutes PER ATTEMPT - three retries of a black-holed connection would
                    # wedge that conversation far longer than the turn it was answering.
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        return
                    logger.warning(
                        "managed chat: gateway reply -> HTTP %s (attempt %d/%d)",
                        resp.status, attempt, attempts,
                    )
                    # 5xx/429 are retryable; other 4xx is OUR bug - the same bytes cannot succeed.
                    if resp.status < 500 and resp.status != 429:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reply delivery is best-effort per attempt
                logger.warning(
                    "managed chat: gateway reply failed (attempt %d/%d)", attempt, attempts,
                    exc_info=True,
                )
            if attempt < attempts:
                await asyncio.sleep(1.0 * 4 ** (attempt - 1))

# ── shared startup (both hosting paths) ────────────────────────────────────────────────────────────

async def start_managed_chat_if_configured(cfg, respond) -> "ManagedChatServer | None":
    """Start the StandIn Managed Bot chat lane when a secret is configured; return None otherwise.

    Lives here, not in a caller, because there are TWO hosting paths and they must behave
    identically: ``hermes teams-call serve`` (standalone) and the gateway-resident platform adapter.
    The lane originally started only in the CLI path, so anyone running the gateway-hosted platform -
    the default once ``platforms.teams_call.enabled`` is set - got voice with silently no chat.
    """
    if not cfg.managed_chat_secret:
        return None
    server = ManagedChatServer(
        ManagedChatConfig(
            chat_secret=cfg.managed_chat_secret,
            gateway_reply_url=cfg.managed_chat_gateway_reply_url,
            host=cfg.managed_chat_host,
            port=cfg.managed_chat_port,
            path=cfg.managed_chat_path,
        ),
        respond,
    )
    await server.start()
    return server
