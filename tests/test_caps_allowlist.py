"""Tests for vision budget, allowlist/scope/wake config, and slideshow schema."""

from __future__ import annotations

from hermes_msteams_bridge import realtime_tools
from hermes_msteams_bridge.config import resolve_config
from hermes_msteams_bridge.vision_budget import VisionBudget


def test_vision_budget_caps_per_minute():
    b = VisionBudget(max_per_minute=2)
    assert b.try_consume(now=0.0) is True
    assert b.try_consume(now=1.0) is True
    assert b.try_consume(now=2.0) is False  # cap hit within the window


def test_vision_budget_window_slides():
    b = VisionBudget(max_per_minute=1)
    assert b.try_consume(now=0.0) is True
    assert b.try_consume(now=30.0) is False  # still in window
    assert b.try_consume(now=61.0) is True  # first hit aged out


def test_vision_budget_refund_and_unlimited():
    b = VisionBudget(max_per_minute=1)
    assert b.try_consume(now=0.0) is True
    b.refund()
    assert b.try_consume(now=1.0) is True  # refunded slot reusable
    assert VisionBudget(max_per_minute=0).try_consume() is True  # 0 = unlimited


def test_config_resolves_caps_and_allowlist():
    extra = {
        "shared_secret": "s",
        "allowlist": ["AAD-123", "Alice"],
        "max_vision_per_minute": 10,
        "session_scope": "per-thread",
        "wake_phrases": ["Aria", "Bot"],
    }
    c = resolve_config(extra=extra)
    assert c.allowlist == ("aad-123", "alice")  # lowercased
    assert c.max_vision_per_minute == 10
    assert c.session_scope == "per-thread"
    assert c.wake_phrases == ("aria", "bot")


def test_show_to_caller_has_count():
    props = realtime_tools.SHOW_TO_CALLER["parameters"]["properties"]
    assert props["count"]["type"] == "integer"
