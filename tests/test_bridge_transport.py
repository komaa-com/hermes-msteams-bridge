"""Bridge-level WebSocket transport tests (real aiohttp client against a live
server): duplicate-callId rejection, session.start callId mismatch, abrupt-close
handler teardown, and deny-by-default inbound."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
from aiohttp import WSMsgType

from hermes_msteams_bridge import hmac_auth
from hermes_msteams_bridge.bridge_server import BridgeServer, CallSessionHandler
from hermes_msteams_bridge.config import (
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    LEGACY_HEADER_SIGNATURE,
    LEGACY_HEADER_TIMESTAMP,
    TeamsVoiceConfig,
)
from hermes_msteams_bridge.handlers import StreamingCallSessionHandler

SECRET = "test-secret"


class RecordingHandler(CallSessionHandler):
    """Records lifecycle callbacks so tests can assert teardown ran (once)."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[str] = []

    async def on_session_start(self, session, msg) -> None:
        self.started.append(msg.call_id)

    async def on_session_end(self, session, msg) -> None:
        self.ended.append(msg.reason)


def _config(**overrides) -> TeamsVoiceConfig:
    # port=0 → the OS picks a free port; read it back off the runner.
    return TeamsVoiceConfig(shared_secret=SECRET, host="127.0.0.1", port=0, **overrides)


def _headers(call_id: str, ts: int | None = None) -> dict[str, str]:
    ts = hmac_auth._now_ms() if ts is None else ts
    return {
        HEADER_TIMESTAMP: str(ts),
        HEADER_SIGNATURE: hmac_auth.sign(SECRET, ts, call_id),
    }


def _start_frame(call_id: str) -> str:
    return json.dumps(
        {
            "type": "session.start",
            "callId": call_id,
            "threadId": "t1",
            "caller": {"aadId": "aad-1", "displayName": "Test Caller"},
            "recordingStatus": "inactive",
            "direction": "inbound",
        }
    )


async def _serve(handler_factory, config: TeamsVoiceConfig | None = None):
    server = BridgeServer(config=config or _config(), handler_factory=handler_factory)
    await server.start()
    port = server._runner.addresses[0][1]
    return server, f"http://127.0.0.1:{port}{server.config.path}"


async def _wait_for(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def test_legacy_headers_still_accepted():
    """Pre-rename StandIn deployments send X-OpenClawTeamsBridge-*; they must keep working."""

    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h)
        try:
            async with aiohttp.ClientSession() as client:
                ts = hmac_auth._now_ms()
                headers = {
                    LEGACY_HEADER_TIMESTAMP: str(ts),
                    LEGACY_HEADER_SIGNATURE: hmac_auth.sign(SECRET, ts, "c-legacy"),
                }
                ws = await client.ws_connect(f"{url}/c-legacy", headers=headers)
                await ws.send_str(_start_frame("c-legacy"))
                await _wait_for(lambda: h.started)
                assert h.started
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_duplicate_call_id_rejects_new_socket_keeps_first():
    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h)
        try:
            async with aiohttp.ClientSession() as client:
                ws1 = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws1.send_str(_start_frame("c1"))
                await _wait_for(lambda: h.started)
                # Second socket for the same callId (fresh HMAC timestamp so the
                # replay guard passes): closed with 1008, the first one survives.
                ws2 = await client.ws_connect(
                    f"{url}/c1", headers=_headers("c1", ts=hmac_auth._now_ms() + 1)
                )
                msg = await ws2.receive(timeout=2)
                assert msg.type is WSMsgType.CLOSE
                assert ws2.close_code == aiohttp.WSCloseCode.POLICY_VIOLATION
                # The FIRST socket is still live: ping is still answered.
                await ws1.send_str(json.dumps({"type": "ping", "ts": 5}))
                pong = await ws1.receive(timeout=2)
                assert json.loads(pong.data)["type"] == "pong"
                await ws1.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_cap_rejection_does_not_burn_replay_tuple():
    """BRIDGE-10: a 503 (cap reached) must be retryable — it must NOT consume the
    handshake's single-use replay tuple, or the immediate retry would 401 as a
    replay instead of connecting once a slot frees."""

    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h, _config(max_connections=1))
        try:
            async with aiohttp.ClientSession() as client:
                ws1 = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws1.send_str(_start_frame("c1"))
                await _wait_for(lambda: h.started)
                # Second call while the pool is full → 503, using a fixed tuple we reuse.
                hdrs2 = _headers("c2")
                try:
                    await client.ws_connect(f"{url}/c2", headers=hdrs2)
                    raise AssertionError("expected the capped upgrade to be rejected")
                except aiohttp.WSServerHandshakeError as err:
                    assert err.status == 503
                # Free the slot, then retry c2 with the SAME tuple: it must connect,
                # not 401 as a replay (the 503 above must not have recorded the tuple).
                await ws1.close()
                await _wait_for(lambda: server._conn_count == 0)
                ws2 = await client.ws_connect(f"{url}/c2", headers=hdrs2)
                await ws2.send_str(_start_frame("c2"))
                await _wait_for(lambda: "c2" in h.started)
                assert "c2" in h.started
                await ws2.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_session_start_call_id_mismatch_closes():
    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                # Body claims a different callId than the HMAC-authenticated path.
                await ws.send_str(_start_frame("other-id"))
                msg = await ws.receive(timeout=2)
                assert msg.type is WSMsgType.CLOSE
            assert h.started == []  # handler never saw the mismatched start
            assert h.ended == []  # nothing started, so no teardown either
        finally:
            await server.stop()

    asyncio.run(run())


def test_abrupt_close_runs_handler_teardown_once():
    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_start_frame("c1"))
                await _wait_for(lambda: h.started)
                await ws.close()  # hang up WITHOUT a session.end frame
            await _wait_for(lambda: h.ended)
            assert h.ended == ["socket-closed"]  # teardown ran, exactly once
        finally:
            await server.stop()

    asyncio.run(run())


def test_explicit_session_end_skips_abrupt_close_fallback():
    async def run():
        h = RecordingHandler()
        server, url = await _serve(lambda: h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_start_frame("c1"))
                await ws.send_str(json.dumps({"type": "session.end", "reason": "caller-hangup"}))
                msg = await ws.receive(timeout=2)  # server closes after session.end
                assert msg.type is WSMsgType.CLOSE
            await _wait_for(lambda: h.ended)
            assert h.ended == ["caller-hangup"]  # no second "socket-closed" delivery
        finally:
            await server.stop()

    asyncio.run(run())


def test_max_call_duration_reaps_a_wedged_call():
    async def run():
        h = RecordingHandler()
        cfg = _config(max_call_duration_s=0.3)  # short bound for the test
        server, url = await _serve(lambda: h, config=cfg)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_start_frame("c1"))
                await _wait_for(lambda: h.started)
                # Send no further frames: the call is "wedged". The duration reaper
                # must close it once it exceeds the bound (not wait for a hangup).
                msg = await ws.receive(timeout=2)
                assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
            await _wait_for(lambda: h.ended)
            assert h.ended == ["socket-closed"]  # teardown ran on the reap
        finally:
            await server.stop()

    asyncio.run(run())


def test_inbound_denied_by_default_at_bridge():
    async def run():
        cfg = _config()  # empty allowlist, allow_all off → deny everyone
        server, url = await _serve(lambda: StreamingCallSessionHandler(bridge_config=cfg), config=cfg)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_start_frame("c1"))
                msg = await ws.receive(timeout=2)
                assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        finally:
            await server.stop()

    asyncio.run(run())


def test_inbound_allow_all_opt_in_at_bridge():
    async def run():
        cfg = _config(allow_all=True)
        server, url = await _serve(lambda: StreamingCallSessionHandler(bridge_config=cfg), config=cfg)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_start_frame("c1"))
                await ws.send_str(json.dumps({"type": "ping", "ts": 5}))
                # The call stays up: skip cosmetic frames (expression) until pong.
                for _ in range(5):
                    msg = await ws.receive(timeout=2)
                    assert msg.type is WSMsgType.TEXT
                    if json.loads(msg.data)["type"] == "pong":
                        break
                else:
                    raise AssertionError("no pong received; connection not serving")
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())
