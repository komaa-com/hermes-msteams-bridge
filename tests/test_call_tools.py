"""Tests for CallToolRunner (unit-testable against a fake handler)."""

from __future__ import annotations

import asyncio

from hermes_msteams_bridge.call_tools import CallToolRunner
from hermes_msteams_bridge.meeting import MeetingTranscript
from hermes_msteams_bridge.vision_budget import VisionBudget
from hermes_msteams_bridge.vision_store import VisionStore


class _FakeConsult:
    async def ask(self, query, *, timeout_s=45.0):
        return f"CONSULT:{query}"


class _FakeHandler:
    """Minimal handler exposing the attrs CallToolRunner reads."""

    def __init__(self, *, thread_id="", vision_budget=None, vision=None):
        self._bridge = None
        self._session = None
        self._caller = None
        self._consult = _FakeConsult()
        self._vision = vision or VisionStore()
        self._vision_budget = vision_budget or VisionBudget(0)  # 0 = unlimited
        self._meeting = MeetingTranscript()
        self._thread_id = thread_id


def test_run_tool_consult_delegates():
    r = CallToolRunner(_FakeHandler())
    assert asyncio.run(r.run_tool("hermes_agent_consult", {"query": "hi"})) == "CONSULT:hi"


def test_run_tool_unknown():
    r = CallToolRunner(_FakeHandler())
    assert "Unknown tool" in asyncio.run(r.run_tool("nope", {}))


def test_look_at_screen_no_frame():
    r = CallToolRunner(_FakeHandler())  # empty vision store
    out = asyncio.run(r.run_tool("look_at_screen", {"question": "what's there"}))
    assert "can't see" in out.lower()


def test_look_at_screen_budget_exhausted():
    budget = VisionBudget(max_per_minute=1)
    budget.try_consume()  # use up the single slot
    r = CallToolRunner(_FakeHandler(vision_budget=budget))
    out = asyncio.run(r.run_tool("look_at_screen", {"question": "x"}))
    assert "moment" in out.lower()


def test_call_me_back_without_caller():
    r = CallToolRunner(_FakeHandler())  # no bridge/caller
    out = asyncio.run(r.run_tool("call_me_back", {"message": "the result"}))
    assert "can't call you back" in out.lower()


def test_agent_task_delivers_result_to_chat(monkeypatch):
    import hermes_msteams_bridge.meeting as meeting

    delivered = {}

    async def fake_deliver(conv, text):
        delivered["conv"] = conv
        delivered["text"] = text
        return True

    monkeypatch.setattr(meeting, "_deliver_to_teams", fake_deliver)
    r = CallToolRunner(_FakeHandler(thread_id="19:abc@thread.v2"))
    asyncio.run(r._run_background_task("do X", None))  # no caller, but a postable thread
    assert delivered["conv"] == "19:abc@thread.v2"
    assert "CONSULT:do X" in delivered["text"]
