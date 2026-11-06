"""Deterministic PDF with embedded Unicode glyphs and measured, bounded lines.

Unsupported glyphs and scripts needing shaping fail explicitly; no replacement
boxes may reach an approved export. Font bytes and template identity are pinned.
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from threading import Lock
from typing import Any

from packages.services.rendering.document import DocumentEntry, ResumeDocument
from packages.services.rendering.protocol import RendererUnavailableError

PDF_CONTENT_TYPE = "application/pdf"
_PAGE_WIDTH, _PAGE_HEIGHT = 612.0, 792.0
_MARGIN = 54.0
_BODY_FONT, _BODY_SIZE = "JobForgeNoto", 10.0
_HEAD_FONT, _HEAD_SIZE = _BODY_FONT, 12.0
_LEADING = 14.0
_FONT_PATH = Path(__file__).with_name("fonts") / "NotoSans-Regular.ttf"
_FONT_LOCK = Lock()


def _font() -> Any:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with _FONT_LOCK:
        if _BODY_FONT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_BODY_FONT, str(_FONT_PATH)))
        return pdfmetrics.getFont(_BODY_FONT)


def validate_text(text: str) -> None:
    """Fail before upload for missing glyphs, controls and unsupported shaping."""
    font = _font()
    glyphs = font.face.charToGlyph
    for char in text:
        code = ord(char)
        if char in "\n\r\t":
            continue
        if (
            unicodedata.combining(char) != 0
            or code < 32
            or 0x7F <= code < 0xA0
            or code not in glyphs
            or 0x0590 <= code <= 0x109F
            or 0x200B <= code <= 0x200F
            or 0x202A <= code <= 0x202E
            or 0x2066 <= code <= 0x2069
        ):
            raise RendererUnavailableError(
                f"Unsupported export character U+{code:04X}; "
                "revise text or add a tested font/shaping implementation"
            )


def wrap_text(text: str, width: float, size: float = _BODY_SIZE) -> list[str]:
    """Break long tokens without inserting hyphens or dropping characters."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    _font()
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        remaining = paragraph
        while stringWidth(remaining, _BODY_FONT, size) > width:
            end = 0
            for index in range(1, len(remaining) + 1):
                if stringWidth(remaining[:index], _BODY_FONT, size) > width:
                    break
                end = index
            if end == 0:
                raise RendererUnavailableError("A glyph exceeds the available line width")
            space = remaining.rfind(" ", 0, end + 1)
            cut = space if space > 0 else end
            lines.append(remaining[:cut])
            remaining = remaining[cut + 1 :] if space > 0 else remaining[cut:]
        lines.append(remaining)
    return lines


class PdfResumeRenderer:
    kind = "resume_pdf"
    extension = "pdf"
    content_type = PDF_CONTENT_TYPE
    production = True
    implemented = True

    def __init__(self, template_version: str) -> None:
        self.template_version = template_version

    def available(self) -> bool:
        try:
            import reportlab  # noqa: F401
        except ImportError:
            return False
        return _FONT_PATH.is_file()

    def render(self, document: object) -> bytes:
        if not isinstance(document, ResumeDocument):
            raise TypeError("PdfResumeRenderer requires a ResumeDocument")
        try:
            from reportlab.pdfgen import canvas
        except ImportError as exc:
            raise RendererUnavailableError("PDF rendering requires ReportLab") from exc
        validate_text("\n".join(document.all_text()))
        validate_text("\n".join(section.heading for section in document.sections))
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(_PAGE_WIDTH, _PAGE_HEIGHT), invariant=1)
        pdf.setTitle("")
        pdf.setAuthor("JobForge")
        pdf.setSubject("")
        pdf.setCreator("JobForge")
        y = _PAGE_HEIGHT - _MARGIN
        width = _PAGE_WIDTH - 2 * _MARGIN

        def room(height: float) -> None:
            nonlocal y
            if y - height < _MARGIN:
                pdf.showPage()
                y = _PAGE_HEIGHT - _MARGIN

        def draw(
            text: str, size: float = _BODY_SIZE, bullet: bool = False, keep: bool = False
        ) -> None:
            nonlocal y
            indent = 12.0 if bullet else 0.0
            lines = wrap_text(text, width - indent, size)
            room(min(len(lines) + int(keep), 4) * _LEADING)
            for index, line in enumerate(lines):
                room(_LEADING)
                pdf.setFont(_BODY_FONT, size)
                prefix = "- " if bullet and index == 0 else ""
                pdf.drawString(_MARGIN if prefix else _MARGIN + indent, y, prefix + line)
                y -= _LEADING

        for index, line in enumerate(document.contact_lines):
            draw(line, 16 if index == 0 else _BODY_SIZE, keep=True)

        def entry_height(entry: DocumentEntry) -> float:
            indent = 12 if not entry.heading else 0
            return min(len(wrap_text(entry.text, width - indent)), 4) * _LEADING

        for section in document.sections:
            first = section.entries[0]
            reserve = 10 + _LEADING + entry_height(first)
            if first.heading and len(section.entries) > 1:
                reserve += entry_height(section.entries[1])
            room(reserve)
            y -= 10
            draw(section.heading, _HEAD_SIZE, keep=True)
            for index, entry in enumerate(section.entries):
                if entry.heading and index + 1 < len(section.entries):
                    room(entry_height(entry) + entry_height(section.entries[index + 1]))
                draw(
                    entry.text,
                    _BODY_SIZE,
                    bullet=section.key != "summary" and not entry.heading,
                    keep=entry.heading and index + 1 < len(section.entries),
                )
        pdf.save()
        return buffer.getvalue()
