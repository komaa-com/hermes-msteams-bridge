"""§3.1 show_file containment — adversarial first, rendering second — plus
show_web_page routing."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import pytest

from hermes_msteams_bridge.display_files import (
    IMAGE_EXTS,
    ShowFileError,
    render_for_display,
    resolve_contained,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def root(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "pics").mkdir()
    (ws / "pics" / "cat.png").write_bytes(PNG)
    (tmp_path / "outside.png").write_bytes(PNG)  # exists, but outside the root
    return ws


# ── containment: every escape dies ───────────────────────────────────────────


def test_happy_path_resolves(root):
    assert resolve_contained(root, "pics/cat.png").name == "cat.png"


@pytest.mark.parametrize(
    "attack",
    [
        "../outside.png",
        "pics/../../outside.png",
        "/etc/hosts",
        "/../outside.png",
        "C:\\Windows\\win.ini",
        "\\\\server\\share\\x.png",
        "~/x.png",
    ],
)
def test_traversal_and_absolute_paths_refused(root, attack):
    with pytest.raises(ShowFileError):
        resolve_contained(root, attack)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlink_escape_refused(root, tmp_path):
    (root / "sneaky.png").symlink_to(tmp_path / "outside.png")
    with pytest.raises(ShowFileError) as exc:
        resolve_contained(root, "sneaky.png")
    assert "outside" in exc.value.spoken


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "prod.pem",
        "id.key",
        "api_key_backup.png",
        "credentials.pdf",
        "my-secret-notes.pdf",
        ".git/config",
        ".ssh/known_hosts.png",
    ],
)
def test_sensitive_names_refused_even_inside_root(root, name):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)
    with pytest.raises(ShowFileError) as exc:
        resolve_contained(root, name)
    assert "sensitive" in exc.value.spoken


def test_extension_allowlist(root):
    (root / "run.sh").write_text("#!/bin/sh")
    with pytest.raises(ShowFileError):
        resolve_contained(root, "run.sh")


def test_size_cap(root, monkeypatch):
    import hermes_msteams_bridge.display_files as df

    monkeypatch.setattr(df, "MAX_FILE_MB", 0)
    with pytest.raises(ShowFileError) as exc:
        resolve_contained(root, "pics/cat.png")
    assert "large" in exc.value.spoken


def test_missing_and_empty(root):
    with pytest.raises(ShowFileError):
        resolve_contained(root, "pics/nope.png")
    with pytest.raises(ShowFileError):
        resolve_contained(root, "")


# ── rendering ────────────────────────────────────────────────────────────────


def test_image_passthrough_with_magic_check(root):
    data, mime, caption = render_for_display(root, "pics/cat.png")
    assert data == PNG and mime == "image/png" and caption == "cat.png"


def test_magic_mismatch_refused(root):
    (root / "fake.png").write_bytes(b"MZ not a png")
    with pytest.raises(ShowFileError) as exc:
        render_for_display(root, "fake.png")
    assert "claimed" in exc.value.spoken


def test_fake_pdf_refused(root):
    (root / "fake.pdf").write_bytes(b"not a pdf at all")
    with pytest.raises(ShowFileError):
        render_for_display(root, "fake.pdf")


def test_pdf_render_refuses_politely_without_pypdfium(root, monkeypatch):
    if "pypdfium2" in sys.modules or _importable("pypdfium2"):
        pytest.skip("pypdfium2 installed; refusal path not reachable")
    (root / "real.pdf").write_bytes(b"%PDF-1.4 minimal")
    with pytest.raises(ShowFileError) as exc:
        render_for_display(root, "real.pdf")
    assert "pypdfium2" in exc.value.spoken


def test_office_refuses_politely_without_soffice(root, monkeypatch):
    import hermes_msteams_bridge.display_files as df

    monkeypatch.setattr(df.shutil, "which", lambda name: None)
    (root / "deck.pptx").write_bytes(b"PK\x03\x04 fake zip")
    with pytest.raises(ShowFileError) as exc:
        render_for_display(root, "deck.pptx")
    assert "LibreOffice" in exc.value.spoken


def _importable(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


# ── the call tool wiring ─────────────────────────────────────────────────────


class _Session:
    closed = False

    def __init__(self):
        self.shown: list[dict] = []

    async def send_display_image(self, data_b64, mime, *, duration_ms=None, mode=None, caption=None):
        self.shown.append({"data": base64.b64decode(data_b64), "mime": mime, "caption": caption})


class _Bridge:
    def __init__(self, root):
        self.show_file_root = str(root)


def _runner(root):
    from hermes_msteams_bridge.call_tools import CallToolRunner

    class _Handler:
        _session = _Session()
        _bridge = _Bridge(root)
        _turn_id = 0

    return CallToolRunner(_Handler()), _Handler


def test_show_file_tool_shows_and_speaks(root):
    runner, handler = _runner(root)
    out = asyncio.run(runner.run_tool("show_file", {"path": "pics/cat.png"}))
    assert "showing cat.png" in out
    assert handler._session.shown[0]["mime"] == "image/png"


def test_show_file_tool_speaks_refusal(root):
    runner, handler = _runner(root)
    out = asyncio.run(runner.run_tool("show_file", {"path": "../outside.png"}))
    assert "workspace" in out
    assert handler._session.shown == []


def test_show_web_page_rejects_non_http(root):
    runner, handler = _runner(root)
    for bad in ("file:///etc/hosts", "javascript:alert(1)", "ftp://x", "not a url"):
        out = asyncio.run(runner.run_tool("show_web_page", {"url": bad}))
        assert "http" in out
    assert handler._session.shown == []


def test_show_web_page_happy_path(root, tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)

    import hermes_msteams_bridge.display_files as df
    import hermes_msteams_bridge.hermes_api as hermes_api

    monkeypatch.setattr(df, "url_is_public", lambda u: (True, ""))  # hermetic: no DNS

    async def fake_shot(url, task_id=""):
        assert url == "https://example.com/docs"
        assert task_id.startswith("msteams_bridge:")
        return str(shot), "rendered"

    monkeypatch.setattr(hermes_api, "browser_page_screenshot", fake_shot)
    runner, handler = _runner(root)
    out = asyncio.run(runner.run_tool("show_web_page", {"url": "https://example.com/docs"}))
    assert "on screen" in out
    assert handler._session.shown[0]["data"] == PNG


def test_real_pdf_renders_to_png(root):
    pdfium = pytest.importorskip("pypdfium2")  # runs under the documents extra
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 100)
    pdf_path = root / "real.pdf"
    doc.save(str(pdf_path))
    doc.close()

    data, mime, caption = render_for_display(root, "real.pdf")
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    assert caption == "real.pdf (page 1/1)"


def test_show_web_page_blocks_private_hosts(root, monkeypatch):
    import hermes_msteams_bridge.display_files as df

    runner, handler = _runner(root)
    monkeypatch.setattr(df, "url_is_public", lambda u: (False, "That address points inside a private network, so I won't browse it."))
    out = asyncio.run(runner.run_tool("show_web_page", {"url": "https://intranet.local/x"}))
    assert "private network" in out
    assert handler._session.shown == []


def test_url_policy_blocks_loopback_and_metadata():
    from hermes_msteams_bridge.display_files import url_is_public

    ok, why = url_is_public("https://127.0.0.1/x")
    assert not ok and "private" in why
    ok, _ = url_is_public("http://169.254.169.254/latest/meta-data/")
    assert not ok


def test_windows_drive_relative_and_backslash_denylist(root):
    with pytest.raises(ShowFileError):
        resolve_contained(root, "C:evil.png")
    target = root / ".ssh"
    target.mkdir(exist_ok=True)
    (target / "k.png").write_bytes(PNG)
    with pytest.raises(ShowFileError) as exc:
        resolve_contained(root, ".ssh\\k.png")
    assert "sensitive" in exc.value.spoken
