"""The DOCX renderer, against python-docx itself.

Everything here opens the produced file and inspects it. A test that only
checked the renderer returned bytes would pass for a corrupt archive.

Synthetic content only.
"""

from __future__ import annotations

import io
import sys
import zipfile

import pytest

from packages.services.rendering.document import ResumeDocument, build_document
from packages.services.rendering.docx import (
    DETERMINISTIC_ZIP_TIMESTAMP,
    DocxResumeRenderer,
)
from packages.services.rendering.protocol import TEMPLATE_VERSION, RendererUnavailableError
from tests.unit.renderer_deps import require_dependency

pytestmark = pytest.mark.unit

require_dependency("docx", "python-docx", "requirements.txt")

RESUME_ID = "0192f000-0000-7000-8000-000000000001"
CONTENT = {
    "summary": {"0": {"text": "Synthetic summary line.", "order": 0}},
    "experience": {
        "0": {"text": "Ran a synthetic migration.", "order": 0},
        "1": {"text": "Reduced a synthetic number from 40 to 12.", "order": 1},
    },
    "skills": {"0": {"text": "Synthetic skill A, synthetic skill B.", "order": 0}},
}


def document() -> ResumeDocument:
    return build_document(resume_id=RESUME_ID, resume_version=2, content=CONTENT)


def rendered() -> bytes:
    return DocxResumeRenderer(TEMPLATE_VERSION).render(document())


def reopened() -> object:
    import docx

    return docx.Document(io.BytesIO(rendered()))


# --------------------------------------------------- the file is a real DOCX
def test_the_normalised_archive_still_opens() -> None:
    """The safety check on normalisation.

    `_normalise` repacks the ZIP without touching any entry's bytes, so it
    should be incapable of corrupting OOXML - but "should be" is not a test.
    """
    assert reopened() is not None


def test_every_zip_entry_carries_the_fixed_timestamp() -> None:
    """The one genuinely nondeterministic thing python-docx emits.

    Measured, not assumed: an unnormalised save stamps every entry with the
    wall clock, so two renders a second apart differ.
    """
    archive = zipfile.ZipFile(io.BytesIO(rendered()))
    stamps = {archive.getinfo(n).date_time for n in archive.namelist()}
    assert stamps == {DETERMINISTIC_ZIP_TIMESTAMP}


def test_core_properties_carry_no_clock() -> None:
    props = reopened().core_properties
    assert props.created.year == 1980
    assert props.modified.year == 1980
    assert props.revision == 1


# ------------------------------------------------------------ the ATS shape
def test_no_tables_are_used_for_layout() -> None:
    """A table is the classic way to make a resume unparseable."""
    assert len(reopened().tables) == 0


def test_no_text_boxes() -> None:
    archive = zipfile.ZipFile(io.BytesIO(rendered()))
    body = archive.read("word/document.xml")
    assert b"txbxContent" not in body
    assert b"<w:pict" not in body


def test_headers_and_footers_carry_no_content() -> None:
    """Contact details in a header are invisible to many parsers."""
    doc = reopened()
    for section in doc.sections:
        assert not [p.text for p in section.header.paragraphs if p.text.strip()]
        assert not [p.text for p in section.footer.paragraphs if p.text.strip()]


def test_headings_use_a_heading_style_and_bullets_a_list_style() -> None:
    doc = reopened()
    styled = [(p.style.name, p.text) for p in doc.paragraphs if p.text.strip()]
    assert ("Heading 1", "Summary") in styled
    assert ("Heading 1", "Experience") in styled
    assert ("List Bullet", "Ran a synthetic migration.") in styled


def test_the_summary_is_body_text_not_a_bullet() -> None:
    """A one-line summary rendered as a bullet reads as an achievement."""
    doc = reopened()
    summary = [p for p in doc.paragraphs if p.text == "Synthetic summary line."]
    assert summary and summary[0].style.name == "Body Text"


# ------------------------------------------------------------- the content
def test_every_approved_line_appears_exactly_once() -> None:
    doc = reopened()
    text = [p.text for p in doc.paragraphs if p.text.strip()]
    for section in CONTENT.values():
        for entry in section.values():
            assert text.count(entry["text"]) == 1


def test_text_is_not_reflowed_or_retouched() -> None:
    """Verbatim means verbatim, including awkward spacing."""
    odd = {"summary": {"0": {"text": "  spaced   out  text  ", "order": 0}}}
    doc = build_document(resume_id=RESUME_ID, resume_version=1, content=odd)
    import docx

    reopened_doc = docx.Document(
        io.BytesIO(DocxResumeRenderer(TEMPLATE_VERSION).render(doc))
    )
    assert any(
        p.text == "  spaced   out  text  " for p in reopened_doc.paragraphs
    )


def test_section_order_follows_the_canonical_document() -> None:
    doc = reopened()
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Summary", "Skills", "Experience"]


# ------------------------------------------------ fail closed, never degrade
def test_a_missing_library_raises_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer refuses when python-docx is absent.

    A text file carrying a .docx name is refused by Word and parsed as
    garbage by an ATS, so a renderer that cannot produce OOXML has to fail
    the render rather than hand back something with the right extension.
    `render_resume` only consults `available()`, which makes the guard inside
    `render()` the last line of defence and worth asserting on its own.

    The absence is simulated rather than arranged by uninstalling anything:
    poisoning the entry in `sys.modules` makes the guarded import raise
    exactly the ImportError a machine without the library would raise, and
    monkeypatch restores it afterwards.
    """
    renderer = DocxResumeRenderer(TEMPLATE_VERSION)
    monkeypatch.setitem(sys.modules, "docx", None)

    assert renderer.available() is False
    with pytest.raises(RendererUnavailableError):
        renderer.render(document())
