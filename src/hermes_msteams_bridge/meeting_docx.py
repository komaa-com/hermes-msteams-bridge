"""Minimal, dependency-free .docx writer for meeting minutes.

A .docx is just a ZIP of OOXML parts; we emit the three required parts so the file
opens in Word/LibreOffice without needing python-docx. Used to produce a
Word-openable minutes artifact alongside the text posted to the Teams chat (cross-
process attachment to the chat itself remains text-only — see meeting.py).
"""

from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)


def _para(text: str, *, bold: bool = False) -> str:
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def write_minutes_docx(title: str, minutes_text: str, path: str) -> None:
    """Write ``minutes_text`` (markdown-ish) to a Word-openable .docx at ``path``."""
    paras = [_para(title, bold=True)]
    for raw in minutes_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bold = line.startswith("**") and line.endswith("**")
        paras.append(_para(line.strip("*").strip(), bold=bold))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(paras) + "<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
