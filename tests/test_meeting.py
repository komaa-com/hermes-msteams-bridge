"""Tests for meeting transcript accumulation + summary-request detection."""

from __future__ import annotations

import asyncio

from hermes_msteams_bridge import meeting, realtime_tools
from hermes_msteams_bridge.meeting import MeetingTranscript, post_minutes


def test_transcript_add_and_render():
    t = MeetingTranscript()
    assert t.is_empty()
    t.add("Alice", "  hello there  ")
    t.add("Assistant", "hi")
    t.add("Bob", "")  # blank ignored
    assert not t.is_empty()
    assert t.render() == "Alice: hello there\nAssistant: hi"


def test_transcript_render_caps_length():
    t = MeetingTranscript()
    for i in range(1000):
        t.add("X", f"line {i}")
    out = t.render(max_chars=200)
    assert len(out) <= 200


def test_is_summary_request():
    assert meeting.is_summary_request("can you summarize the meeting")
    assert meeting.is_summary_request("send me the call minutes")
    assert meeting.is_summary_request("recap of our discussion please")
    assert not meeting.is_summary_request("what's the weather in Dubai")
    assert not meeting.is_summary_request("summarize this PDF")  # no meeting subject


def test_post_minutes_empty_is_noop():
    t = MeetingTranscript()
    out = asyncio.run(post_minutes(consult=None, transcript=t, conversation_id="19:x@thread.v2"))
    assert "wasn't enough" in out.lower()


def test_post_minutes_requires_conversation():
    """No delivery target must not be reported as no conversation.

    The transcript EXISTS here; only the target is missing. A 1:1 call has no meeting thread,
    so the bridge sends an empty threadId, and this branch is what a caller hits. Reporting
    "wasn't enough of a conversation" told them their call was too short when it was not, and
    hid a real delivery failure behind a content excuse.
    """
    t = MeetingTranscript()
    t.add("A", "hi")
    out = asyncio.run(post_minutes(consult=None, transcript=t, conversation_id=""))
    assert "wasn't enough" not in out.lower()  # that is the empty-TRANSCRIPT message
    assert "no teams chat" in out.lower()


def test_post_meeting_minutes_tool_registered():
    names = {t["name"] for t in realtime_tools.default_tools()}
    assert "post_meeting_minutes" in names
