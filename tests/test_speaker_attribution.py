"""Speaker attribution — WHO is talking must reach the model, not just the minutes.

The worker stamps ``speakerName`` on unmixed ``audio.frame`` messages. Storing it for
the end-of-call minutes only meant the live model heard a five-person meeting as one
undifferentiated voice, and attribution materialised retroactively or not at all.

* realtime — the active speaker is announced to the model on every change, as a
  non-responding conversation item, and the instructions teach the model what those
  announcements are for.
* streaming — the caller turn handed to the agent is prefixed with the speaker.
* both — a finished turn is filed under the person who STARTED it, so a group call
  does not collapse onto whoever spoke last.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from hermes_msteams_bridge import handlers, protocol
from hermes_msteams_bridge.config import resolve_config
from hermes_msteams_bridge.realtime.openai_client import RealtimeConfig

PCM_SILENCE = base64.b64encode(b"\x00" * 640).decode("ascii")  # 20 ms @ 16 kHz


class FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, human_count=1):
        self._ws = FakeWS()
        self.recording_active = True
        self.human_count = human_count
        self.call_id = "call-1"

    async def send_expression(self, e):
        ...

    async def send_audio_frame(self, *a):
        ...

    async def send_speech_marks(self, *a, **k):
        ...

    async def send_assistant_cancel(self, t):
        ...


class FakeRealtime:
    def __init__(self):
        self.texts: list[tuple[str, bool]] = []  # (text, respond)
        self.audio_pushes = 0

    async def push_audio(self, pcm):
        self.audio_pushes += 1

    async def send_user_text(self, text, *, respond=True):
        self.texts.append((text, respond))

    async def create_response(self):
        ...

    async def cancel_response(self):
        ...

    async def request_say(self, instruction):
        ...


def _audio(speaker: str | None = None) -> protocol.AudioFrame:
    return protocol.AudioFrame(
        type="audio.frame", seq=1, timestamp_ms=20,
        payload_base64=PCM_SILENCE, speaker_name=speaker,
    )


def _realtime(human_count=2):
    cfg = resolve_config(extra={"shared_secret": "s", "wake_phrases": ["aria"]})
    h = handlers.RealtimeCallSessionHandler(RealtimeConfig(api_key="x"), bridge_config=cfg)
    h._rt = FakeRealtime()
    sess = FakeSession(human_count=human_count)
    h._session = sess
    return h, sess


def _streaming(human_count=2):
    cfg = resolve_config(extra={"shared_secret": "s", "wake_phrases": ["aria"]})
    h = handlers.StreamingCallSessionHandler(bridge_config=cfg)
    sess = FakeSession(human_count=human_count)
    h._session = sess
    return h, sess


# ── realtime: the model is told who is speaking ──────────────────────────────


def test_realtime_announces_the_speaker_to_the_model_once_per_change():
    h, sess = _realtime()

    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))
    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))  # same speaker, no repeat
    asyncio.run(h.on_audio_frame(sess, _audio("Bob Chen")))

    said = [t for t, _respond in h._rt.texts]
    assert said == ["[The person now speaking is Sara Ali.]", "[The person now speaking is Bob Chen.]"]
    # The label must NOT make the bot start talking: no forced response.
    assert all(respond is False for _t, respond in h._rt.texts)
    assert h._rt.audio_pushes == 3  # audio still flows through unchanged


def test_realtime_speaker_announcement_is_recording_gated():
    h, sess = _realtime()
    sess.recording_active = False  # require_recording_status defaults on
    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))
    assert h._rt.texts == [] and h._rt.audio_pushes == 0


def test_realtime_frames_without_a_speaker_name_announce_nothing():
    h, sess = _realtime()
    asyncio.run(h.on_audio_frame(sess, _audio(None)))
    assert h._rt.texts == []


def test_realtime_instructions_teach_the_model_to_use_speaker_labels():
    h, _sess = _realtime()
    h._caller = protocol.CallerInfo(aad_id="a", display_name="Sara Ali")
    text = h._build_instructions()
    assert "CALLER IDENTITY" in text
    assert "Sara" in text
    assert "The person now speaking is" in text  # the exact shape it will receive
    assert "never read an announcement aloud" in text


def test_realtime_turn_is_attributed_to_whoever_started_it():
    """A group call must not collapse onto the last voice heard.

    Input transcripts finalize a beat after end-of-speech, so by then the next
    person's frames have already landed. The turn belongs to whoever started it.
    """
    h, sess = _realtime()

    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))  # Sara starts talking
    asyncio.run(h.on_audio_frame(sess, _audio("Bob Chen")))  # Bob cuts in before it finalizes
    asyncio.run(h._on_input_transcript("we should ship on friday"))

    asyncio.run(h.on_audio_frame(sess, _audio("Bob Chen")))
    asyncio.run(h._on_input_transcript("agreed, friday works"))

    assert h._meeting.turns == [
        ("Sara Ali", "we should ship on friday"),
        ("Bob Chen", "agreed, friday works"),
    ]


def test_realtime_falls_back_to_the_caller_name_without_unmixed_audio():
    h, sess = _realtime(human_count=1)
    h._caller = protocol.CallerInfo(aad_id="a", display_name="Dee Smith")
    asyncio.run(h.on_audio_frame(sess, _audio(None)))
    asyncio.run(h._on_input_transcript("hello there"))
    assert h._meeting.turns == [("Dee", "hello there")]


# ── streaming: the agent sees who asked ──────────────────────────────────────


def test_streaming_audio_frame_captures_the_speaker():
    h, sess = _streaming()
    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))
    assert h._last_speaker == "Sara Ali"


def test_streaming_turn_is_prefixed_with_the_speaker(monkeypatch):
    h, sess = _streaming()
    asked: list[str] = []
    spoken: list[str] = []

    class FakeConsult:
        async def ask(self, text, **kw):
            asked.append(text)
            return "Ready on friday."

    async def fake_transcribe(_pcm):
        return "aria, what's the status"  # addressed: this is a group call

    async def fake_speak(text):
        spoken.append(text)

    h._consult = FakeConsult()
    h._transcribe = fake_transcribe  # type: ignore[assignment]
    h._speak = fake_speak  # type: ignore[assignment]

    asyncio.run(h.on_audio_frame(sess, _audio("Sara Ali")))  # real call site sets the speaker
    asyncio.run(h._handle_utterance(b"\x00" * 640))

    assert asked == ["Sara Ali: aria, what's the status"]
    assert spoken == ["Ready on friday."]
    assert ("Sara Ali", "aria, what's the status") in h._meeting.turns


def test_streaming_turn_is_unprefixed_without_unmixed_audio():
    h, _sess = _streaming(human_count=1)
    h._caller = protocol.CallerInfo(aad_id="a", display_name="Dee Smith")
    asked: list[str] = []

    class FakeConsult:
        async def ask(self, text, **kw):
            asked.append(text)
            return "ok"

    async def fake_transcribe(_pcm):
        return "what's the status"

    async def fake_speak(text):
        ...

    h._consult = FakeConsult()
    h._transcribe = fake_transcribe  # type: ignore[assignment]
    h._speak = fake_speak  # type: ignore[assignment]
    asyncio.run(h._handle_utterance(b"\x00" * 640))

    assert asked == ["what's the status"]  # no label invented from the roster
    assert ("Dee", "what's the status") in h._meeting.turns


@pytest.mark.parametrize("handler_name", ["RealtimeCallSessionHandler", "StreamingCallSessionHandler"])
def test_both_brains_read_speaker_name_off_the_wire(handler_name):
    """The wire field must be consumed by the real inbound entry point, not a helper."""
    import inspect

    src = inspect.getsource(getattr(handlers, handler_name).on_audio_frame)
    assert "speaker_name" in src
