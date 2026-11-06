"""Cowork M1/M2/M4/M5 regressions; all inputs are fictional."""

import io
import unicodedata
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import docx
import pytest
from pypdf import PdfReader

from packages.core.models import Artifact, Profile, Resume
from packages.core.services.resume_export import digest
from packages.services.rendering.document import MalformedResumeContentError, build_document
from packages.services.rendering.docx import DocxResumeRenderer
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.services.rendering.protocol import TEMPLATE_VERSION
from packages.shared.errors import NotFoundError
from tests.unit.test_resume_export import sample

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def document(resume, data):
    return build_document(
        resume_id=str(resume.id),
        resume_version=resume.version,
        content=resume.content,
        export_data=data,
    )


def test_m1_tabs_expand_identically_in_both_formats():
    resume, data = sample()
    resume.content["summary"]["0"]["text"] = "Skills:\tLinux\tAWS"
    source = document(resume, data)
    pdf = PdfResumeRenderer(TEMPLATE_VERSION).render(source)
    word = DocxResumeRenderer(TEMPLATE_VERSION).render(source)
    assert "Skills: Linux AWS" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Skills: Linux AWS" in "\n".join(
        p.text for p in docx.Document(io.BytesIO(word)).paragraphs
    )


@pytest.mark.parametrize("target", ["name", "email", "phone", "location", "employer", "link"])
@pytest.mark.parametrize("separator", ["\n", "\r", "\t", "\u2028"])
def test_m2_single_line_fields_cannot_hide_additional_lines(target, separator):
    resume, data = sample()
    if target == "employer":
        data["records"][0]["fields"]["company_name"] += separator + "Hidden Line"
    elif target == "link":
        data["links"]["portfolio"] += separator + "Hidden Line"
    else:
        data["contact"][target] += separator + "Hidden Line"
    with pytest.raises(MalformedResumeContentError, match="single-line"):
        document(resume, data)


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://example.invalid/ hidden",
        # pragma: allowlist nextline secret (synthetic URL rejection case)
        "https://user:pass@example.invalid/",
        "https://example.invalid:invalid/",
    ],
)
def test_m2_malformed_http_links_are_refused(url):
    resume, data = sample()
    data["links"]["portfolio"] = url
    with pytest.raises(MalformedResumeContentError):
        document(resume, data)


def test_m4_nfd_names_and_claims_normalise_before_digest_and_render():
    resume, data = sample()
    data["contact"]["name"] = unicodedata.normalize("NFD", "José Núñez")
    resume.content["summary"]["0"]["text"] = unicodedata.normalize(
        "NFD", "Maintained résumé evidence."
    )
    assert digest(resume.content) == digest(
        {"summary": resume.content["summary"], "experience": resume.content["experience"]}
    )
    assert digest("José Núñez") == digest(unicodedata.normalize("NFD", "José Núñez"))
    source = document(resume, data)
    pdf = PdfReader(io.BytesIO(PdfResumeRenderer(TEMPLATE_VERSION).render(source)))
    word = docx.Document(io.BytesIO(DocxResumeRenderer(TEMPLATE_VERSION).render(source)))
    assert "José Núñez" in pdf.pages[0].extract_text()
    assert word.paragraphs[0].text == "José Núñez"
    assert "Maintained résumé evidence." in pdf.pages[0].extract_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("template", [None, "ats-single-column/3", "custom/unclassified"])
async def test_m5_unknown_templates_are_owner_only_without_a_snapshot_marker(template):
    from apps.api.routers import artifacts as route

    artifact = Artifact(
        id=UUID(int=40), resume_id=UUID(int=1), kind="resume_pdf", template_version=template
    )
    resume = SimpleNamespace(profile_id=UUID(int=2))
    profile = SimpleNamespace(user_id=UUID(int=9))
    db = AsyncMock()
    db.get.side_effect = lambda model, identifier: {
        Artifact: artifact,
        Resume: resume,
        Profile: profile,
    }[model]
    with patch.object(route, "artifact_store") as storage, pytest.raises(NotFoundError):
        await route.download_artifact(
            artifact.id, db, MagicMock(), SimpleNamespace(role="reviewer", id=UUID(int=9))
        )
    storage.assert_not_called()


def test_docx_uses_editable_paragraphs_and_portable_font():
    resume, data = sample()
    word = docx.Document(
        io.BytesIO(DocxResumeRenderer(TEMPLATE_VERSION).render(document(resume, data)))
    )
    assert word.styles["Normal"].font.name == "Arial"
    word.paragraphs[0].text = "Edited Fictional Name"
    saved = io.BytesIO()
    word.save(saved)
    assert docx.Document(io.BytesIO(saved.getvalue())).paragraphs[0].text == "Edited Fictional Name"
    assert not word.tables
