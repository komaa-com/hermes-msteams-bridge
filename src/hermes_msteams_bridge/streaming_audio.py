"""Streaming-mode audio helpers: utterance VAD segmentation + format decode.

The streaming voice path is STT→agent→TTS (vs realtime speech-to-speech). It needs
to slice the continuous caller PCM into *utterances* (energy VAD with trailing-
silence detection), and to turn TTS output (mp3/wav/…) back into PCM 16 kHz frames.
"""

from __future__ import annotations

import subprocess
import wave


class UtteranceBuffer:
    """Accumulate caller audio and emit a complete utterance after trailing silence.

    Frame-based (each pushed frame is ~``frame_ms``). Pre-speech silence is dropped;
    an utterance ends after ``silence_ms`` of trailing silence once at least
    ``min_speech_ms`` of speech has been seen, or at ``max_utterance_ms``.
    """

    def __init__(
        self,
        *,
        silence_ms: int = 700,
        min_speech_ms: int = 300,
        speech_rms: float = 0.02,
        frame_ms: int = 20,
        max_utterance_ms: int = 20_000,
    ) -> None:
        self.speech_rms = speech_rms
        self._silence_frames = max(1, silence_ms // frame_ms)
        self._min_speech_frames = max(1, min_speech_ms // frame_ms)
        self._max_frames = max(1, max_utterance_ms // frame_ms)
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._frames = 0
        self._speech_frames = 0
        self._trailing_silence = 0
        self._in_speech = False

    def push(self, frame: bytes, rms: float) -> bytes | None:
        """Add one frame; return the utterance PCM when it completes, else None."""
        is_speech = rms >= self.speech_rms
        if not self._in_speech:
            if not is_speech:
                return None  # drop pre-speech silence
            self._in_speech = True
        self._buf += frame
        self._frames += 1
        if is_speech:
            self._speech_frames += 1
            self._trailing_silence = 0
        else:
            self._trailing_silence += 1
        ended = (
            self._trailing_silence >= self._silence_frames
            and self._speech_frames >= self._min_speech_frames
        )
        if ended or self._frames >= self._max_frames:
            return self._emit()
        return None

    def _emit(self) -> bytes:
        pcm = bytes(self._buf)
        self.reset()
        return pcm


def write_wav_pcm16(pcm: bytes, path: str, sample_rate: int = 16_000) -> None:
    """Write mono 16-bit PCM to a WAV file for the STT engine."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def decode_to_pcm16k(path: str) -> bytes:
    """Decode any audio file (mp3/wav/ogg…) to raw PCM 16 kHz mono via ffmpeg.

    Returns ``b""`` if ffmpeg is unavailable or the decode fails (caller degrades)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", path, "-ar", "16000", "-ac", "1", "-f", "s16le", "-loglevel", "quiet", "-"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def decode_bytes_to_pcm16k(data: bytes) -> bytes:
    """Decode in-memory audio bytes (mp3/…) to PCM 16 kHz mono via ffmpeg stdin."""
    if not data:
        return b""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "s16le", "-loglevel", "quiet", "pipe:1"],
            input=data,
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return b""
    return proc.stdout if proc.returncode == 0 else b""
