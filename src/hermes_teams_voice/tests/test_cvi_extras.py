"""Tests for CVI extras: attribution, look_at_screen scope, ambient/DTMF client paths."""

from __future__ import annotations

import asyncio

from hermes_teams_voice import realtime_tools
from hermes_teams_voice.realtime.openai_client import RealtimeConfig, RealtimeSession
from hermes_teams_voice.vision_store import StoredFrame


def _session_capturing() -> tuple[RealtimeSession, list[dict]]:
    sent: list[dict] = []
    s = RealtimeSession(RealtimeConfig(api_key="x"))
    s._closed = False

    async def fake_send(obj):
        sent.append(obj)

    s._send = fake_send  # type: ignore[assignment]
    return s, sent


def test_frame_describe_attribution():
    assert StoredFrame("screenshare", "x", "image/jpeg", 1, "Alice").describe() == "Alice's shared screen"
    assert StoredFrame("camera", "x", "image/jpeg", 1).describe() == "camera"


def test_look_at_screen_schema_has_scope():
    props = realtime_tools.LOOK_AT_SCREEN["parameters"]["properties"]
    assert props["scope"]["enum"] == ["live", "history"]


def test_send_user_text_guards_double_response():
    s, sent = _session_capturing()
    s._response_active = True  # a response is already in progress
    asyncio.run(s.send_user_text("the caller pressed 1", respond=True))
    types = [m["type"] for m in sent]
    assert "conversation.item.create" in types
    assert "response.create" not in types  # guarded — no 'already active' error


def test_send_user_text_creates_when_idle():
    s, sent = _session_capturing()
    s._response_active = False
    asyncio.run(s.send_user_text("hello", respond=True))
    assert [m["type"] for m in sent] == ["conversation.item.create", "response.create"]


def test_send_image_is_ambient_no_response():
    s, sent = _session_capturing()
    asyncio.run(s.send_image("data:image/jpeg;base64,AAA"))
    assert sent[0]["item"]["content"][0]["type"] == "input_image"
    assert all(m["type"] != "response.create" for m in sent)
