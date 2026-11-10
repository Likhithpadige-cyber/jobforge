"""DOCX master-résumé text extraction: content order and archive limits."""

from __future__ import annotations

import io
import zipfile

import docx
import pytest

from packages.core.services import resume_text_extraction as rx

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def _docx() -> bytes:
    document = docx.Document()
    document.add_paragraph("Jordan Example")
    document.add_paragraph("Experience")
    document.add_paragraph("Administered 180 RHEL servers with Ansible.", style="List Bullet")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Skills"
    table.cell(0, 1).text = "Bash, Ansible, Git"
    document.add_paragraph("")
    document.add_paragraph("Education")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_extracts_paragraphs_and_table_cells_in_reading_order() -> None:
    assert rx.extract_docx_text(_docx()).splitlines() == [
        "Jordan Example",
        "Experience",
        "Administered 180 RHEL servers with Ansible.",
        "Skills",
        "Bash, Ansible, Git",
        "Education",
    ]


def _zip(entries: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"%PDF-1.7 not a docx", "not a DOCX"),
        (b"x" * (rx.MAX_DOCX_BYTES + 1), "larger than"),
        (_zip({"hello.txt": b"hi"}), "no word/document.xml"),
        (_zip({"word/document.xml": b"\0" * 5_000_000}), "compressed suspiciously"),
        (
            _zip({f"p{i}.xml": b"x" for i in range(rx.MAX_ENTRIES + 1)}, zipfile.ZIP_STORED),
            "too many parts",
        ),
        (
            _zip({"word/document.xml": b"<not xml", "[Content_Types].xml": b"<x/>"}),
            "could not be read",
        ),
    ],
)
def test_refuses_hostile_or_invalid_files(data: bytes, message: str) -> None:
    with pytest.raises(rx.ExtractionError, match=message):
        rx.extract_docx_text(data)


def test_external_entities_are_not_resolved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An XXE payload must not pull a local file's contents into the text."""
    secret = tmp_path / "secret.txt"
    secret.write_text("CANARY-XXE-SECRET")
    raw = io.BytesIO(_docx())
    with zipfile.ZipFile(raw) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    xml = parts["word/document.xml"].decode()
    xml = xml.replace(
        "<w:document", f'<!DOCTYPE d [<!ENTITY x SYSTEM "file://{secret}">]><w:document', 1
    ).replace("Jordan Example", "&x;", 1)
    parts["word/document.xml"] = xml.encode()
    try:
        text = rx.extract_docx_text(_zip(parts))
    except rx.ExtractionError:
        return  # refusing the document is also safe
    assert "CANARY-XXE-SECRET" not in text


def test_empty_document_is_refused() -> None:
    out = io.BytesIO()
    docx.Document().save(out)
    with pytest.raises(rx.ExtractionError, match="no text"):
        rx.extract_docx_text(out.getvalue())
