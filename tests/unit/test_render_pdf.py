"""The PDF renderer, checked with two independent text extractors.

A resume an ATS cannot read is a resume nobody reads, so the assertions here
are about extraction rather than appearance. Using both pypdf and pdfminer is
deliberate: when they disagreed about the bullet glyph, that disagreement was
the finding.

Synthetic content only.
"""

from __future__ import annotations

import io
import sys

import pytest

from packages.services.rendering.document import ResumeDocument, build_document
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.services.rendering.protocol import TEMPLATE_VERSION, RendererUnavailableError
from tests.unit.renderer_deps import require_dependency

pytestmark = pytest.mark.unit

require_dependency("reportlab", "reportlab", "requirements.txt")
require_dependency("pypdf", "pypdf", "requirements-dev.txt")
require_dependency("pdfminer", "pdfminer.six", "requirements-dev.txt")

RESUME_ID = "0192f000-0000-7000-8000-000000000001"
LINES = {
    "summary": ["Synthetic summary line."],
    "experience": [
        "Ran a synthetic migration.",
        "Reduced a synthetic number from 40 to 12.",
    ],
    "skills": ["Synthetic skill A, synthetic skill B."],
}
CONTENT = {
    key: {str(i): {"text": t, "order": i} for i, t in enumerate(texts)}
    for key, texts in LINES.items()
}


def document(content: dict | None = None) -> ResumeDocument:
    return build_document(resume_id=RESUME_ID, resume_version=2, content=content or CONTENT)


def rendered(content: dict | None = None) -> bytes:
    return PdfResumeRenderer(TEMPLATE_VERSION).render(document(content))


def by_pypdf(raw: bytes) -> str:
    import pypdf

    return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def by_pdfminer(raw: bytes) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(raw))


# ------------------------------------------------------------- extractability
@pytest.mark.parametrize("extract", [by_pypdf, by_pdfminer], ids=["pypdf", "pdfminer"])
def test_every_approved_line_is_extractable(extract: object) -> None:
    text = extract(rendered())
    for texts in LINES.values():
        for line in texts:
            assert line in text, f"{line!r} did not survive extraction"


@pytest.mark.parametrize("extract", [by_pypdf, by_pdfminer], ids=["pypdf", "pdfminer"])
def test_headings_are_extractable(extract: object) -> None:
    text = extract(rendered())
    for heading in ("Summary", "Experience", "Skills"):
        assert heading in text


@pytest.mark.parametrize("extract", [by_pypdf, by_pdfminer], ids=["pypdf", "pdfminer"])
def test_the_extracted_text_has_no_control_characters(extract: object) -> None:
    """The regression test for the bullet.

    U+2022 in ReportLab's base-14 Helvetica extracted as \\x7f under pypdf and
    as (cid:127) under pdfminer. A control character at the front of every
    achievement is exactly the kind of thing that survives review and breaks a
    parser, so it is asserted against rather than remembered.
    """
    text = extract(rendered())
    assert "(cid:" not in text
    bad = [c for c in text if ord(c) < 32 and c not in "\n\r\t\x0c"]
    assert bad == [], f"control characters in extracted text: {bad!r}"


@pytest.mark.parametrize("extract", [by_pypdf, by_pdfminer], ids=["pypdf", "pdfminer"])
def test_bullets_extract_as_ascii_and_stay_on_their_line(extract: object) -> None:
    """A bullet drawn as its own string extracts as a lone fragment, which a
    parser can read as content. It is joined to its line instead."""
    text = extract(rendered())
    assert "- Ran a synthetic migration." in text


def test_the_text_is_real_text_and_not_an_image() -> None:
    """An image of text is unreadable to every ATS and every screen reader.

    Asserted against image *XObjects*, not against the bytes `/Image`:
    `/ImageB /ImageC /ImageI` appear in every ReportLab page as legacy
    `/ProcSet` entries and mean nothing. Grepping for them would have failed
    a perfectly text-only document, which is how a test teaches people to
    weaken it.
    """
    import pypdf

    raw = rendered()
    assert b"/Subtype /Image" not in raw and b"/Subtype/Image" not in raw
    assert b"/DCTDecode" not in raw
    page = pypdf.PdfReader(io.BytesIO(raw)).pages[0]
    assert "/XObject" not in page.get("/Resources", {})
    assert by_pypdf(raw).strip() != ""


# --------------------------------------------------------------- determinism
def test_document_metadata_is_pinned() -> None:
    import pypdf

    meta = pypdf.PdfReader(io.BytesIO(rendered())).metadata
    assert str(meta["/CreationDate"]).startswith("D:20000101000000")
    assert str(meta["/ModDate"]).startswith("D:20000101000000")
    assert str(meta["/Creator"]) == "JobForge"


def test_the_document_id_does_not_vary() -> None:
    """`/ID` is a random value by default and is the thing `invariant=1`
    pins. Two renders sharing one is the whole point."""
    import re

    first, second = rendered(), rendered()
    ids = [re.findall(rb"/ID\s*\[([^\]]+)\]", raw) for raw in (first, second)]
    assert ids[0] == ids[1]
    assert ids[0], "no /ID array found"


# ---------------------------------------------------------------- pagination
def test_a_long_document_paginates_deterministically() -> None:
    import pypdf

    many = {
        "experience": {
            str(i): {"text": f"Synthetic achievement number {i}.", "order": i} for i in range(120)
        }
    }
    first, second = rendered(many), rendered(many)
    assert first == second
    pages = len(pypdf.PdfReader(io.BytesIO(first)).pages)
    assert pages > 1, "the fixture should have spilled onto a second page"


def test_a_very_long_line_wraps_without_altering_words() -> None:
    """Wrapping may not hyphenate: breaking a token would alter a string a
    human approved."""
    word = "Supercalifragilistic" * 6
    content = {"experience": {"0": {"text": word, "order": 0}}}
    text = by_pdfminer(rendered(content))
    assert word in text.replace("\n", "")


# ------------------------------------------------ fail closed, never degrade
def test_a_missing_library_raises_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer refuses when ReportLab is absent.

    A PDF is what an employer opens and what an ATS parses; a renderer that
    cannot produce one must fail rather than return bytes that merely carry
    the extension. `render_resume` only consults `available()`, so the guard
    inside `render()` is the last line of defence and is asserted here.

    Every module name the renderer imports is poisoned, not just the package
    root: `reportlab.pdfgen` and `reportlab.pdfbase.pdfmetrics` are already
    in `sys.modules` by this point, and a cached submodule is returned
    without the parent being consulted.
    """
    renderer = PdfResumeRenderer(TEMPLATE_VERSION)
    for name in ("reportlab", "reportlab.pdfgen", "reportlab.pdfbase.pdfmetrics"):
        monkeypatch.setitem(sys.modules, name, None)

    assert renderer.available() is False
    with pytest.raises(RendererUnavailableError):
        renderer.render(document())
