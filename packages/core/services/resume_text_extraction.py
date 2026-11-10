"""Plain-text extraction from an owner-uploaded DOCX master résumé.

Uses python-docx, already a runtime dependency (the renderer writes DOCX
with it); its XML parser does not resolve entities. Before parsing, the
archive itself is checked so a hostile or broken file cannot exhaust memory:
compressed size, entry count, total uncompressed size and per-entry
compression ratio are all bounded. Nothing is stored here: the text goes to
the owner's editable preview, and only lines the owner saves are imported
(still unverified, like any master résumé line).

PDF is deliberately not handled: the PDF parsers in this repository are
test-only dependencies, and adding one to the production image is an owner
decision.
"""

from __future__ import annotations

import io
import zipfile

from packages.shared.errors import ValidationError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_BYTES = 2_000_000
MAX_ENTRIES = 500
MAX_UNCOMPRESSED_BYTES = 25_000_000
MAX_RATIO = 100
MAX_TEXT_CHARS = 200_000


class ExtractionError(ValidationError):
    """The file is not a readable DOCX within the limits. Says why, never content."""


def _check_archive(data: bytes) -> None:
    if not data:
        raise ExtractionError("the file is empty")
    if len(data) > MAX_DOCX_BYTES:
        raise ExtractionError(f"the file is larger than {MAX_DOCX_BYTES // 1_000_000} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("not a DOCX file") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise ExtractionError("the document has too many parts")
        if sum(e.file_size for e in entries) > MAX_UNCOMPRESSED_BYTES:
            raise ExtractionError("the document expands to more than 25 MB")
        for entry in entries:
            if entry.compress_size and entry.file_size / entry.compress_size > MAX_RATIO:
                raise ExtractionError("the document is compressed suspiciously well")
        if "word/document.xml" not in {e.filename for e in entries}:
            raise ExtractionError("not a Word document (no word/document.xml)")


def extract_docx_text(data: bytes) -> str:
    """Paragraph and table text in reading order, one line per paragraph or
    table cell; blank lines dropped. Raises ExtractionError."""
    _check_archive(data)
    import docx
    from docx.oxml.ns import qn

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # malformed XML or parts: one opaque refusal
        raise ExtractionError("the document could not be read") from exc

    lines: list[str] = []

    def paragraph_text(element: object) -> str:
        return "".join(node.text or "" for node in element.iter(qn("w:t")))  # type: ignore[attr-defined]

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            lines.append(paragraph_text(child))
        elif child.tag == qn("w:tbl"):
            for cell_paragraph in child.iter(qn("w:p")):
                lines.append(paragraph_text(cell_paragraph))
    text = "\n".join(line.strip() for line in lines if line.strip())
    if not text:
        raise ExtractionError("no text was found in the document")
    return text[:MAX_TEXT_CHARS]
