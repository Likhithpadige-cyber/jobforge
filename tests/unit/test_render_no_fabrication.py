"""M8 renders; it never writes.

The boundary being defended:

    M7 approved structured content -> M8 canonical document -> renderer

There is no model, no provider, no network and no generation anywhere in
this path, and this file asserts it two ways. The first is structural: the
rendering package must not import anything that could reach a provider. The
second is behavioural: every word in a rendered artifact must be a word the
persisted content already held, or one of a short allowlist of static
template strings.

The second test is the one that matters. An implementation could pass an
import audit and still quietly add "Results-driven professional" to a summary
or round 12 minutes up to 15; this compares the produced document against the
input word for word.

Synthetic content only.
"""

from __future__ import annotations

import io
import re

import pytest

from packages.services.rendering.document import SECTION_HEADINGS, build_document
from packages.services.rendering.docx import DocxResumeRenderer
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.services.rendering.protocol import TEMPLATE_VERSION
from packages.services.rendering.reference import ReferenceTextRenderer
from tests.unit.renderer_deps import require_dependency

pytestmark = pytest.mark.unit

# All three renderers are asserted below, so all three dependencies are
# required. Skipping the DOCX and PDF cases would leave only the reference
# renderer - the one that cannot reach a candidate - under test.
require_dependency("docx", "python-docx", "requirements.txt")
require_dependency("reportlab", "reportlab", "requirements.txt")
require_dependency("pdfminer", "pdfminer.six", "requirements-dev.txt")

RESUME_ID = "0192f000-0000-7000-8000-000000000001"

#: Deliberately contains numerals, a range and an acronym - the things a
#: "helpful" renderer would be most tempted to round, expand or reword.
LINES = {
    "summary": ["Synthetic engineer, 7 years, AWS and GCP."],
    "experience": [
        "Cut a synthetic deploy from 40 to 12 minutes.",
        "Ran a synthetic migration for 3 teams.",
    ],
    "skills": ["Terraform, Kubernetes, PostgreSQL."],
}
CONTENT = {
    key: {str(i): {"text": t, "order": i} for i, t in enumerate(texts)}
    for key, texts in LINES.items()
}

#: Every string M8 may contribute on its own.
#:
#: Headings are read out of `SECTION_HEADINGS` rather than copied here. A
#: duplicated list would drift: someone adds a heading, the test still holds
#: the old set, and the new one is reported as fabricated - or worse, the
#: list is "fixed" by pasting in whatever the renderer produced, which is how
#: an anti-fabrication test stops testing anything.
ALLOWED_CHROME = set(SECTION_HEADINGS.values()) | {"JobForge", "-"}


def document():  # type: ignore[no-untyped-def]
    return build_document(resume_id=RESUME_ID, resume_version=2, content=CONTENT)


def approved_words() -> set[str]:
    words: set[str] = set()
    for texts in LINES.values():
        for line in texts:
            words |= set(re.findall(r"[A-Za-z0-9][A-Za-z0-9._%+-]*", line))
    return words


def chrome_words() -> set[str]:
    words: set[str] = set()
    for item in ALLOWED_CHROME:
        words |= set(re.findall(r"[A-Za-z0-9][A-Za-z0-9._%+-]*", item))
    return words


# ----------------------------------------------------------- structural proof
def test_the_rendering_package_imports_no_provider_path() -> None:
    """No LLM client, no httpx, no provider SDK anywhere under rendering.

    An import audit is weak on its own - it is the cheap half of the proof -
    but it catches the case where somebody reaches for a model to "improve"
    a bullet and nobody notices in review.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "services" / "rendering"
    forbidden = ("httpx", "requests", "anthropic", "openai", "agents.llm", "StructuredClient")
    offenders = []
    for path in sorted(root.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if line.strip().startswith(("import ", "from "))
        )
        for name in forbidden:
            if name in code:
                offenders.append((path.name, name))
    assert offenders == [], offenders


# --------------------------------------------------------- behavioural proof
def _docx_text(data: bytes) -> str:
    import docx

    return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)


def _pdf_text(data: bytes) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(data))


def _reference_text(data: bytes) -> str:
    return data.decode("utf-8")


RENDERERS = [
    (ReferenceTextRenderer, _reference_text, "reference"),
    (DocxResumeRenderer, _docx_text, "docx"),
    (PdfResumeRenderer, _pdf_text, "pdf"),
]


@pytest.mark.parametrize("factory,extract,name", RENDERERS, ids=[r[2] for r in RENDERERS])
def test_no_word_appears_that_was_not_approved_or_allowlisted(
    factory: object, extract: object, name: str
) -> None:
    """The anti-fabrication assertion.

    Every alphanumeric token in the rendered document must come from the
    persisted resume content or from the static chrome list. A skill, a
    metric, a job title or a date that nobody approved fails here.
    """
    renderer = factory(TEMPLATE_VERSION)  # type: ignore[operator]
    assert renderer.available() and renderer.implemented, (
        f"the {name} renderer is not usable in this environment; its "
        "dependency is declared and this assertion must not be skipped"
    )
    text = extract(renderer.render(document()))  # type: ignore[operator]
    produced = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9._%+-]*", text))

    # Approved content is matched exactly - a renderer that recased a line a
    # human approved has altered it. Chrome is matched case-insensitively,
    # because how a template cases its own heading is a styling decision:
    # the reference renderer sets headings in capitals, and that is not a
    # claim about the candidate.
    approved = approved_words()
    chrome = {word.lower() for word in chrome_words()}
    unexplained = {
        token for token in produced if token not in approved and token.lower() not in chrome
    }
    assert unexplained == set(), f"{name} produced unapproved tokens: {unexplained}"


@pytest.mark.parametrize("factory,extract,name", RENDERERS, ids=[r[2] for r in RENDERERS])
def test_numerals_are_neither_rounded_nor_invented(
    factory: object, extract: object, name: str
) -> None:
    """Numbers are the highest-consequence thing to get wrong on a resume."""
    renderer = factory(TEMPLATE_VERSION)  # type: ignore[operator]
    assert renderer.available() and renderer.implemented, (
        f"the {name} renderer is not usable in this environment; its "
        "dependency is declared and this assertion must not be skipped"
    )
    text = extract(renderer.render(document()))  # type: ignore[operator]
    approved_numbers = set()
    for texts in LINES.values():
        for line in texts:
            approved_numbers |= set(re.findall(r"\d+", line))
    produced_numbers = set(re.findall(r"\d+", text))
    assert produced_numbers <= approved_numbers, (
        f"{name} produced numbers nobody approved: " f"{produced_numbers - approved_numbers}"
    )


@pytest.mark.parametrize("factory,extract,name", RENDERERS, ids=[r[2] for r in RENDERERS])
def test_an_empty_section_stays_empty(factory: object, extract: object, name: str) -> None:
    """A renderer must not fill a gap with a plausible placeholder."""
    renderer = factory(TEMPLATE_VERSION)  # type: ignore[operator]
    assert renderer.available() and renderer.implemented, (
        f"the {name} renderer is not usable in this environment; its "
        "dependency is declared and this assertion must not be skipped"
    )
    sparse = build_document(
        resume_id=RESUME_ID,
        resume_version=1,
        content={"summary": {"0": {"text": "Only one line.", "order": 0}}},
    )
    text = extract(renderer.render(sparse))  # type: ignore[operator]
    for absent in ("Experience", "Skills", "Education", "Certifications"):
        assert absent not in text
