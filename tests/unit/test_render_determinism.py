"""The determinism harness.

The requirement, stated once:

    same canonical content + same resume version + same template version
        -> byte-identical artifact

This file is the arbiter of that claim. It is written against the
`ResumeRenderer` protocol rather than against a particular renderer, so when
the real DOCX and PDF implementations land they are checked by exactly these
assertions with no rewriting. Today it runs against the reference text
renderer, which proves the harness itself works and that the canonical
document feeding a renderer is stable.

Both production bodies are now implemented, so these assertions apply to the
real DOCX and PDF output. Because a filtered-to-empty renderer list would
make every parametrised case below pass vacuously, the set under test is
asserted against `production_renderers()` rather than left to whatever
happens to import.
"""

from __future__ import annotations

import pytest

from packages.services.rendering import production_renderers
from packages.services.rendering.document import ResumeDocument, build_document
from packages.services.rendering.docx import DocxResumeRenderer
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.services.rendering.protocol import (
    ALLOWED_CONTENT_TYPES,
    RESUME_ARTIFACT_KINDS,
    TEMPLATE_VERSION,
    RenderedArtifact,
    ResumeRenderer,
)
from packages.services.rendering.reference import ReferenceTextRenderer
from tests.unit.renderer_deps import require_dependency

pytestmark = pytest.mark.unit

require_dependency("docx", "python-docx", "requirements.txt")
require_dependency("reportlab", "reportlab", "requirements.txt")

RESUME_ID = "0192f000-0000-7000-8000-000000000001"
CONTENT = {
    "summary": {"0": {"text": "A synthetic summary line.", "fact_ids": ["f0"], "order": 0}},
    "experience": {
        "0": {"text": "First synthetic bullet.", "fact_ids": ["f1"], "order": 0},
        "1": {"text": "Second synthetic bullet.", "fact_ids": ["f2"], "order": 1},
    },
}


def document(version: int = 3) -> ResumeDocument:
    return build_document(resume_id=RESUME_ID, resume_version=version, content=CONTENT)


def renderers_under_test() -> list[ResumeRenderer]:
    """Every renderer whose dependency is actually installed.

    A renderer qualifies only when its dependency is installed AND its body
    has been written. python-docx and ReportLab are installed in some
    environments while their renderer bodies are still declared boundaries;
    including those would assert determinism over a function that raises.

    Flipping `implemented` to True is all it takes for the assertions below
    to start applying to a real renderer - nobody has to remember to add it.
    """
    candidates: list[ResumeRenderer] = [
        ReferenceTextRenderer(TEMPLATE_VERSION),
        DocxResumeRenderer(TEMPLATE_VERSION),
        PdfResumeRenderer(TEMPLATE_VERSION),
    ]
    return [r for r in candidates if r.available() and r.implemented]


def _artifact(renderer: ResumeRenderer, doc: ResumeDocument) -> RenderedArtifact:
    return RenderedArtifact(
        kind=renderer.kind,
        content_type=renderer.content_type,
        extension=renderer.extension,
        template_version=renderer.template_version,
        data=renderer.render(doc),
    )


# ------------------------------------------------------------- the invariant
def test_the_production_renderers_are_exactly_docx_and_pdf() -> None:
    """The set this milestone ships, named explicitly.

    Without this, a rename, a dropped export or an uninstalled dependency
    could shrink `production_renderers()` to nothing and every determinism
    case below would pass over an empty list - a green suite asserting about
    no documents at all. Naming the kinds makes that failure loud.
    """
    produced = production_renderers()
    assert {r.kind for r in produced} == {"resume_docx", "resume_pdf"}
    for renderer in produced:
        assert renderer.production, f"{renderer.kind} is not a production renderer"
        assert renderer.implemented, f"{renderer.kind} has no implementation"
        assert renderer.available(), f"{renderer.kind} has no usable dependency"


def test_both_production_renderers_are_exercised_here() -> None:
    """Guards against a vacuous pass if a renderer became unavailable."""
    exercised = {r.kind for r in renderers_under_test()}
    assert {"resume_docx", "resume_pdf"} <= exercised, (
        f"production renderers missing from the determinism run: {exercised}"
    )


@pytest.mark.parametrize("renderer", renderers_under_test(), ids=lambda r: r.kind)
def test_the_same_document_renders_byte_identically(renderer: ResumeRenderer) -> None:
    first = _artifact(renderer, document())
    second = _artifact(renderer, document())
    assert first.data == second.data
    assert first.sha256 == second.sha256


@pytest.mark.parametrize("renderer", renderers_under_test(), ids=lambda r: r.kind)
def test_a_fresh_renderer_instance_produces_the_same_bytes(renderer: ResumeRenderer) -> None:
    """No instance-level state may leak into output."""
    twin = type(renderer)(renderer.template_version)
    assert _artifact(renderer, document()).data == _artifact(twin, document()).data


@pytest.mark.parametrize("renderer", renderers_under_test(), ids=lambda r: r.kind)
def test_changed_content_changes_the_bytes(renderer: ResumeRenderer) -> None:
    """The other half of determinism: identical output for different input
    would mean the renderer is ignoring the document."""
    other = build_document(
        resume_id=RESUME_ID,
        resume_version=3,
        content={"summary": {"0": {"text": "A different line.", "order": 0}}},
    )
    assert _artifact(renderer, document()).data != _artifact(renderer, other).data


@pytest.mark.parametrize("renderer", renderers_under_test(), ids=lambda r: r.kind)
def test_no_wall_clock_leaks_into_the_bytes(renderer: ResumeRenderer) -> None:
    """Renders separated in time must still match.

    A timestamp written into a document is the single most common reason
    "identical" artifacts differ, and it is invisible until someone diffs two
    files that should have been the same.
    """
    import time

    first = _artifact(renderer, document()).data
    time.sleep(0.05)
    assert _artifact(renderer, document()).data == first


# ----------------------------------------------------------------- contracts
@pytest.mark.parametrize("renderer", renderers_under_test(), ids=lambda r: r.kind)
def test_declared_metadata_is_on_the_allowlist(renderer: ResumeRenderer) -> None:
    assert renderer.content_type in ALLOWED_CONTENT_TYPES
    assert renderer.kind in RESUME_ARTIFACT_KINDS
    assert renderer.template_version == TEMPLATE_VERSION


def test_the_reference_renderer_is_not_production() -> None:
    """A plain-text file must never be able to serve as a real resume."""
    reference = ReferenceTextRenderer(TEMPLATE_VERSION)
    assert reference.production is False
    assert reference.content_type == "text/plain; charset=utf-8"
    assert reference.extension == "txt"


def test_the_production_renderers_declare_themselves_production() -> None:
    assert DocxResumeRenderer(TEMPLATE_VERSION).production is True
    assert PdfResumeRenderer(TEMPLATE_VERSION).production is True


def test_the_rendered_artifact_rejects_an_unknown_content_type() -> None:
    with pytest.raises(ValueError):
        RenderedArtifact(
            kind="resume_docx",
            content_type="application/octet-stream",
            extension="docx",
            template_version=TEMPLATE_VERSION,
            data=b"x",
        )


def test_the_rendered_artifact_rejects_empty_bytes() -> None:
    with pytest.raises(ValueError):
        RenderedArtifact(
            kind="resume_docx",
            content_type="text/plain; charset=utf-8",
            extension="txt",
            template_version=TEMPLATE_VERSION,
            data=b"",
        )


def test_the_rendered_artifact_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError):
        RenderedArtifact(
            kind="handoff_packet",
            content_type="application/pdf",
            extension="pdf",
            template_version=TEMPLATE_VERSION,
            data=b"x",
        )
