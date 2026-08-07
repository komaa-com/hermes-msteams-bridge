"""ADR-6 spike, criteria 1-4 (synthetic): the VoiceBridgeService lifecycle
contract that a gateway-resident adapter would wrap. Criteria 5-9 (load,
multi-profile at scale, restart-during-call, chat-latency soak) need live
sessions and stay in the interactive half of the spike."""

from __future__ import annotations

import asyncio
import socket

import pytest

from hermes_msteams_bridge.config import TeamsVoiceConfig
from hermes_msteams_bridge.service import VoiceBridgeService


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(port: int) -> TeamsVoiceConfig:
    return TeamsVoiceConfig(shared_secret="s3cret", host="127.0.0.1", port=port)


def _connectable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def test_criterion_1_ready_only_after_listener_accepts():
    port = _free_port()
    service = VoiceBridgeService(_cfg(port), handler_factory=None)

    async def scenario():
        assert service.ready is False
        await service.start()
        try:
            assert service.ready is True
            assert await asyncio.to_thread(_connectable, port)
        finally:
            await service.stop()
        assert service.ready is False

    asyncio.run(scenario())


def test_criterion_2_second_owner_fails_loudly_first_unaffected():
    port = _free_port()

    async def scenario():
        first = VoiceBridgeService(_cfg(port), handler_factory=None)
        await first.start()
        try:
            second = VoiceBridgeService(_cfg(port), handler_factory=None)
            with pytest.raises(OSError):
                await second.start()
            # the loser must not have disturbed the winner
            assert first.ready and await asyncio.to_thread(_connectable, port)
            await second.stop()  # cleanup of the failed instance is harmless
        finally:
            await first.stop()

    asyncio.run(scenario())


def test_criterion_3_stop_is_idempotent_and_signal_free():
    port = _free_port()
    service = VoiceBridgeService(_cfg(port), handler_factory=None)

    async def scenario():
        await service.start()
        await service.stop()
        await service.stop()  # second stop: no-op, no raise
        assert not await asyncio.to_thread(_connectable, port)  # port released
        with pytest.raises(RuntimeError):
            await service.start()  # single-use: no zombie revival

    asyncio.run(scenario())


def test_criterion_4_all_owned_tasks_cancelled_and_awaited():
    port = _free_port()
    service = VoiceBridgeService(_cfg(port), handler_factory=None)

    async def scenario():
        await service.start()
        owned = list(service._tasks)
        assert owned and all(not t.done() for t in owned)
        await service.stop()
        assert all(t.done() for t in owned)  # cancelled AND awaited
        assert service._tasks == []

    asyncio.run(scenario())
