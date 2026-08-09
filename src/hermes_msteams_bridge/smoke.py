"""Synthetic StandIn caller + the Phase 0b smoke check.

``SyntheticCall`` speaks the real wire: HMAC-signed WebSocket upgrade,
``session.start`` / ``recording.status`` / 20 ms PCM ``audio.frame`` stream /
``session.end`` — exactly what the StandIn media bridge sends, minus Teams.
Two consumers:

* ``hermes teams-call smoke`` — one call against an EPHEMERAL logging-handler
  service on a free port (no provider cost, no Teams, no funnel): proves the
  whole install end-to-end — config, HMAC, wire decode, session lifecycle.
* the ADR-6 spike harness — N concurrent calls against an echo service while
  an event-loop-lag probe stands in for the chat plane.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time

from . import hmac_auth, protocol
from .config import (
    BYTES_PER_FRAME,
    DEFAULT_PATH,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    TeamsVoiceConfig,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SyntheticCall:
    """One fake StandIn leg. Requires ``aiohttp`` (already a dependency)."""

    def __init__(self, host: str, port: int, secret: str, call_id: str) -> None:
        # Uses the bridge's own configured path, so the smoke tool cannot drift from what the server
        # actually serves - it hardcoded /voice/msteams/stream and only failed once the alias was gone.
        self._url = f"ws://{host}:{port}{DEFAULT_PATH}/{call_id}"
        self._secret = secret
        self._call_id = call_id
        self._ws = None
        self._session = None
        self.echo_frames = 0  # audio frames received back (echo handler)

    async def connect(self) -> None:
        import aiohttp

        ts = int(time.time() * 1000)
        headers = {
            HEADER_TIMESTAMP: str(ts),
            HEADER_SIGNATURE: hmac_auth.sign(self._secret, ts, self._call_id),
        }
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, headers=headers)

    async def start_call(self, caller_name: str = "Smoke Test") -> None:
        await self._ws.send_str(json.dumps({
            "type": "session.start",
            "callId": self._call_id,
            "threadId": f"19:smoke-{self._call_id}@thread.v2",
            "caller": {"aadId": f"smoke-{self._call_id}", "displayName": caller_name},
            "recordingStatus": "active",
            "direction": "inbound",
        }))
        await self._ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))

    async def stream_audio(self, frames: int, frame_interval_s: float = 0.02) -> None:
        """Stream silence frames at the wire cadence, draining any echo."""
        payload = base64.b64encode(b"\x00" * BYTES_PER_FRAME).decode("ascii")
        for seq in range(frames):
            await self._ws.send_str(json.dumps({
                "type": protocol.TYPE_AUDIO_FRAME,
                "seq": seq,
                "timestampMs": seq * 20,
                "payloadBase64": payload,
            }))
            # Drain without blocking the cadence.
            while True:
                try:
                    msg = self._ws._reader._buffer  # peek cheaply
                except AttributeError:
                    msg = None
                try:
                    incoming = await asyncio.wait_for(self._ws.receive(), timeout=0.001)
                except asyncio.TimeoutError:
                    break
                if incoming.type.name == "TEXT":
                    try:
                        if json.loads(incoming.data).get("type") == protocol.TYPE_AUDIO_FRAME:
                            self.echo_frames += 1
                    except ValueError:
                        pass
                else:
                    return
            await asyncio.sleep(frame_interval_s)

    async def end_call(self) -> None:
        try:
            await self._ws.send_str(json.dumps({"type": "session.end", "reason": "smoke-done"}))
            await self._ws.close()
        finally:
            await self._session.close()

    async def close(self) -> None:
        """Leak-proof teardown, safe in any state — error paths that never
        reached ``end_call`` must still release the client session."""
        try:
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._session is not None and not self._session.closed:
                await self._session.close()
        except Exception:  # noqa: BLE001
            pass


async def run_smoke() -> dict:
    """Phase 0b smoke: probe + ephemeral end-to-end synthetic call.

    Returns a result dict; ``ok`` is the overall verdict. No Teams, no
    provider, no funnel — this validates the install, not the network.
    """
    from .handlers import EchoCallSessionHandler
    from .hermes_api import hermes_version_note, probe_boundaries
    from .service import VoiceBridgeService

    results: dict = {"hermes": hermes_version_note()}
    boundaries = probe_boundaries()
    results["boundaries_ok"] = all(b["ok"] for b in boundaries)
    results["operational_ok"] = all(b.get("operational") is not False for b in boundaries)
    results["boundaries_missing"] = [b["surface"] for b in boundaries if not b["ok"]]

    port = free_port()
    secret = "smoke-secret"
    cfg = TeamsVoiceConfig(shared_secret=secret, host="127.0.0.1", port=port, allow_all=True)
    # background_workers=False: this must stay OFFLINE — the durable-job
    # resume and pending reaper deliver for real (agent runs, Teams sends).
    service = VoiceBridgeService(
        cfg, handler_factory=EchoCallSessionHandler, background_workers=False
    )
    call = SyntheticCall("127.0.0.1", port, secret, "smoke-call-1")
    call_ok = echo_ok = False
    try:
        await service.start()
        await call.connect()
        await call.start_call()
        await call.stream_audio(frames=10)
        echo_ok = call.echo_frames > 0
        await call.end_call()
        call_ok = True
    except Exception as exc:  # noqa: BLE001 — the verdict carries the reason
        results["call_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await call.close()  # error paths must not leak the client session
        await service.stop()

    results["synthetic_call_ok"] = call_ok
    results["echo_audio_ok"] = echo_ok
    results["ok"] = bool(
        results["boundaries_ok"] and call_ok and echo_ok
    )
    return results
