"""DTMF on the STREAMING path — a keypress must reach the agent.

``RealtimeCallSessionHandler`` has always surfaced keypad input; the streaming brain
defined no ``on_dtmf`` at all, so a ``dtmf`` frame was decoded, dispatched, and then
dropped on the base class's no-op. The caller pressed a key and got nothing back —
total for streaming-mode deployments.

Same recording gate as the realtime path (DTMF is in-band, media-derived caller
input) and the same half-duplex busy guard the audio path uses.
"""

from __future__ import annotations

import asyncio

from hermes_msteams_bridge import handlers, protocol
from hermes_msteams_bridge.config import resolve_config


class FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, recording=True):
        self._ws = FakeWS()
        self.recording_active = recording
        self.human_count = 1
        self.call_id = "call-1"

    async def send_expression(self, e):
        ...

    async def send_audio_frame(self, *a):
        ...

    async def send_speech_marks(self, *a, **k):
        ...


def _handler(recording=True, **extra):
    cfg = resolve_config(extra={"shared_secret": "s", **extra})
    h = handlers.StreamingCallSessionHandler(bridge_config=cfg)
    sess = FakeSession(recording=recording)
    h._session = sess
    asked: list[str] = []
    spoken: list[str] = []

    class FakeConsult:
        async def ask(self, text, **kw):
            asked.append(text)
            return "You picked option one."

    async def fake_speak(text):
        spoken.append(text)

    h._consult = FakeConsult()
    h._speak = fake_speak  # type: ignore[assignment]
    return h, sess, asked, spoken


def _press(h, sess, digit="1"):
    async def run():
        await h.on_dtmf(sess, protocol.Dtmf(type="dtmf", digit=digit))
        if h._utterance_task is not None:
            await h._utterance_task

    asyncio.run(run())


def test_streaming_dtmf_runs_an_agent_turn_and_speaks_the_reply():
    h, sess, asked, spoken = _handler()
    _press(h, sess, "1")
    assert asked == ['The caller pressed the key "1".']
    assert spoken == ["You picked option one."]
    assert h._processing is False  # the turn released the half-duplex lock


def test_streaming_dtmf_is_recorded_in_the_minutes():
    h, sess, _asked, _spoken = _handler()
    h._caller = protocol.CallerInfo(aad_id="a", display_name="Dee Smith")
    _press(h, sess, "#")
    assert ("Dee", 'The caller pressed the key "#".') in h._meeting.turns
    assert ("Assistant", "You picked option one.") in h._meeting.turns


def test_streaming_dtmf_is_recording_gated():
    h, sess, asked, _spoken = _handler(recording=False)
    _press(h, sess, "2")
    assert asked == [] and h._utterance_task is None


def test_streaming_dtmf_runs_when_recording_is_not_required():
    h, sess, asked, _spoken = _handler(recording=False, require_recording_status=False)
    _press(h, sess, "2")
    assert asked == ['The caller pressed the key "2".']


def test_streaming_dtmf_respects_the_half_duplex_busy_guard():
    h, sess, asked, _spoken = _handler()
    h._processing = True  # mid-utterance / mid-playback
    _press(h, sess, "3")
    assert asked == [] and h._utterance_task is None


def test_streaming_dtmf_reaches_the_gateway_agent_when_one_is_wired():
    h, sess, asked, _spoken = _handler()
    delivered: list[str] = []

    async def fake_gateway_turn(_adapter, text):
        delivered.append(text)
        return True

    h._gateway_adapter = object()
    h._emit_gateway_turn = fake_gateway_turn  # type: ignore[assignment]
    _press(h, sess, "0")
    assert delivered == ['The caller pressed the key "0".']
    assert asked == []  # the gateway took the turn; no inline consult


def test_streaming_dtmf_is_dispatched_from_the_wire():
    """The bridge dispatcher must route ``dtmf`` into this handler, not the no-op."""
    from hermes_msteams_bridge.bridge_server import BridgeServer, CallSession

    h, sess, asked, _spoken = _handler()
    server = BridgeServer(config=resolve_config(extra={"shared_secret": "s"}))

    class _WS:
        closed = False

        async def send_str(self, _s):
            ...

        async def close(self, **_kw):
            ...

    wire = CallSession("call-1", _WS())  # type: ignore[arg-type]
    wire.recording_active = True
    h._session = wire

    async def run():
        await server._dispatch(wire, h, protocol.Dtmf(type="dtmf", digit="7"))
        if h._utterance_task is not None:
            await h._utterance_task

    asyncio.run(run())
    assert asked == ['The caller pressed the key "7".']
