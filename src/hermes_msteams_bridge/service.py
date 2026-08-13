"""VoiceBridgeService — the bridge's lifecycle as a contract (ADR-6 spike).

The gateway-residency spike (backlog criteria 1-4) demands lifecycle
properties the old ``serve`` body could not demonstrate: readiness only after
the listener accepts, loud dual-run failure, signal-free idempotent shutdown,
and every owned task cancelled AND awaited. This service encodes those
properties once; ``hermes msteams-bridge serve`` uses it today, and a future
gateway platform adapter would be a thin shell over it
(``connect()`` = ``start()`` + ``_mark_connected()``, ``disconnect()`` =
``stop()``) — which is exactly what makes the spike measurable in tests.
"""

from __future__ import annotations

import asyncio
import logging

from .bridge_server import BridgeServer
from .config import TeamsVoiceConfig

logger = logging.getLogger(__name__)


class VoiceBridgeService:
    """Owns the WS server plus the background workers (no-answer reaper,
    durable-job resume). Signal handling stays with the caller — the service
    itself must work gateway-resident, where it does not own the process."""

    def __init__(
        self, config: TeamsVoiceConfig, handler_factory, *, background_workers: bool = True
    ) -> None:
        self._config = config
        self._server = BridgeServer(config=config, handler_factory=handler_factory)
        self._tasks: list[asyncio.Task] = []
        self._ready = False
        self._stopped = False
        # False = listener only. The smoke check runs offline; the reaper and
        # durable-job resume DELIVER (agent consults, Teams messages), which an
        # install-verification step must never trigger (round 8).
        self._background_workers = background_workers

    @property
    def ready(self) -> bool:
        """True only between a successful :meth:`start` and :meth:`stop` —
        criterion 1: never before the listener is bound and accepting."""
        return self._ready

    async def start(self) -> None:
        """Bind and start accepting; raises loudly on a bind conflict.

        Criterion 2: a second owner (stray ``serve``, second profile) gets the
        ``OSError`` from the port bind — traffic is never silently split.
        """
        if self._ready or self._stopped:
            raise RuntimeError("VoiceBridgeService is single-use: already started or stopped")
        await self._server.start()  # OSError propagates on conflict (loud)

        async def _no_answer_reaper() -> None:
            from .call_session_base import deliver_stale_pending

            while True:
                await asyncio.sleep(60)
                try:
                    await deliver_stale_pending()
                except Exception:  # noqa: BLE001 — the reaper never dies
                    pass

        async def _resume_jobs() -> None:
            from .background_jobs import resume_pending_jobs

            try:
                await resume_pending_jobs()
            except Exception:  # noqa: BLE001
                logger.error("[msteams_bridge] job resume failed", exc_info=True)

        if self._background_workers:
            self._tasks = [
                asyncio.create_task(_no_answer_reaper(), name="msteams_bridge_reaper"),
                asyncio.create_task(_resume_jobs(), name="msteams_bridge_job_resume"),
            ]
        self._ready = True

    async def stop(self) -> None:
        """Idempotent, signal-free shutdown (criterion 3); every owned task is
        cancelled AND awaited (criterion 4) before the server drains."""
        if self._stopped:
            return
        self._stopped = True
        self._ready = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self._server.stop()
