"""Recording-status race: an explicit ``recording.status`` must survive ``session.start``.

The worker is free to send ``recording.status`` FIRST. ``session.start`` carries a
setup-time snapshot of the same value — usually absent, which decodes to ``None`` —
so seeding the gate from it after a live status has already arrived flips the media
gate back to closed for the whole call. Every media path is gated on that one flag
(audio, video, DTMF, ambient vision, look_at_screen), so the symptom is a call that
silently refuses to see or hear anything.

These run over a real socket against a real :class:`BridgeServer`, so they fail if the
latch, the guard at the seed, or the duplicate-start refusal is dropped.
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from hermes_msteams_bridge import hmac_auth
from hermes_msteams_bridge.bridge_server import BridgeServer, CallSessionHandler
from hermes_msteams_bridge.config import HEADER_SIGNATURE, HEADER_TIMESTAMP, TeamsVoiceConfig

SECRET = "test-secret"


class GateHandler(CallSessionHandler):
    """Records the recording gate exactly as the media paths read it."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.audio_gate: list[bool] = []

    async def on_session_start(self, session, msg) -> None:
        self.starts.append(msg.call_id)

    async def on_audio_frame(self, session, msg) -> None:
        self.audio_gate.append(session.recording_active)


def _headers(call_id: str, ts: int | None = None) -> dict[str, str]:
    ts = hmac_auth._now_ms() if ts is None else ts
    return {HEADER_TIMESTAMP: str(ts), HEADER_SIGNATURE: hmac_auth.sign(SECRET, ts, call_id)}


def _start_frame(call_id: str, recording_status: str | None = None) -> str:
    body: dict[str, object] = {
        "type": "session.start",
        "callId": call_id,
        "threadId": "t1",
        "caller": {"aadId": "aad-1", "displayName": "Test Caller"},
        "direction": "inbound",
    }
    if recording_status is not None:
        body["recordingStatus"] = recording_status
    return json.dumps(body)


def _recording_frame(status: str) -> str:
    return json.dumps({"type": "recording.status", "status": status})


def _audio_frame() -> str:
    return json.dumps(
        {"type": "audio.frame", "seq": 1, "timestampMs": 20, "payloadBase64": "AAAA"}
    )


async def _serve(handler):
    server = BridgeServer(
        config=TeamsVoiceConfig(shared_secret=SECRET, host="127.0.0.1", port=0),
        handler_factory=lambda: handler,
    )
    await server.start()
    port = server._runner.addresses[0][1]
    return server, f"http://127.0.0.1:{port}{server.config.path}"


async def _wait_for(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def test_explicit_recording_status_survives_a_later_session_start():
    """recording.status active BEFORE session.start — the gate must stay OPEN.

    Without the explicit-status latch, session.start's absent snapshot decodes to
    None, ``None == "active"`` is False, and the gate closes for the whole call.
    """

    async def run():
        h = GateHandler()
        server, url = await _serve(h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c1", headers=_headers("c1"))
                await ws.send_str(_recording_frame("active"))  # worker sends this FIRST
                await ws.send_str(_start_frame("c1"))  # no recordingStatus at all
                await ws.send_str(_audio_frame())
                await _wait_for(lambda: h.audio_gate)
                assert h.audio_gate == [True]
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_explicit_inactive_is_not_reopened_by_a_stale_start_snapshot():
    """The latch protects the value in BOTH directions, not just "active"."""

    async def run():
        h = GateHandler()
        server, url = await _serve(h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c2", headers=_headers("c2"))
                await ws.send_str(_recording_frame("inactive"))
                await ws.send_str(_start_frame("c2", recording_status="active"))
                await ws.send_str(_audio_frame())
                await _wait_for(lambda: h.audio_gate)
                assert h.audio_gate == [False]
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_session_start_still_seeds_the_gate_when_no_status_arrived():
    """The seed itself must stay — the latch only defends an explicit status."""

    async def run():
        h = GateHandler()
        server, url = await _serve(h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c3", headers=_headers("c3"))
                await ws.send_str(_start_frame("c3", recording_status="active"))
                await ws.send_str(_audio_frame())
                await _wait_for(lambda: h.audio_gate)
                assert h.audio_gate == [True]
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())


def test_duplicate_session_start_is_refused():
    """A second session.start must not re-run the seed (nor the handler).

    Re-running it would undo an explicit recording.status and build a second
    (billed) realtime session for a call that already has one.
    """

    async def run():
        h = GateHandler()
        server, url = await _serve(h)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"{url}/c4", headers=_headers("c4"))
                await ws.send_str(_start_frame("c4"))
                await _wait_for(lambda: h.starts)
                await ws.send_str(_recording_frame("active"))
                await ws.send_str(_start_frame("c4"))  # duplicate start
                await ws.send_str(_audio_frame())
                await _wait_for(lambda: h.audio_gate)
                assert h.starts == ["c4"]  # handler ran once
                assert h.audio_gate == [True]  # gate not clobbered
                await ws.close()
        finally:
            await server.stop()

    asyncio.run(run())
