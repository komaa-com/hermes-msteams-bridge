"""Inbound Teams voice messages -> words in the chat turn.

Two halves, and the second one is where the reference implementation had nothing at all:

* the FETCH guard - an ordered set of refusals whose whole job is that a URL arriving inside a
  message someone else wrote cannot point this process at an arbitrary host, or make it read an
  arbitrary number of bytes, or spend an unbounded number of STT calls;
* the ORCHESTRATION - the default (off), the three placeholder shapes, the mime-to-extension rule,
  and the fact that a real hosting path actually calls any of it.

NOTE: sync test methods wrapping asyncio.run - the repo convention (pytest-asyncio is not a test
dependency here; see test_managed_chat.py).
"""

from __future__ import annotations

import ast
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_msteams_bridge import voice_messages
from hermes_msteams_bridge.managed_chat import InboundChat, build_turn_query
from hermes_msteams_bridge.voice_messages import (
    MAX_CLIP_BYTES,
    MAX_CLIPS_PER_MESSAGE,
    fetch_voice_clips,
    transcribe_voice_messages,
)

GATEWAY = "https://teams.standin.komaa.com/api/chat/reply"
PKG_DIR = Path(voice_messages.__file__).resolve().parent


def clip(**over):
    a = {"kind": "audio", "name": "voice.ogg", "url": "https://teams.standin.komaa.com/att/1"}
    a.update(over)
    return a


# ── a fake HTTP response, shaped like the aiohttp one the real opener yields ──────────────────────


class _Body:
    """Chunked body that RECORDS how much was pulled, so 'refused before reading' is checkable."""

    def __init__(self, chunks: list[bytes], forbid_read: bool = False) -> None:
        self._chunks = chunks
        self._forbid_read = forbid_read
        self.chunks_read = 0

    def iter_chunked(self, _size: int):
        return self._gen()

    async def _gen(self):
        if self._forbid_read:
            raise AssertionError("the body was read after a guard should have refused it")
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


class _Response:
    def __init__(self, body: bytes | list[bytes], status: int = 200, headers: dict | None = None,
                 forbid_read: bool = False) -> None:
        chunks = [body] if isinstance(body, bytes) else list(body)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "audio/ogg"}
        self.content = _Body(chunks, forbid_read=forbid_read)


def opener(response, calls: list[str] | None = None):
    """An injectable ``get``: async context manager over a canned response."""
    calls = calls if calls is not None else []

    @asynccontextmanager
    async def _get(url: str):
        calls.append(url)
        yield response(url) if callable(response) else response

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


# ── the fetch guard ───────────────────────────────────────────────────────────────────────────────


class TestFetchGuard:
    def test_fetches_a_clip_from_the_pinned_gateway(self):
        get = opener(_Response(b"OGGDATA"))
        got = asyncio.run(fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=get))
        assert len(got) == 1
        assert got[0].data == b"OGGDATA"
        assert got[0].name == "voice.ogg"
        assert got[0].mime == "audio/ogg"

    def test_refuses_a_url_that_is_not_on_the_gateway_without_issuing_the_request(self):
        # The whole SSRF guard: a message can name any URL it likes, and the gateway's signature on
        # that URL is verified BY the gateway - it proves nothing here.
        get = opener(_Response(b"EVIL"))
        got = asyncio.run(
            fetch_voice_clips([clip(url="https://evil.example/x")], gateway_origin=GATEWAY, get=get)
        )
        assert got == []
        assert get.calls == [], "refused AFTER the request instead of before it"

    def test_a_lookalike_host_is_not_the_gateway(self):
        get = opener(_Response(b"EVIL"))
        for url in (
            "https://teams.standin.komaa.com.evil.example/att/1",  # suffix trick
            "http://teams.standin.komaa.com/att/1",                # scheme downgrade
            "https://teams.standin.komaa.com:8443/att/1",          # different port
            "not a url",
        ):
            assert asyncio.run(fetch_voice_clips([clip(url=url)], gateway_origin=GATEWAY, get=get)) == []
        assert get.calls == []

    @pytest.mark.parametrize(
        "attachment",
        [clip(kind="image"), clip(kind="file"), clip(relayable=False), clip(url=None), clip(url="")],
        ids=["image", "file", "unrelayable", "no-url", "empty-url"],
    )
    def test_skips_anything_that_is_not_a_relayable_audio_clip(self, attachment):
        get = opener(_Response(b"X"))
        assert asyncio.run(fetch_voice_clips([attachment], gateway_origin=GATEWAY, get=get)) == []
        assert get.calls == []

    def test_absent_attachment_list_is_not_an_error(self):
        assert asyncio.run(fetch_voice_clips(None, gateway_origin=GATEWAY, get=opener(_Response(b"X")))) == []
        assert asyncio.run(fetch_voice_clips([], gateway_origin=GATEWAY, get=opener(_Response(b"X")))) == []

    def test_refuses_a_non_audio_body_before_reading_a_byte_of_it(self):
        resp = _Response(b"<html>", headers={"Content-Type": "text/html"}, forbid_read=True)
        got = asyncio.run(
            fetch_voice_clips([clip(contentType=None)], gateway_origin=GATEWAY, get=opener(resp))
        )
        assert got == []
        assert resp.content.chunks_read == 0

    def test_accepts_a_video_container_teams_labels_some_notes_with(self):
        resp = _Response(b"MP4", headers={"Content-Type": "video/mp4"})
        got = asyncio.run(fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(resp)))
        assert [c.mime for c in got] == ["video/mp4"]

    def test_declared_content_type_wins_over_the_header(self):
        resp = _Response(b"OGG", headers={"Content-Type": "application/octet-stream"})
        got = asyncio.run(
            fetch_voice_clips([clip(contentType="AUDIO/OGG; codecs=opus")], gateway_origin=GATEWAY,
                              get=opener(resp))
        )
        assert [c.mime for c in got] == ["audio/ogg"]

    def test_non_2xx_is_skipped(self):
        # 3xx included on purpose: the real opener refuses redirects, so a redirect surfaces here as
        # a non-2xx status rather than as a fetch of somewhere else.
        for status in (302, 404, 500):
            resp = _Response(b"X", status=status, forbid_read=True)
            assert asyncio.run(fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(resp))) == []

    def test_an_empty_body_is_skipped(self):
        assert asyncio.run(fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(_Response(b"")))) == []

    def test_refuses_a_clip_that_declares_itself_over_the_byte_cap(self):
        resp = _Response(
            b"A" * 50,
            headers={"Content-Type": "audio/ogg", "Content-Length": "999999999"},
            forbid_read=True,
        )
        got = asyncio.run(
            fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(resp), max_bytes=1024)
        )
        assert got == []
        assert resp.content.chunks_read == 0, "transferred a body it had already refused"

    def test_a_body_that_lies_about_its_length_is_aborted_mid_read(self):
        # The gap the reference left open for audio: a content-length that lies, or is simply absent,
        # would otherwise get to allocate whatever it liked before anyone objected.
        chunks = [b"A" * 1024] * 100  # 100 KiB, no Content-Length header at all
        resp = _Response(chunks, headers={"Content-Type": "audio/ogg"})
        got = asyncio.run(
            fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(resp), max_bytes=2048)
        )
        assert got == []
        assert resp.content.chunks_read <= 3, "read the whole body and checked the size afterwards"

        # The same body under a cap that fits comes through, so the abort is about the CAP and not
        # about a missing content-length.
        big = _Response(chunks, headers={"Content-Type": "audio/ogg"})
        got = asyncio.run(
            fetch_voice_clips([clip()], gateway_origin=GATEWAY, get=opener(big), max_bytes=200_000)
        )
        assert len(got) == 1 and len(got[0].data) == 100 * 1024

    def test_caps_the_clip_count_so_one_message_cannot_multiply_into_unbounded_stt_calls(self):
        many = [clip(url=f"https://teams.standin.komaa.com/att/{i}") for i in range(6)]
        get = opener(_Response(b"A"))
        got = asyncio.run(fetch_voice_clips(many, gateway_origin=GATEWAY, get=get, max_clips=2))
        assert len(got) == 2
        assert len(get.calls) == 2, "fetched clips it was never going to keep"

    def test_one_bad_clip_drops_that_clip_and_not_the_others(self):
        def response_for(url: str):
            if url.endswith("/att/0"):
                raise RuntimeError("connection reset")
            return _Response(b"GOOD")

        got = asyncio.run(
            fetch_voice_clips(
                [clip(url="https://teams.standin.komaa.com/att/0"),
                 clip(url="https://teams.standin.komaa.com/att/1")],
                gateway_origin=GATEWAY,
                get=opener(response_for),
            )
        )
        assert [c.data for c in got] == [b"GOOD"]

    def test_the_caps_are_the_documented_asymmetry_against_images(self):
        # Bigger bytes (a voice note is minutes of audio), smaller count (each clip costs an STT
        # call). Reversing either is the bug this pins.
        assert MAX_CLIP_BYTES == 16 * 1024 * 1024
        assert MAX_CLIPS_PER_MESSAGE == 2


# ── the orchestrator: default, line shapes, spool file ────────────────────────────────────────────


def _run_note(attachments, *, enabled=True, stt=None, get=None, **kw):
    return asyncio.run(
        transcribe_voice_messages(
            attachments,
            enabled=enabled,
            gateway_origin=GATEWAY,
            get=get if get is not None else opener(_Response(b"OGG")),
            transcribe=stt,
            **kw,
        )
    )


class TestVoiceNote:
    def test_off_makes_no_fetch_and_no_stt_call(self):
        get = opener(_Response(b"OGG"))
        stt_calls: list[str] = []

        async def stt(path):
            stt_calls.append(path)
            return "should never run"

        assert _run_note([clip()], enabled=False, get=get, stt=stt) == ""
        assert get.calls == [] and stt_calls == []

    def test_transcribed_words_land_in_the_note(self):
        async def stt(_path):
            return "  can you look at the invoice  "

        assert _run_note([clip()], stt=stt) == '[voice message "voice.ogg", transcribed]: can you look at the invoice'

    def test_an_unnamed_clip_omits_the_quoted_segment(self):
        async def stt(_path):
            return "hello"

        assert _run_note([clip(name=None)], stt=stt) == "[voice message, transcribed]: hello"

    def test_an_empty_transcript_says_no_speech_detected(self):
        async def stt(_path):
            return "   "

        assert _run_note([clip()], stt=stt) == '[voice message "voice.ogg": no speech detected]'

    def test_a_failed_transcription_still_produces_a_placeholder(self):
        # Fail LOUDLY to the model: a dropped voice note is otherwise indistinguishable from an empty
        # message and the agent answers as though nothing was sent.
        async def stt(_path):
            raise RuntimeError("stt provider is down")

        note = _run_note([clip()], stt=stt)
        assert note == (
            '[voice message "voice.ogg" could not be transcribed - '
            "tell the sender you could not play it]"
        )

    def test_a_failing_clip_does_not_take_the_other_clip_with_it(self):
        seen: list[str] = []

        async def stt(path):
            seen.append(path)
            if len(seen) == 1:
                raise RuntimeError("boom")
            return "the second one"

        two = [
            clip(name="a.ogg", url="https://teams.standin.komaa.com/att/0"),
            clip(name="b.ogg", url="https://teams.standin.komaa.com/att/1"),
        ]
        note = _run_note(two, stt=stt)
        assert note.splitlines() == [
            '[voice message "a.ogg" could not be transcribed - tell the sender you could not play it]',
            '[voice message "b.ogg", transcribed]: the second one',
        ]

    def test_nothing_audio_contributes_nothing(self):
        async def stt(_path):
            return "x"

        assert _run_note([clip(kind="file")], stt=stt) == ""
        assert _run_note([], stt=stt) == ""
        assert _run_note(None, stt=stt) == ""

    def test_the_spool_file_carries_the_container_extension_and_is_cleaned_up(self):
        # Transcribers sniff the container by extension: an .ogg written as .wav decodes to nothing
        # with no error worth reading. The strip is also what stops an attacker-declared contentType
        # from steering the path.
        seen: list[Path] = []

        async def stt(path):
            p = Path(path)
            seen.append(p)
            assert p.exists() and p.read_bytes() == b"OGG"
            return "ok"

        _run_note([clip(contentType="audio/x-wav")], stt=stt)
        assert seen[0].suffix == ".xwav"
        assert not seen[0].exists(), "the spool file outlived the turn"

        seen.clear()
        _run_note([clip(contentType="audio/../../etc/passwd")], stt=stt)
        assert seen[0].suffix == ".etcpasswd"
        # The sanitizer is what keeps an attacker-declared type from walking out of the spool dir.
        assert seen[0].parent == voice_messages._spool_dir()

        seen.clear()
        _run_note([clip(contentType="application/octet-stream", name=None)], get=opener(
            _Response(b"OGG", headers={"Content-Type": "audio/ogg"})), stt=stt)
        assert seen == [], "a non-audio declared type is refused, so nothing is ever spooled"


# ── config: the default is off, and it is the only key ────────────────────────────────────────────


class TestConfig:
    def _resolve(self, extra=None, env=None, monkeypatch=None):
        from hermes_msteams_bridge.config import resolve_config

        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        return resolve_config(extra if extra is not None else {"shared_secret": "voice"})

    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES", raising=False)
        assert self._resolve(monkeypatch=monkeypatch).transcribe_voice_messages is False

    def test_opt_in_through_the_plugin_config_block(self, monkeypatch):
        monkeypatch.delenv("MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES", raising=False)
        cfg = self._resolve({"shared_secret": "v", "transcribe_voice_messages": True},
                            monkeypatch=monkeypatch)
        assert cfg.transcribe_voice_messages is True

    def test_opt_in_through_the_env_var(self, monkeypatch):
        cfg = self._resolve(env={"MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES": "true"},
                            monkeypatch=monkeypatch)
        assert cfg.transcribe_voice_messages is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_anything_but_an_explicit_yes_reads_as_off(self, value, monkeypatch):
        cfg = self._resolve({"shared_secret": "v", "transcribe_voice_messages": value},
                            env={"MSTEAMS_BRIDGE_TRANSCRIBE_VOICE_MESSAGES": value},
                            monkeypatch=monkeypatch)
        assert cfg.transcribe_voice_messages is False


# ── wiring: a capability nothing calls is a bug ───────────────────────────────────────────────────


def _called_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


class TestWiredNotOrphaned:
    """Both hosting paths must reach the transcriber. Dropping a call site has to FAIL a test, not
    quietly turn the feature off on one of the two ways this plugin can be run."""

    def test_both_hosting_paths_build_the_turn_through_the_shared_builder(self):
        for module in ("cli.py", "gateway_adapter.py"):
            assert "build_turn_query" in _called_names(PKG_DIR / module), (
                f"{module} no longer calls build_turn_query - its turns lost voice transcription"
            )

    def test_the_shared_builder_calls_the_transcriber(self):
        assert "transcribe_voice_messages" in _called_names(PKG_DIR / "managed_chat.py")

    def test_neither_hosting_path_still_assembles_the_query_itself(self):
        # Drift is how one path ends up with the capability and the other without it.
        for module in ("cli.py", "gateway_adapter.py"):
            assert "attachments_note" not in _called_names(PKG_DIR / module), (
                f"{module} builds the turn query itself again; it will drift from the other path"
            )

    def test_the_voice_note_lands_ahead_of_the_attachment_note(self, monkeypatch):
        async def fake(attachments, **kw):
            assert kw["enabled"] is True
            assert kw["gateway_origin"] == GATEWAY
            return '[voice message "voice.ogg", transcribed]: read this out'

        monkeypatch.setattr(voice_messages, "transcribe_voice_messages", fake)
        message = InboundChat(
            tenant_id="t1", conversation_id="c1", activity_id="a1", scope="personal",
            text="what do you make of this?", attachments=[clip()],
            card_action={"action": "approve"},
        )
        cfg = SimpleNamespace(transcribe_voice_messages=True, managed_chat_gateway_reply_url=GATEWAY)
        query = asyncio.run(build_turn_query(message, cfg))
        assert query.splitlines() == [
            "what do you make of this?",
            f"[card button pressed - submit payload: {json.dumps({'action': 'approve'})}]",
            '[voice message "voice.ogg", transcribed]: read this out',
            "[attachment audio: voice.ogg at https://teams.standin.komaa.com/att/1]",
        ]

    def test_the_clip_keeps_its_attachment_placeholder_when_transcription_is_off(self):
        # The attachment note is built independently, so a clip is never silently absent from the
        # turn - whether the feature is off or the transcription failed.
        message = InboundChat(
            tenant_id="t1", conversation_id="c1", activity_id="a1", scope="personal",
            text="", attachments=[clip()],
        )
        cfg = SimpleNamespace(transcribe_voice_messages=False, managed_chat_gateway_reply_url=GATEWAY)
        query = asyncio.run(build_turn_query(message, cfg))
        assert query == "[attachment audio: voice.ogg at https://teams.standin.komaa.com/att/1]"


class TestEndToEndAgainstARealGateway:
    """The default opener, against a real HTTP server, through the real turn builder. Exercises the
    parts the injectable fake cannot: aiohttp response shape, redirect refusal, and the fact that
    off really does mean no request leaves the process."""

    def test_a_real_clip_becomes_words_in_the_turn(self):
        asyncio.run(self._scenario(enabled=True))

    def test_off_issues_no_request_at_all(self):
        asyncio.run(self._scenario(enabled=False))

    def test_a_redirecting_url_is_refused(self):
        asyncio.run(self._scenario(enabled=True, redirect=True))

    async def _scenario(self, *, enabled: bool, redirect: bool = False):
        from aiohttp import web

        served: list[str] = []

        async def clip_handler(request: web.Request):
            served.append(request.path)
            if redirect:
                # Same-host signed URL that redirects: already anomalous, and following it would
                # re-open the door the origin pin closed.
                raise web.HTTPFound("/att/elsewhere")
            return web.Response(body=b"OGGDATA", content_type="audio/ogg")

        app = web.Application()
        app.router.add_get("/att/{name}", clip_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        stt_calls: list[bytes] = []

        async def stt(path):
            stt_calls.append(Path(path).read_bytes())
            return "the transcribed words"

        original = voice_messages._hermes_transcribe
        voice_messages._hermes_transcribe = stt
        try:
            message = InboundChat(
                tenant_id="t1", conversation_id="c1", activity_id="a1", scope="personal",
                text="listen to this", attachments=[clip(url=f"http://127.0.0.1:{port}/att/1")],
            )
            cfg = SimpleNamespace(
                transcribe_voice_messages=enabled,
                managed_chat_gateway_reply_url=f"http://127.0.0.1:{port}/api/chat/reply",
            )
            query = await build_turn_query(message, cfg)
        finally:
            voice_messages._hermes_transcribe = original
            await runner.cleanup()

        # Whatever happened, the clip is still NAMED in the turn: the attachment note is built
        # independently, so a voice message is never silently absent.
        assert f"[attachment audio: voice.ogg at http://127.0.0.1:{port}/att/1]" in query

        if not enabled:
            assert served == [] and stt_calls == []
            assert "voice message" not in query
            return
        if redirect:
            assert served == ["/att/1"], "should have asked once and refused to follow"
            assert stt_calls == []
            # A clip that never arrived produces no voice line at all - the attachment note above is
            # its placeholder. The "could not be transcribed" line is for a clip that DID arrive.
            assert "voice message" not in query
            return
        assert stt_calls == [b"OGGDATA"]
        assert query.splitlines() == [
            "listen to this",
            '[voice message "voice.ogg", transcribed]: the transcribed words',
            f"[attachment audio: voice.ogg at http://127.0.0.1:{port}/att/1]",
        ]


class TestSchemaDrift:
    SCHEMA = (Path(__file__).parent.parent / "protocol" / "chat-schema.yaml").read_text()

    def test_audio_is_a_declared_attachment_kind(self):
        assert "values: [image, file, audio]" in self.SCHEMA

    def test_the_kind_enum_stays_open(self):
        # A peer that predates 'audio' relays the clip as an unknown kind: the feature is a silent
        # no-op there, which is correct, rather than a rejected message.
        block = self.SCHEMA.split("AttachmentKind:", 1)[1]
        assert "open: true" in block.split("values:", 1)[0]
