"""Tests for the realtime teardown fixes: provider-drop closes the Teams call
(on_close callback + handler wiring) and the 'error' event un-mutes the bot."""

from __future__ import annotations

import asyncio
import types

import aiohttp

from hermes_msteams_bridge.handlers import RealtimeCallSessionHandler
from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig, RealtimeSession


class _FakeWS:
    """Minimal async-iterable WebSocket stand-in for the realtime recv loop."""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.closed = False

    def __aiter__(self) -> "_FakeWS":
        self._it = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def _msg(mtype: aiohttp.WSMsgType, data=None):
    return types.SimpleNamespace(type=mtype, data=data)


def _session():
    sent: list[dict] = []
    s = RealtimeSession(RealtimeConfig(api_key="x"))
    s._closed = False

    async def fake_send(obj):
        sent.append(obj)

    s._send = fake_send  # type: ignore[assignment]
    return s, sent


# ── Fix 1: provider drop fires on_close (fault-injected close) ────────────────


def test_recv_loop_fires_on_close_on_provider_close():
    s = RealtimeSession(RealtimeConfig(api_key="x"))
    reasons: list[str] = []

    async def on_close(reason: str) -> None:
        reasons.append(reason)

    s.on_close = on_close
    s._ws = _FakeWS([_msg(aiohttp.WSMsgType.CLOSE)])
    asyncio.run(s._recv_loop())
    assert reasons == ["provider-closed"]  # mid-call drop surfaced to the handler


def test_recv_loop_fires_on_close_on_provider_error():
    s = RealtimeSession(RealtimeConfig(api_key="x"))
    reasons: list[str] = []

    async def on_close(reason: str) -> None:
        reasons.append(reason)

    s.on_close = on_close
    s._ws = _FakeWS([_msg(aiohttp.WSMsgType.ERROR)])
    asyncio.run(s._recv_loop())
    assert reasons == ["provider-error"]


def test_recv_loop_suppresses_on_close_when_we_closed():
    # Our own close() must NOT trigger a teardown callback (the handler is already
    # ending the call); only a provider-side drop should.
    s = RealtimeSession(RealtimeConfig(api_key="x"))
    reasons: list[str] = []

    async def on_close(reason: str) -> None:
        reasons.append(reason)

    s.on_close = on_close
    s._closed = True
    s._ws = _FakeWS([_msg(aiohttp.WSMsgType.CLOSE)])
    asyncio.run(s._recv_loop())
    assert reasons == []


# ── Fix 1: the handler tears the Teams call down on a provider drop ───────────


class _FakeCallWS:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self) -> None:
        self._ws = _FakeCallWS()
        self.call_id = "c1"

    @property
    def closed(self) -> bool:
        return self._ws.closed


def test_handler_closes_teams_call_on_realtime_drop():
    h = RealtimeCallSessionHandler(RealtimeConfig(api_key="x"))
    sess = _FakeSession()
    h._session = sess  # type: ignore[assignment]
    asyncio.run(h._on_realtime_closed("provider-error"))
    assert sess._ws.closed is True  # caller is not left in dead air


def test_handler_close_call_is_noop_when_already_closed():
    h = RealtimeCallSessionHandler(RealtimeConfig(api_key="x"))
    sess = _FakeSession()
    sess._ws.closed = True  # already gone
    h._session = sess  # type: ignore[assignment]
    # Must not raise even though the socket is already closed.
    asyncio.run(h._close_call("late"))


# ── Fix 2: the 'error' event resets _response_active so the bot speaks again ──


def test_error_event_resets_response_active():
    s, _ = _session()
    s._response_active = True
    asyncio.run(s._dispatch({"type": "error", "error": {"message": "active response already"}}))
    assert s._response_active is False


def test_error_then_next_turn_can_speak():
    s, sent = _session()
    s._response_active = True  # a rejected response.create left this latched
    asyncio.run(s._dispatch({"type": "error", "error": "rate_limited"}))
    # The next turn is no longer muted: create_response now actually fires.
    asyncio.run(s.create_response())
    assert sent[-1]["type"] == "response.create"
