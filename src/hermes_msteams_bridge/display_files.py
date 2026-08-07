"""``show_file`` — render a real workspace file for the bot's video tile (§3.1).

Security model first, pixels second. The tool is driven by a voice model that
is in turn driven by whatever a caller says out loud, so without containment
it is an arbitrary-file-read that paints secrets onto a Teams tile. Every
lookup goes through :func:`resolve_contained`:

* paths are **workspace-relative only** — absolute paths and drive/UNC forms
  are rejected before any filesystem touch;
* the resolved real path must stay inside the configured root (symlink
  escapes and ``..`` both die on the same check);
* a sensitive-name denylist applies to every path component even inside the
  root (``.env``, keys, certs, ``.git``, credential stores);
* extension allowlist + content sniffing (magic numbers must agree with the
  claimed type) + a hard size cap.

Rendering mirrors the host's own document toolchain (the Hermes pdf/docx
skills): **pypdfium2** (PDFium, Apache/BSD - upstream's explicit PyMuPDF
replacement) renders PDF pages to PNG, and **LibreOffice** (``soffice
--headless --convert-to pdf``) converts Office documents to PDF first - the
exact pipeline the docx skill documents for visual verification. Images pass
through untouched. Both renderers are optional: pypdfium2 via the
``documents`` extra, soffice on PATH (the same pattern as ffmpeg for
streaming mode); when absent the tool refuses with a spoken, actionable
reason instead of failing silently.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

logger = logging.getLogger(__name__)

# Types we will show. Office formats go through soffice -> PDF -> pypdfium2.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
ALLOWED_EXTS = IMAGE_EXTS | PDF_EXTS | OFFICE_EXTS

MAX_FILE_MB = 25
RENDER_SCALE = 2.0  # PDFium scale: crisp enough for a 640x360 tile at 2x
SOFFICE_TIMEOUT_S = 60

# Any path component matching one of these is refused even inside the root.
_DENY_COMPONENTS = {".env", ".git", ".ssh", ".aws", ".azure", ".gnupg"}
_DENY_SUBSTRINGS = ("secret", "credential", "password", "token", "apikey", "api_key")
_DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/gif": b"GIF8",
    "application/pdf": b"%PDF",
}


@dataclass(frozen=True)
class ShowFileError(Exception):
    """Refusal with a short reason suitable to be spoken to the caller."""

    spoken: str

    def __str__(self) -> str:  # pragma: no cover — repr convenience
        return self.spoken


def _component_denied(name: str) -> bool:
    lowered = name.lower()
    if lowered in _DENY_COMPONENTS:
        return True
    if any(lowered.endswith(sfx) for sfx in _DENY_SUFFIXES):
        return True
    return any(sub in lowered for sub in _DENY_SUBSTRINGS)


def resolve_contained(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` strictly inside ``root`` or raise :class:`ShowFileError`."""
    raw = (rel_path or "").strip()
    if not raw:
        raise ShowFileError("I need a file name to show.")
    # Reject absolute paths in EITHER convention before touching the fs —
    # PureWindowsPath catches drive letters and UNC on any host OS.
    win = PureWindowsPath(raw)
    if PurePosixPath(raw).is_absolute() or win.is_absolute() or win.drive:
        # win.drive also catches drive-RELATIVE forms like "C:foo.png".
        raise ShowFileError("I can only show files inside my workspace, by relative path.")
    # Scan components under BOTH separator conventions so "..\.ssh\x.png"
    # cannot slip past the denylist on Windows hosts.
    for part in set(PurePosixPath(raw).parts) | set(win.parts):
        for piece in str(part).replace("\\", "/").split("/"):
            if piece and _component_denied(piece):
                raise ShowFileError("That file looks sensitive, so I won't display it.")
    root_real = root.resolve()
    candidate = (root_real / raw).resolve()  # collapses ../ and follows symlinks
    if candidate == root_real or root_real not in candidate.parents:
        raise ShowFileError("That path leads outside my workspace, so I won't display it.")
    if not candidate.is_file():
        raise ShowFileError("I can't find that file in my workspace.")
    if candidate.suffix.lower() not in ALLOWED_EXTS:
        raise ShowFileError("I can't display that file type on screen.")
    if candidate.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise ShowFileError("That file is too large to display.")
    return candidate


def _sniff_ok(data: bytes, mime: str) -> bool:
    magic = _MAGIC.get(mime)
    if magic is not None:
        return data.startswith(magic)
    if mime == "image/jpeg":
        return data[:2] == b"\xff\xd8"
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "image/bmp":
        return data[:2] == b"BM"
    return True  # office zips are validated by conversion instead


def _image_mime(ext: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }[ext]


def _render_pdf_page(pdf_path: Path, page: int) -> tuple[bytes, int]:
    """(PNG bytes of ``page`` [1-based], total page count) via pypdfium2."""
    try:
        import pypdfium2 as pdfium  # optional: the `documents` extra
    except ImportError as exc:
        raise ShowFileError(
            "I can't render documents on this install — the pypdfium2 extra is missing."
        ) from exc
    import io

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(doc)
        index = min(max(page, 1), total) - 1
        pg = doc[index]
        # A PDF may declare enormous page dimensions; cap the rendered bitmap
        # at ~8 MP so scale * page size cannot balloon memory or the frame.
        w, h = pg.get_size()
        scale = RENDER_SCALE
        if w > 0 and h > 0:
            import math

            max_px = 8_000_000
            if (w * scale) * (h * scale) > max_px:
                scale = math.sqrt(max_px / (w * h))
        bitmap = pg.render(scale=scale)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue(), total
    finally:
        doc.close()


def _office_to_pdf(path: Path, workdir: Path) -> Path:
    """Convert an Office document to PDF with headless LibreOffice — the same
    pipeline the Hermes docx skill uses for visual verification."""
    soffice = shutil.which("soffice")
    if soffice is None:
        raise ShowFileError(
            "I can't display Office documents on this install — LibreOffice is missing."
        )
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(workdir), str(path)],
        capture_output=True,
        timeout=SOFFICE_TIMEOUT_S,
    )
    produced = workdir / (path.stem + ".pdf")
    if result.returncode != 0 or not produced.is_file():
        logger.warning(
            "[teams_voice] soffice convert failed rc=%s: %s",
            result.returncode, (result.stderr or b"")[:400],
        )
        raise ShowFileError("I couldn't convert that document for display.")
    return produced


def render_for_display(root: Path, rel_path: str, page: int = 1) -> tuple[bytes, str, str]:
    """Containment + render: ``(bytes, mime, caption)`` or :class:`ShowFileError`.

    Images return their own bytes (magic-checked against the extension);
    PDFs return the requested page as PNG; Office documents convert to PDF
    first. Synchronous and CPU/subprocess-bound — call via a worker thread.
    """
    path = resolve_contained(root, rel_path)
    ext = path.suffix.lower()

    if ext in IMAGE_EXTS:
        data = path.read_bytes()
        mime = _image_mime(ext)
        if not _sniff_ok(data, mime):
            raise ShowFileError("That file doesn't match its claimed image type, so I won't display it.")
        return data, mime, path.name

    if ext in PDF_EXTS:
        if not _sniff_ok(path.read_bytes()[:8], "application/pdf"):
            raise ShowFileError("That file doesn't look like a real PDF, so I won't display it.")
        png, total = _render_pdf_page(path, page)
        return png, "image/png", f"{path.name} (page {min(max(page, 1), total)}/{total})"

    # Office: convert in an ephemeral dir, then render page 1..n of the PDF.
    with tempfile.TemporaryDirectory(prefix="teams_voice_show_") as tmp:
        pdf = _office_to_pdf(path, Path(tmp))
        png, total = _render_pdf_page(pdf, page)
        return png, "image/png", f"{path.name} (page {min(max(page, 1), total)}/{total})"


# ── show_web_page URL policy (§3.1 / review round 5) ─────────────────────────
#
# The Hermes browser deliberately relaxes its private-network gate for local
# backends (its threat model assumes the URL author already has local shell
# access). That assumption breaks here: our URL author is a REMOTE Teams
# caller. So the plugin enforces its own resolve-time policy before any
# dispatch: http(s) only, and every resolved address must be publicly
# routable — loopback, RFC1918, link-local, CGNAT, ULA, and cloud metadata
# ranges are refused. (Residual risk: post-navigation redirects inside the
# browser are out of our reach; this gate bounds the entry point.)

_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


def url_is_public(url: str) -> tuple[bool, str]:
    """(ok, spoken_reason). Resolves the host and vets every address."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "I couldn't parse that address."
    if parsed.scheme not in ("http", "https"):
        return False, "I can only show regular web pages (http or https)."
    host = parsed.hostname or ""
    if not host:
        return False, "That address has no host name."
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "I couldn't resolve that address."
    for info in infos:
        addr = str(info[4][0])
        if addr in _METADATA_IPS:
            return False, "That address points at infrastructure I won't browse."
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, "That address resolved somewhere I can't verify."
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False, "That address points inside a private network, so I won't browse it."
    return True, ""
