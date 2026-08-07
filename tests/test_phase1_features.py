"""Phase 1 features: timed-TTS registry, D8 speech strip, §3.3 visual recap,
D3 barge-in slideshow, D4 honest timeout, §3.6 call_user."""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes_msteams_bridge import timed_tts
from hermes_msteams_bridge.meeting import MeetingTranscript, summarize_prompt
from hermes_msteams_bridge.speech_text import strip_for_speech

# ── timed TTS registry (provider-agnostic viseme timing) ─────────────────────


@pytest.fixture(autouse=True)
def _restore_timed_providers():
    saved = list(timed_tts._PROVIDERS)
    yield
    timed_tts._PROVIDERS = saved


def test_elevenlabs_is_a_registration_not_a_hardcode():
    assert "elevenlabs" in timed_tts.timed_providers()


def test_registered_provider_wins_and_order_is_respected():
    async def fake_synth(text):
        return b"AUDIO", [("h", 0), ("i", 90)]

    timed_tts._PROVIDERS = []  # start clean: no providers -> None
    assert asyncio.run(timed_tts.synth_with_timing("hi")) is None

    timed_tts.register_timed_provider("fake", lambda: True, fake_synth)
    audio, timing = asyncio.run(timed_tts.synth_with_timing("hi"))
    assert audio == b"AUDIO" and timing[1] == ("i", 90)


def test_unavailable_and_failing_providers_fall_through():
    async def broken(text):
        raise RuntimeError("boom")

    async def good(text):
        return b"OK", [("x", 0)]

    timed_tts._PROVIDERS = []
    timed_tts.register_timed_provider("off", lambda: False, good)   # gated off
    timed_tts.register_timed_provider("broken", lambda: True, broken)
    timed_tts.register_timed_provider("good", lambda: True, good)
    audio, _ = asyncio.run(timed_tts.synth_with_timing("x"))
    assert audio == b"OK"


def test_reregistering_same_name_replaces():
    async def a(text):
        return b"A", []

    async def b(text):
        return b"B", []

    timed_tts._PROVIDERS = []
    timed_tts.register_timed_provider("p", lambda: True, a)
    timed_tts.register_timed_provider("p", lambda: True, b)
    assert timed_tts.timed_providers() == ["p"]
    audio, _ = asyncio.run(timed_tts.synth_with_timing("x"))
    assert audio == b"B"


# ── D8: markdown never reaches the voice ─────────────────────────────────────


def test_strip_for_speech_drops_syntax_keeps_words():
    raw = (
        "<think>internal plan</think>\n"
        "## Answer\n"
        "The fix is **simple**: open *Settings*, then:\n"
        "- Click `Advanced`\n"
        "1. Toggle it off\n"
        "> note this\n"
        "See [the docs](https://example.com/x) or https://example.com/raw\n"
        "```python\nprint('hi')\n```\n"
        "| a | b |\n"
    )
    out = strip_for_speech(raw)
    for token in ("**", "##", "`", "](", "```", "<think>", "|", "https://"):
        assert token not in out, f"{token!r} leaked into speech: {out!r}"
    for word in ("simple", "Settings", "Advanced", "Toggle", "the docs", "a link"):
        assert word in out
    assert "internal plan" not in out and "print" not in out


def test_strip_for_speech_unclosed_blocks():
    assert "private chain" not in strip_for_speech("<think>private chain of thought")
    assert "secret_code" not in strip_for_speech("Here you go ```python\nsecret_code()")


def test_strip_for_speech_plain_text_unchanged():
    assert strip_for_speech("Hello there, how can I help?") == "Hello there, how can I help?"


# ── §3.3: the visual track in the minutes ────────────────────────────────────


def test_transcript_visuals_dedupe_and_render():
    t = MeetingTranscript()
    t.add("Sara", "let's look at the dashboard")
    t.add_visual("Sara's shared screen")
    t.add_visual("Sara's shared screen")  # consecutive dupe collapses
    t.add_visual("Sara's shared screen: the Q3 funnel")
    body = t.render()
    assert "[Shared on screen during the call]" in body
    assert body.count("Sara's shared screen\n") == 1
    assert "Q3 funnel" in body
    assert "Presented / Shown" in summarize_prompt(body)


def test_transcript_without_visuals_renders_clean():
    t = MeetingTranscript()
    t.add("Caller", "hello")
    assert "[Shared on screen" not in t.render()


def test_visuals_alone_are_not_empty():
    t = MeetingTranscript()
    t.add_visual("camera")
    assert not t.is_empty()


# ── D3: barge-in stops the slideshow ─────────────────────────────────────────


def test_slideshow_stops_on_turn_bump(tmp_path, monkeypatch):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG fake")

    sent: list[dict] = []

    class _Session:
        closed = False

        async def send_display_image(self, data_b64, mime, *, duration_ms=None, mode=None, caption=None):
            sent.append({"mime": mime, "mode": mode})

    class _Handler:
        _session = _Session()
        _turn_id = 1

    handler = _Handler()
    runner = CallToolRunner(handler)

    import hermes_msteams_bridge.call_tools as ct

    monkeypatch.setattr(
        ct, "asyncio", ct.asyncio
    )  # no-op guard; real asyncio.sleep is patched below

    async def scenario():
        from hermes_msteams_bridge import hermes_api

        def fake_generate(prompt, **kw):
            return {"success": True, "image": str(img)}

        monkeypatch.setattr(hermes_api, "generate_image", fake_generate)

        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            handler._turn_id += 1  # caller barges in during the hold
            await real_sleep(0)

        monkeypatch.setattr(ct.asyncio, "sleep", fast_sleep)
        return await runner._show_to_caller("three cats", count=3)

    result = asyncio.run(scenario())
    assert "Stopped after 1 of 3" in result
    assert len(sent) == 1
    assert sent[0]["mode"] is None  # fullscreen, never the 224x126 overlay


# ── D4: the timeout line is honest ───────────────────────────────────────────


def test_consult_timeout_admits_failure(monkeypatch):
    from hermes_msteams_bridge.agent_consult import AgentConsult

    consult = AgentConsult()

    def hang(query):
        import time

        time.sleep(5)
        return "late"

    monkeypatch.setattr(consult, "_run_sync", hang)
    out = asyncio.run(consult.ask("q", timeout_s=0.05))
    assert "follow up" not in out.lower()
    assert "background" in out.lower()


# ── §3.6: call_user ──────────────────────────────────────────────────────────


def _cfg(**over):
    from hermes_msteams_bridge.config import TeamsVoiceConfig

    base = dict(shared_secret="s3cret", tenant_id="tenant-1", allowlist=("aad-1",))
    base.update(over)
    return TeamsVoiceConfig(**base)


def test_call_user_places_call_and_registers_message(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod
    from hermes_msteams_bridge.call_session_base import _pending_pop

    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg())

    placed: dict = {}

    async def fake_place_call(**kw):
        placed.update(kw)
        return {"callId": "call-42"}

    import hermes_msteams_bridge.outbound as outbound

    monkeypatch.setattr(outbound, "place_call", fake_place_call)

    out = json.loads(
        tools_mod.handle_call_user({"user_aad_id": "aad-1", "message": "your build is green"})
    )
    assert out["success"] is True and out["callId"] == "call-42"
    assert placed["user_object_id"] == "aad-1" and placed["tenant_id"] == "tenant-1"
    assert _pending_pop("call-42") == "your build is green"


def test_call_user_enforces_callee_allowlist(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod

    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg())
    out = json.loads(
        tools_mod.handle_call_user({"user_aad_id": "aad-EVIL", "message": "hi"})
    )
    assert "allowlist" in out["error"]


def test_call_user_deny_by_default_with_empty_allowlist(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod

    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg(allowlist=()))
    out = json.loads(tools_mod.handle_call_user({"user_aad_id": "aad-1", "message": "hi"}))
    assert "allowlist" in out["error"]


def test_call_user_requires_fields_and_config(monkeypatch):
    import hermes_msteams_bridge.tools as tools_mod

    out = json.loads(tools_mod.handle_call_user({"user_aad_id": "", "message": ""}))
    assert "required" in out["error"]

    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg(shared_secret=""))
    out = json.loads(tools_mod.handle_call_user({"user_aad_id": "aad-1", "message": "hi"}))
    assert "not configured" in out["error"]


def test_call_user_kwargs_style_invocation(monkeypatch):
    """Forward-compat: hosts may pass fields as kwargs instead of an args dict."""
    import hermes_msteams_bridge.tools as tools_mod

    monkeypatch.setattr(tools_mod, "resolve_config", lambda: _cfg())

    async def fake_place_call(**kw):
        return {"callId": "c1"}

    import hermes_msteams_bridge.outbound as outbound

    monkeypatch.setattr(outbound, "place_call", fake_place_call)
    out = json.loads(tools_mod.handle_call_user(user_aad_id="aad-1", message="hello"))
    assert out["success"] is True
