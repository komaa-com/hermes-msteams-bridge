"""Tests for the streaming-mode VAD utterance segmenter."""

from __future__ import annotations

from hermes_teams_voice.streaming_audio import UtteranceBuffer

FRAME = b"\x00\x00" * 320  # 640 bytes = one 20 ms frame
LOUD = 0.5
QUIET = 0.0


def _buf():
    # 100 ms speech minimum, 200 ms trailing silence, 20 ms frames
    return UtteranceBuffer(silence_ms=200, min_speech_ms=100, speech_rms=0.02, frame_ms=20)


def test_drops_pre_speech_silence():
    b = _buf()
    assert b.push(FRAME, QUIET) is None
    assert b.push(FRAME, QUIET) is None  # still nothing buffered


def test_emits_after_trailing_silence():
    b = _buf()
    # 6 speech frames (120 ms ≥ 100 ms min)
    for _ in range(6):
        assert b.push(FRAME, LOUD) is None
    # trailing silence: 200 ms / 20 ms = 10 frames to trigger
    out = None
    for _ in range(10):
        out = b.push(FRAME, QUIET)
    assert out is not None
    assert len(out) == 16 * 640  # 6 speech + 10 silence frames buffered


def test_resets_after_emit():
    b = _buf()
    for _ in range(6):
        b.push(FRAME, LOUD)
    for _ in range(10):
        b.push(FRAME, QUIET)
    # next push starts fresh — a lone quiet frame is pre-speech silence again
    assert b.push(FRAME, QUIET) is None


def test_max_utterance_caps():
    b = UtteranceBuffer(silence_ms=10_000, min_speech_ms=20, frame_ms=20, max_utterance_ms=100)
    out = None
    for _ in range(10):  # 10 frames * 20 ms = 200 ms > 100 ms cap
        out = b.push(FRAME, 0.5) or out
    assert out is not None  # capped even without trailing silence
