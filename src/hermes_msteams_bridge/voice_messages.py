"""Inbound Teams voice messages, folded into the chat turn as words.

A Teams voice note arrives on the managed chat lane as an attachment of kind ``audio`` carrying a
gateway-signed, short-lived URL (``protocol/chat-schema.yaml``). Without this module the agent can
only NAME the clip, so "listen to this and tell me what you think" is unanswerable. With it, the clip
is fetched, transcribed by whatever STT provider the operator already configured, and the words go
into the same agent turn as the message text.

Chat lane only. There are no frames, no roster, no timers and no state here: it is one synchronous
step inside one message turn, and it touches nothing in the call machinery.

OPT-IN (``transcribe_voice_messages``, default off) because each clip is a paid STT call and a voice
note can run for minutes. That is recurring per-message spend, so an operator should choose it rather
than discover it on a bill.

Failure is reported TO THE MODEL, never swallowed: a dropped voice note is indistinguishable from an
empty message, and the agent would answer as though nothing had been sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Bytes accepted per clip. Deliberately LARGER than the cap an inline image would get: a voice note
#: is minutes of audio, not a screenshot.
MAX_CLIP_BYTES = 16 * 1024 * 1024
#: Clips transcribed per message. Deliberately SMALLER than an image count cap, and the asymmetry is
#: the point: each clip costs an STT call, so what needs bounding tightly here is the number of PAID
#: calls one inbound message can trigger, not the bytes.
MAX_CLIPS_PER_MESSAGE = 2
#: Per-clip fetch budget. Longer than an image fetch would get, for the same reason the byte cap is.
FETCH_TIMEOUT_S = 20.0
#: Read granularity for the streaming size cap.
READ_CHUNK_BYTES = 64 * 1024

# These four are function-level constants on purpose and NOT config keys. An operator has no
# information with which to tune an SSRF cap, and every value they could get wrong here fails in a way
# that costs money or opens a fetch. The one thing they do decide is whether the feature runs at all.


@dataclass(frozen=True)
class VoiceClip:
    """One fetched voice note, ready to hand to file-based STT."""

    data: bytes
    mime: str
    #: Filename as Teams reported it. Empty when the clip is unnamed; the placeholder omits the
    #: quoted segment rather than printing an empty pair of quotes.
    name: str = ""


# ── fetching ─────────────────────────────────────────────────────────────────────────────────────


def _origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` with the default port filled in, or None when the URL is unusable."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:  # e.g. a non-numeric port
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    return scheme, host, port or {"http": 80, "https": 443}.get(scheme, 0)


def _same_origin(url: str, gateway_origin: str) -> bool:
    """True when ``url`` is on the gateway this connection replies through.

    This is the security core of the fetch. The attachment URL is gateway-SIGNED, but that signature
    is verified BY the gateway - on this side it proves nothing, and the URL arrived inside a message
    somebody else wrote. Pinning the origin is the only thing that bounds where this fetch can go.

    An unset pin means no restriction, which only happens if the reply endpoint itself is unset - a
    configuration in which the lane cannot answer anyone anyway.
    """
    if not gateway_origin:
        return True
    a, b = _origin(url), _origin(gateway_origin)
    return a is not None and b is not None and a == b


def _header(headers: Any, name: str) -> str:
    """Case-insensitive header read.

    aiohttp hands back a CIMultiDict that does this natively; the plain dicts tests and other
    HTTP shims use do not, and a header missed because of its casing would skip a guard.
    """
    try:
        value = headers.get(name)
    except AttributeError:
        return ""
    if value is None:
        for key, val in dict(headers).items():
            if str(key).lower() == name.lower():
                value = val
                break
    return str(value or "")


def _resolve_mime(declared: Any, header_value: str) -> str:
    """Attachment-declared content type first, response header second; lowercased, cut at ';'.

    The declared value wins because it is what the relay knows about the clip, and it also picks the
    spool file's extension - which is why sanitizing that extension is load-bearing rather than tidy.
    """
    raw = str(declared or "") or header_value
    return raw.split(";", 1)[0].strip().lower()


@asynccontextmanager
async def _default_get(url: str) -> AsyncIterator[Any]:
    """One-shot GET for a clip.

    ``allow_redirects=False`` is the point: a gateway-signed, same-host URL that redirects is already
    anomalous, and following it would re-open the door the origin pin just closed. The 3xx surfaces as
    a non-2xx status and the clip is dropped.

    A fresh session per clip (at most two) rather than borrowing the chat server's: that session
    belongs to the reply leg, and an attachment fetch must not be tied to its lifecycle.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as resp:
            yield resp


async def _read_capped(resp: Any, max_bytes: int) -> bytes | None:
    """Read the body, ABORTING the moment it goes over the cap.

    Reading it whole and checking the size afterwards would let a content-length that lies - or is
    simply absent - allocate whatever it liked before we got to object. The cap has to hold WHILE
    reading. Returns None when the body runs over, so the caller drops that clip.
    """
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(READ_CHUNK_BYTES):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return None
    return bytes(buf)


async def fetch_voice_clips(
    attachments: Sequence[Mapping[str, Any]] | None,
    *,
    gateway_origin: str,
    get: Callable[[str], Any] | None = None,
    max_bytes: int = MAX_CLIP_BYTES,
    max_clips: int = MAX_CLIPS_PER_MESSAGE,
) -> list[VoiceClip]:
    """Fetch the relayable ``audio`` attachments of one message, in order, guards first.

    ``get`` is an async context manager over the response, injectable for tests; the default issues a
    real redirect-refusing aiohttp GET. Best-effort per clip: one bad attachment drops that
    attachment and never the turn.
    """
    opener = get or _default_get
    clips: list[VoiceClip] = []
    for attachment in attachments or []:
        if len(clips) >= max_clips:
            break
        if attachment.get("kind") != "audio" or attachment.get("relayable") is False:
            continue
        url = str(attachment.get("url") or "")
        if not url:
            continue
        # Refused BEFORE the request is issued, not after the response comes back.
        if not _same_origin(url, gateway_origin):
            continue
        try:
            async with opener(url) as resp:
                if not 200 <= int(resp.status) < 300:
                    continue
                mime = _resolve_mime(attachment.get("contentType"), _header(resp.headers, "content-type"))
                # Refuse anything that is not audio BEFORE reading a byte of it. video/* is accepted
                # because Teams clients label some voice/video notes with a container type
                # (video/mp4) that STT reads perfectly well.
                if not (mime.startswith("audio/") or mime.startswith("video/")):
                    continue
                declared = _declared_length(_header(resp.headers, "content-length"))
                # A body that declares itself over the cap is refused without transferring it at all.
                if declared is not None and declared > max_bytes:
                    continue
                data = await _read_capped(resp, max_bytes)
            if not data:
                continue
            clips.append(VoiceClip(data=data, mime=mime, name=str(attachment.get("name") or "")))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad clip drops that clip, never the turn
            logger.warning("managed chat: voice message fetch failed", exc_info=True)
    return clips


def _declared_length(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (AttributeError, ValueError):
        return None


# ── transcribing ─────────────────────────────────────────────────────────────────────────────────

_EXT_UNSAFE = re.compile(r"[^a-z0-9]")


def _suffix_for(mime: str) -> str:
    """Spool-file extension derived from the mime subtype.

    Transcribers routinely sniff the container by extension, and an .ogg written as .wav decodes to
    nothing with no error worth reading. Stripping to ``[a-z0-9]`` is also what stops an
    attacker-declared contentType from steering the path.
    """
    subtype = mime.split("/", 1)[1] if "/" in mime else ""
    return _EXT_UNSAFE.sub("", subtype.lower()) or "bin"


def _spool_dir() -> Path:
    """Where clip bytes land on the way to STT.

    The Hermes cache dir when a host is present - the same place the streaming call lane spools its
    utterance WAVs - and the OS temp dir otherwise, so this path is exercisable without a host.
    """
    try:
        from .hermes_api import hermes_home

        directory = hermes_home() / "cache" / "msteams_bridge"
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception:  # noqa: BLE001 - no host, or an unwritable home: the OS temp dir will do
        return Path(tempfile.gettempdir())


async def _hermes_transcribe(path: str) -> str:
    """Default STT: the host's file-based transcriber, through the hermes_api boundary.

    Whatever ``stt.provider`` the operator already configured applies unchanged, including Hermes's
    own fallback chain. A failed result RAISES rather than returning "": the caller must be able to
    tell "STT could not read this" from "there was no speech in it", because they are different
    sentences to the model.
    """
    from .hermes_api import transcribe

    result = await asyncio.to_thread(transcribe, path)
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or "transcription failed"))
    return str(result.get("transcript") or "")


async def transcribe_voice_messages(
    attachments: Sequence[Mapping[str, Any]] | None,
    *,
    enabled: bool,
    gateway_origin: str,
    get: Callable[[str], Any] | None = None,
    transcribe: Callable[[str], Awaitable[str]] | None = None,
    max_bytes: int = MAX_CLIP_BYTES,
    max_clips: int = MAX_CLIPS_PER_MESSAGE,
) -> str:
    """Return the voice-note block for one inbound message ("" when it contributes nothing).

    "" is returned when the feature is off, when nothing in the message is audio, and when there are
    no attachments at all. Feature-off short-circuits BEFORE any network work, so off costs zero
    fetches and zero STT calls.

    Exactly three line shapes, all bracketed so the model reads them as metadata rather than as words
    the sender spoke::

        [voice message "clip.ogg", transcribed]: <text>
        [voice message "clip.ogg": no speech detected]
        [voice message "clip.ogg" could not be transcribed - tell the sender you could not play it]

    The quoted name is omitted when the attachment has none. Nothing raises out of here: every
    per-clip failure becomes a placeholder and the turn always runs.
    """
    if not enabled:
        return ""
    clips = await fetch_voice_clips(
        attachments,
        gateway_origin=gateway_origin,
        get=get,
        max_bytes=max_bytes,
        max_clips=max_clips,
    )
    if not clips:
        return ""

    stt = transcribe or _hermes_transcribe
    lines: list[str] = []
    for clip in clips:
        label = f' "{clip.name}"' if clip.name else ""
        # The host's STT takes a PATH, so the bytes have to land on disk first. A transcriber that
        # accepted bytes would drop this whole step.
        spooled: Path | None = None
        try:
            spooled = _spool_dir() / f"voicemsg-{uuid.uuid4().hex}.{_suffix_for(clip.mime)}"
            await asyncio.to_thread(spooled.write_bytes, clip.data)
            text = (await stt(str(spooled))).strip()
            lines.append(
                f"[voice message{label}, transcribed]: {text}"
                if text
                else f"[voice message{label}: no speech detected]"
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the turn runs either way; the model is told what happened
            logger.warning("managed chat: voice message transcription failed", exc_info=True)
            # Deliberately NOT silent: the agent is told the clip exists and could not be read.
            lines.append(
                f"[voice message{label} could not be transcribed - "
                "tell the sender you could not play it]"
            )
        finally:
            # Swallowed on purpose: cleanup must not mask the transcription result, good or bad.
            if spooled is not None:
                try:
                    spooled.unlink(missing_ok=True)
                except OSError:
                    logger.debug("managed chat: could not remove %s", spooled, exc_info=True)
    return "\n".join(lines)
