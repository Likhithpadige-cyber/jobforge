"""PDF rendering with ReportLab.

Real selectable text on a single column, never an image of text - an image is
unreadable to every ATS and to every screen reader, and a resume that cannot
be parsed is a resume that does not get read.

Determinism came down to one switch and one measurement. A default ReportLab
canvas writes `/CreationDate` from the clock and a random `/ID`, and two runs
in separate processes differ. `Canvas(..., invariant=1)` pins both: measured
here across three separate processes with a second between them, the output
was byte-identical, with `/CreationDate` fixed at `D:20000101000000+00'00'`.

Only the base-14 fonts are used, so nothing is embedded. Font subsetting is
the remaining realistic source of nondeterminism in a PDF - a subset built
from a set of glyphs can be serialised in an unstable order - and avoiding
embedding avoids the problem outright rather than betting on it.

Pagination is deterministic because layout is: a fixed frame, a fixed leading,
and text measured by ReportLab's own metrics for a fixed font. No widow or
orphan heuristics, no optimisation pass, no randomness.
"""

from __future__ import annotations

import io

from packages.services.rendering.document import ResumeDocument
from packages.services.rendering.protocol import RendererUnavailableError

PDF_CONTENT_TYPE = "application/pdf"

# --- fixed page geometry, in points -----------------------------------------
_PAGE_WIDTH, _PAGE_HEIGHT = 612.0, 792.0  # US Letter
_MARGIN = 54.0  # 0.75in
_BODY_FONT, _BODY_SIZE = "Helvetica", 10.0
_HEAD_FONT, _HEAD_SIZE = "Helvetica-Bold", 12.0
_LEADING = 13.0
_HEADING_SPACE_BEFORE = 10.0
_HEADING_SPACE_AFTER = 4.0
#: An ASCII hyphen, not U+2022. Measured, not assumed: with ReportLab's
#: base-14 Helvetica the real bullet glyph extracts as \x7f under pypdf and
#: as (cid:127) under pdfminer - two independent extractors agreeing that an
#: ATS would not see a bullet there. A control character at the front of every
#: achievement is worse than a plain hyphen, and a hyphen is the conventional
#: plain-text bullet anyway. Fixing it properly would mean registering a
#: WinAnsi-encoded font, which buys a nicer glyph at the cost of an embedded
#: subset - the one remaining realistic source of PDF nondeterminism.
_BULLET = "- "
_BULLET_INDENT = 12.0


class PdfResumeRenderer:
    """Single column, selectable text, identical bytes every time."""

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
        return True

    def render(self, document: object) -> bytes:
        if not isinstance(document, ResumeDocument):
            raise TypeError("PdfResumeRenderer requires a ResumeDocument")
        try:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            from reportlab.pdfgen import canvas
        except ImportError as exc:
            raise RendererUnavailableError("PDF rendering requires ReportLab") from exc

        buffer = io.BytesIO()
        # invariant=1 is what makes this reproducible: it pins /CreationDate,
        # /ModDate and the /ID array, which are otherwise the clock and a
        # random value.
        pdf = canvas.Canvas(buffer, pagesize=(_PAGE_WIDTH, _PAGE_HEIGHT), invariant=1)
        pdf.setTitle("")
        pdf.setAuthor("JobForge")
        pdf.setSubject("")
        pdf.setCreator("JobForge")

        usable = _PAGE_WIDTH - (2 * _MARGIN)
        y = _PAGE_HEIGHT - _MARGIN

        def new_page() -> float:
            pdf.showPage()
            return _PAGE_HEIGHT - _MARGIN

        def wrap(text: str, font: str, size: float, width: float) -> list[str]:
            """Greedy wrap on whitespace. Deterministic and not clever.

            A word longer than the line is emitted on its own rather than
            hyphenated: breaking a token would alter a string a human
            approved, which this renderer may not do even cosmetically.
            """
            lines: list[str] = []
            current = ""
            for word in text.split():
                candidate = f"{current} {word}".strip()
                if current and stringWidth(candidate, font, size) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            return lines or [""]

        for section in document.sections:
            if y - (_HEADING_SPACE_BEFORE + _HEAD_SIZE + _LEADING) < _MARGIN:
                y = new_page()
            y -= _HEADING_SPACE_BEFORE
            pdf.setFont(_HEAD_FONT, _HEAD_SIZE)
            pdf.drawString(_MARGIN, y, section.heading)
            y -= _HEAD_SIZE + _HEADING_SPACE_AFTER

            pdf.setFont(_BODY_FONT, _BODY_SIZE)
            for entry in section.entries:
                bullet = section.key != "summary"
                indent = _BULLET_INDENT if bullet else 0.0
                lines = wrap(entry.text, _BODY_FONT, _BODY_SIZE, usable - indent)
                for index, line in enumerate(lines):
                    if y - _LEADING < _MARGIN:
                        y = new_page()
                        pdf.setFont(_BODY_FONT, _BODY_SIZE)
                    if bullet and index == 0:
                        # The bullet is drawn as part of the same string, not
                        # as a separate one at its own x. Two text-showing
                        # operations on one visual line extract as two
                        # fragments, and a parser that sees a lone glyph on
                        # its own line can treat the bullet as content. One
                        # string keeps the line contiguous for an ATS.
                        pdf.drawString(_MARGIN, y, _BULLET + line)
                    else:
                        pdf.drawString(_MARGIN + indent, y, line)
                    y -= _LEADING

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
