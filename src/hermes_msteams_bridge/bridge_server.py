"""Bridge WebSocket server - the seam the StandIn media bridge dials into.

Unlike a typical client/gateway split, here the **StandIn media bridge is the WS client** and
this driver is the **server** (it binds and waits). Each Teams call opens one
connection to ``{path}/{callId}``; the bridge authenticates the upgrade with HMAC
headers, sends ``session.start``, then streams inbound media while this side
streams TTS audio + avatar driver cues back.

This module owns transport concerns only: the HMAC handshake, connection caps,
the pre-start timeout, the read/dispatch loop, and ping/pong. Dialogue/perception
logic lives behind a :class:`CallSessionHandler` so the realtime/streaming brain
can be wired in without touching the wire layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import WSCloseCode, WSMsgType, web

from . import hmac_auth, protocol
from .config import (
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    LEGACY_HEADER_SIGNATURE,
    LEGACY_HEADER_TIMESTAMP,
    TeamsVoiceConfig,
    resolve_config,
)

logger = logging.getLogger(__name__)

_MAX_FRAME_BYTES = 2 * 1024 * 1024  # 2 MB — accommodates a base64 JPEG video.frame
# Outbound backpressure bound: past this much buffered on the worker socket, DROP
# stale audio.frame rather than await the send and head-of-line-block the provider
# recv-loop (which would wedge the whole call). Control frames are never dropped.
_OUTBOUND_BACKPRESSURE_BYTES = 1 * 1024 * 1024  # 1 MB


class CallSession:
    """One live Teams call: the WebSocket plus typed send helpers.

    Inbound frames are delivered to the bound :class:`CallSessionHandler`; the
    handler (and the dialogue brain) drives the call back via the ``send_*``
    methods, which serialize the outbound protocol builders.
    """

    def __init__(self, call_id: str, ws: web.WebSocketResponse) -> None:
        self.call_id = call_id
        self._ws = ws
        self.recording_active = False
        self.human_count = 0
        # Lifecycle flags: ``started`` turns off the pre-start timeout;
        # ``ended`` marks that handler teardown already ran (idempotence guard
        # between an explicit session.end and the abrupt-close fallback).
        self.started = False
        self.ended = False

    @property
    def closed(self) -> bool:
        return self._ws.closed

    def _write_buffer_bytes(self) -> int:
        """Bytes currently buffered on the worker socket's transport, or 0 when it
        cannot be read (a closing socket, or a transport that does not expose it) -
        0 means "do not drop", degrading safely to the prior always-send behavior."""
        writer = getattr(self._ws, "_writer", None)
        transport = getattr(writer, "transport", None)
        if transport is None:
            return 0
        try:
            return transport.get_write_buffer_size()
        except Exception:
            return 0

    async def _send(self, msg: dict, *, droppable: bool = False) -> None:
        if self._ws.closed:
            return
        if droppable and self._write_buffer_bytes() > _OUTBOUND_BACKPRESSURE_BYTES:
            # Shed stale audio instead of head-of-line-blocking the provider stream
            # on a slow/wedged worker socket (matches the TS/LiveKit siblings).
            return
        try:
            await self._ws.send_str(protocol.encode(msg))
        except (ConnectionError, RuntimeError) as exc:
            # A send failure means the call is gone; surface it rather than
            # silently "succeeding" on a dead socket.
            logger.warning("[teams_voice] send failed on %s: %s", self.call_id, exc)
            raise

    async def send_audio_frame(self, seq: int, timestamp_ms: int, payload_base64: str) -> None:
        # Audio is the hot path and latest-wins: droppable under backpressure.
        await self._send(protocol.audio_frame(seq, timestamp_ms, payload_base64), droppable=True)

    async def send_expression(self, emotion: str) -> None:
        await self._send(protocol.expression(emotion))

    async def send_speech_marks(self, marks: list[dict[str, int]], ts: int = 0) -> None:
        await self._send(protocol.speech_marks(marks, ts))

    async def send_display_image(self, data_base64: str, mime: str, **kwargs) -> None:
        await self._send(protocol.display_image(data_base64, mime, **kwargs))

    async def send_assistant_cancel(self, turn_id: int) -> None:
        await self._send(protocol.assistant_cancel(turn_id))


class CallSessionHandler:
    """Interface the dialogue/perception brain implements.

    Every method is async and best-effort: a handler exception is logged and the
    call continues (a bad frame must not tear down the socket). The default
    implementation just logs — wire a real handler via :class:`BridgeServer`.
    """

    async def on_session_start(self, session: CallSession, msg: protocol.SessionStart) -> None:
        logger.info(
            "[teams_voice] session.start call=%s thread=%s dir=%s caller=%s",
            msg.call_id, msg.thread_id, msg.direction, msg.caller.display_name,
        )

    async def on_audio_frame(self, session: CallSession, msg: protocol.AudioFrame) -> None:
        ...  # base no-op; the realtime/streaming handlers route this to the model

    async def on_video_frame(self, session: CallSession, msg: protocol.VideoFrame) -> None:
        ...  # base no-op; the realtime/streaming handlers store + use vision frames

    async def on_recording_status(self, session: CallSession, msg: protocol.RecordingStatus) -> None:
        session.recording_active = msg.status == "active"
        logger.info("[teams_voice] recording.status call=%s = %s", session.call_id, msg.status)

    async def on_participants(self, session: CallSession, msg: protocol.Participants) -> None:
        session.human_count = msg.count

    async def on_dtmf(self, session: CallSession, msg: protocol.Dtmf) -> None:
        ...  # base no-op; the realtime handler surfaces keypresses to the model

    async def on_assistant_say(self, session: CallSession, msg: protocol.AssistantSay) -> None:
        ...  # base no-op; the realtime/streaming handlers speak the text (H4 cutoff goodbye)

    async def on_session_end(self, session: CallSession, msg: protocol.SessionEnd) -> None:
        logger.info("[teams_voice] session.end call=%s reason=%s", session.call_id, msg.reason)


HandlerFactory = Callable[[], CallSessionHandler]


class BridgeServer:
    """Hosts the HMAC-authenticated WebSocket the StandIn media bridge connects to."""

    def __init__(
        self,
        config: Optional[TeamsVoiceConfig] = None,
        handler_factory: Optional[HandlerFactory] = None,
    ) -> None:
        self.config = config or resolve_config()
        self._handler_factory = handler_factory or CallSessionHandler
        self._replay = hmac_auth.ReplayGuard(window_ms=self.config.hmac_window_ms)
        self._runner: Optional[web.AppRunner] = None
        self._conn_count = 0
        self._conn_by_ip: dict[str, int] = {}
        self._live: dict[str, CallSession] = {}  # callId → connected session

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind and start serving. Raises if no shared secret is configured."""
        if not self.config.configured:
            raise RuntimeError(
                "teams_voice bridge has no shared secret "
                "(set TEAMS_VOICE_SHARED_SECRET or config.extra.shared_secret)"
            )
        app = web.Application()
        route = f"{self.config.path.rstrip('/')}/{{call_id}}"
        app.router.add_get(route, self._handle_ws)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await site.start()
        logger.info(
            "[teams_voice] bridge listening host=%s port=%s path=%s",
            self.config.host, self.config.port, route,
        )

    async def stop(self) -> None:
        # Drain live calls first: close each with GOING_AWAY so its read loop ends
        # and per-call teardown runs (releasing the provider realtime session), then
        # let the runner's graceful shutdown wait for those handlers to finish. Without
        # this, a SIGTERM would hard-drop calls and leak (billed) provider sessions.
        for session in list(self._live.values()):
            try:
                await session._ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")
            except Exception:  # noqa: BLE001 - best-effort drain; never block shutdown
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── connection handling ──────────────────────────────────────────────────

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        call_id = request.match_info.get("call_id", "").strip()
        if not call_id:
            return web.Response(status=400, text="missing callId")

        # Cheap connection caps BEFORE the HMAC verify (which records the single-use
        # replay tuple). A cap rejection must be a retryable 503 that does NOT burn
        # the handshake tuple — otherwise the immediate retry would 401 as a replay
        # instead of getting the retryable 503 (BRIDGE-10). Matches the TS siblings'
        # "cheap caps first, before crypto" ordering.
        peer_ip = request.remote or "?"
        if self._conn_count >= self.config.max_connections:
            return web.Response(status=503, text="too many connections")
        if self._conn_by_ip.get(peer_ip, 0) >= self.config.max_connections_per_ip:
            return web.Response(status=503, text="too many connections")

        ok, reason = hmac_auth.verify_upgrade(
            secret=self.config.shared_secret,
            call_id=call_id,
            timestamp_header=request.headers.get(HEADER_TIMESTAMP)
            or request.headers.get(LEGACY_HEADER_TIMESTAMP),
            signature_header=request.headers.get(HEADER_SIGNATURE)
            or request.headers.get(LEGACY_HEADER_SIGNATURE),
            window_ms=self.config.hmac_window_ms,
            replay_guard=self._replay,
        )
        if not ok:
            logger.warning("[teams_voice] upgrade rejected call=%s: %s", call_id, reason)
            return web.Response(status=401, text="unauthorized")

        # Reserve the slots BEFORE the async ws.prepare(): two upgrades that both
        # passed the cap check above would otherwise both prepare and only then
        # increment, transiently exceeding the caps (BRIDGE-8). Rolled back on every
        # early return that does not enter the read loop.
        self._conn_count += 1
        self._conn_by_ip[peer_ip] = self._conn_by_ip.get(peer_ip, 0) + 1
        reserved = True

        def _release() -> None:
            nonlocal reserved
            if not reserved:
                return
            reserved = False
            self._conn_count = max(0, self._conn_count - 1)
            n = self._conn_by_ip.get(peer_ip, 1) - 1
            if n <= 0:
                self._conn_by_ip.pop(peer_ip, None)  # del at 0: no slow map growth (BRIDGE-10)
            else:
                self._conn_by_ip[peer_ip] = n

        try:
            # Server-initiated WS ping so a caller that dies uncleanly (half-open TCP / killed process, no
            # close/FIN) is detected + torn down for the whole call, not just the pre-start / max-duration
            # windows — else its slot + billed worker socket leak forever (max_call_duration_s defaults to 0).
            ws = web.WebSocketResponse(
                max_msg_size=_MAX_FRAME_BYTES,
                heartbeat=self.config.heartbeat_s if self.config.heartbeat_s > 0 else None,
            )
            await ws.prepare(request)

            # Same callId already connected — close the NEW socket to avoid
            # clobbering the live call (mirrors the TS driver's duplicate guard).
            if call_id in self._live:
                logger.warning("[teams_voice] rejected duplicate connection for %s", call_id)
                await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"duplicate-callId")
                return ws

            session = CallSession(call_id, ws)
            self._live[call_id] = session
            handler = self._handler_factory()
            try:
                await self._read_loop(session, handler)
            finally:
                self._live.pop(call_id, None)
                logger.debug("[teams_voice] connection closed %s", call_id)
            return ws
        finally:
            _release()

    async def _read_loop(self, session: CallSession, handler: CallSessionHandler) -> None:
        loop = asyncio.get_event_loop()
        deadline: float | None = None  # max-duration horizon, set once the call starts
        # Absolute pre-start deadline, fixed at connect: a peer past the HMAC upgrade
        # that trickles frames (e.g. a ping every <timeout s) must NOT keep resetting
        # the reap and hold a connection slot forever. The remaining time shrinks toward
        # this instant regardless of inbound frames.
        pre_start_deadline = loop.time() + self.config.pre_start_timeout_s
        try:
            while not session.closed:
                if not session.started:
                    timeout: float | None = pre_start_deadline - loop.time()
                else:
                    # Absolute wall-clock cap on the call (max_call_duration_s > 0):
                    # a wedged call can't run forever and leak a live socket. The
                    # deadline is fixed at session.start, so it fires regardless of
                    # whether frames keep arriving.
                    if deadline is None and self.config.max_call_duration_s > 0:
                        deadline = loop.time() + self.config.max_call_duration_s
                    timeout = (deadline - loop.time()) if deadline is not None else None
                try:
                    msg = await asyncio.wait_for(session._ws.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    if not session.started:
                        logger.warning("[teams_voice] no session.start within %ss; closing %s",
                                       timeout, session.call_id)
                    else:
                        logger.warning(
                            "[teams_voice] call %s exceeded max duration %ss; closing",
                            session.call_id, self.config.max_call_duration_s,
                        )
                    await session._ws.close()
                    return

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    return
                if msg.type is not WSMsgType.TEXT:
                    continue

                try:
                    parsed = protocol.decode(msg.data)
                except protocol.ProtocolError as exc:
                    logger.warning("[teams_voice] bad frame on %s: %s", session.call_id, exc)
                    continue

                await self._dispatch(session, handler, parsed)
        finally:
            # An abrupt socket close (worker crash, network loss, hangup without
            # a session.end frame) must still run the handler teardown for a
            # session that already started — otherwise realtime sockets and
            # ambient tasks leak. ``ended`` keeps this idempotent with an
            # explicit session.end.
            if session.started and not session.ended:
                session.ended = True
                try:
                    await handler.on_session_end(
                        session, protocol.SessionEnd(type=protocol.TYPE_SESSION_END, reason="socket-closed")
                    )
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    logger.error(
                        "[teams_voice] teardown error on abrupt close of %s",
                        session.call_id, exc_info=True,
                    )

    async def _dispatch(
        self,
        session: CallSession,
        handler: CallSessionHandler,
        parsed: protocol.InboundMessage,
    ) -> None:
        """Route one parsed frame to the handler."""
        try:
            if isinstance(parsed, protocol.Ping):
                await session._send(protocol.pong(parsed.ts))
                return
            if isinstance(parsed, protocol.SessionStart):
                # The callId is authenticated via HMAC in the URL path; a
                # session.start body claiming a different callId must be
                # rejected, otherwise the call record and the send/close paths
                # would key off different ids.
                if parsed.call_id != session.call_id:
                    logger.warning(
                        "[teams_voice] session.start callId mismatch (authenticated=%s payload=%s); closing",
                        session.call_id, parsed.call_id,
                    )
                    await session._ws.close()
                    return
                session.started = True
                session.recording_active = parsed.recording_status == "active"
                await handler.on_session_start(session, parsed)
                return
            if isinstance(parsed, protocol.AudioFrame):
                await handler.on_audio_frame(session, parsed)
            elif isinstance(parsed, protocol.VideoFrame):
                await handler.on_video_frame(session, parsed)
            elif isinstance(parsed, protocol.RecordingStatus):
                await handler.on_recording_status(session, parsed)
            elif isinstance(parsed, protocol.Participants):
                await handler.on_participants(session, parsed)
            elif isinstance(parsed, protocol.Dtmf):
                await handler.on_dtmf(session, parsed)
            elif isinstance(parsed, protocol.AssistantSay):
                await handler.on_assistant_say(session, parsed)
            elif isinstance(parsed, protocol.SessionEnd):
                session.ended = True  # explicit end: skip the abrupt-close fallback
                await handler.on_session_end(session, parsed)
                await session._ws.close()
        except Exception:  # noqa: BLE001 — a handler fault must not kill the call
            logger.error(
                "[teams_voice] handler error on %s frame=%s",
                session.call_id, getattr(parsed, "type", "?"), exc_info=True,
            )


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO)
    server = BridgeServer()
    await server.start()
    try:
        await asyncio.Future()  # run until cancelled
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(_amain())
