"""Ambient vision must not burn budget on a frame it failed to deliver.

The ambient loop consumed a budget slot and latched the frame as "already pushed"
*before* the push was attempted, then swallowed the failure. A failed push therefore
both spent a slot and marked the scene delivered, so the 6 s loop never retried it:
the model silently lost that scene for as long as the screen stayed static, while the
spend it never benefited from starved ``look_at_screen``.

Latch only after a successful send; refund on failure. Same defect, same fix, on the
streaming auto-attach path.
"""

from __future__ import annotations

import asyncio
import time

from hermes_msteams_bridge import handlers, protocol
from hermes_msteams_bridge.config import resolve_config
from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig
from hermes_msteams_bridge.vision_budget import VisionBudget


class CountingBudget(VisionBudget):
    def __init__(self, max_per_minute: int = 8) -> None:
        super().__init__(max_per_minute)
        self.refunds = 0

    def refund(self) -> None:
        self.refunds += 1
        super().refund()

    @property
    def spent(self) -> int:
        return self._used(time.monotonic())


class FakeWS:
    closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self._ws = FakeWS()
        self.recording_active = True
        self.human_count = 2
        self.call_id = "call-1"

    async def send_expression(self, e):
        ...


class FlakyRealtime:
    """send_image raises until ``fail_times`` attempts are used up."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: list[str] = []

    async def send_image(self, image_url, caption=""):
        self.calls.append(caption)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("provider rejected the frame")


def _frame(ts: int = 7) -> protocol.VideoFrame:
    return protocol.VideoFrame(
        type="video.frame", source="screenshare", ts=ts, width=1, height=1,
        mime="image/jpeg", data_base64="ZZ", participant_id="p", participant_name="Sara",
    )


def _realtime(fail_times: int = 0):
    cfg = resolve_config(extra={"shared_secret": "s"})
    h = handlers.RealtimeCallSessionHandler(RealtimeConfig(api_key="x"), bridge_config=cfg)
    sess = FakeSession()
    h._session = sess
    h._rt = FlakyRealtime(fail_times=fail_times)
    h._vision_budget = CountingBudget(8)
    h._ambient_interval_s = 0.01  # the real loop, just faster
    return h, sess


async def _run_ambient_until(h, predicate, timeout_s: float = 2.0) -> None:
    task = asyncio.create_task(h._ambient_vision_loop())
    deadline = time.monotonic() + timeout_s
    try:
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_failed_ambient_push_is_refunded_and_retried():
    h, sess = _realtime(fail_times=1)

    async def run():
        await h.on_video_frame(sess, _frame(ts=7))
        await _run_ambient_until(h, lambda: len(h._rt.calls) >= 2)

    asyncio.run(run())

    assert len(h._rt.calls) == 2, "the failed frame was never retried"
    assert h._vision_budget.refunds == 1  # the failed attempt gave its slot back
    assert h._vision_budget.spent == 1  # only the delivered push is charged
    assert h._ambient_last_ts["screenshare"] == 7  # latched once it actually landed


def test_delivered_ambient_frame_is_latched_and_not_re_pushed():
    h, sess = _realtime(fail_times=0)

    async def run():
        await h.on_video_frame(sess, _frame(ts=7))
        await _run_ambient_until(h, lambda: len(h._rt.calls) >= 1)
        await asyncio.sleep(0.05)  # several more loop ticks

    asyncio.run(run())

    assert h._rt.calls == ["[ambient] New frame from Sara's shared screen."]
    assert h._vision_budget.refunds == 0
    assert h._vision_budget.spent == 1


def test_ambient_push_stops_at_the_budget_and_leaves_the_explicit_reserve():
    """The cap still holds — the refund must not become an unlimited retry loop."""
    h, sess = _realtime(fail_times=0)
    h._vision_budget = CountingBudget(4)  # reserve 2 → 2 ambient pushes available

    async def run():
        for i in range(6):
            await h.on_video_frame(sess, _frame(ts=100 + i))
            await _run_ambient_until(h, lambda i=i: len(h._rt.calls) >= i + 1, timeout_s=0.3)

    asyncio.run(run())

    assert len(h._rt.calls) == 2  # ambient share spent
    assert h._vision_budget.try_consume()  # the explicit reserve survived
    assert h._vision_budget.try_consume()
    assert not h._vision_budget.try_consume()


# ── streaming auto-attach: same defect, same fix ─────────────────────────────


def test_streaming_auto_attach_refunds_and_retries_a_failed_describe(monkeypatch):
    import hermes_msteams_bridge.hermes_api as hermes_api

    h = handlers.StreamingCallSessionHandler(bridge_config=resolve_config(extra={"shared_secret": "s"}))
    h._vision_budget = CountingBudget(8)
    sess = FakeSession()
    h._session = sess
    asyncio.run(h.on_video_frame(sess, _frame(ts=42)))

    calls = {"n": 0}

    async def flaky_vision_ask(instructions, blocks, *, max_tokens=400, purpose=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("vision facade blew up")
        return "a budget spreadsheet"

    monkeypatch.setattr(hermes_api, "vision_ask", flaky_vision_ask)

    assert asyncio.run(h._vision_context()) == ""  # failure degrades quietly
    assert h._vision_budget.refunds == 1
    assert h._vision_budget.spent == 0
    assert h._last_frame_ts is None  # NOT marked as already seen

    ctx = asyncio.run(h._vision_context())  # same frame, retried
    assert "budget spreadsheet" in ctx
    assert h._last_frame_ts == 42
    assert h._vision_budget.spent == 1
    assert asyncio.run(h._vision_context()) == ""  # now latched: no re-describe
