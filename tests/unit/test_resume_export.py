"""Synthetic export safety, source lineage, extraction and measured layout regressions."""

from __future__ import annotations

import hashlib
import io
import zipfile
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from packages.core.models import (
    Certification,
    Education,
    Experience,
    Profile,
    ProfileFact,
    Resume,
    User,
)
from packages.core.services import resume_review
from packages.core.services.handoff_packet import build_packet_zip, generate_manual_packet
from packages.core.services.resume_export import (
    approved_export,
    digest,
    export_binding,
    preview_export,
)
from packages.services.rendering.document import MalformedResumeContentError, build_document
from packages.services.rendering.docx import DocxResumeRenderer
from packages.services.rendering.pdf import PdfResumeRenderer, validate_text, wrap_text
from packages.services.rendering.protocol import TEMPLATE_VERSION, RendererUnavailableError
from packages.shared.clock import utc_today
from packages.shared.errors import ConflictError, NotFoundError

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.fixture(autouse=True)
def isolate_new_evidence_dependency():
    with (
        patch("packages.core.services.artifacts.validate_current_evidence", AsyncMock()),
        patch("packages.core.services.artifacts.validate_export_records", AsyncMock()),
        patch("packages.core.services.handoff_packet.validate_export_records", AsyncMock()),
        patch("packages.core.services.handoff_packet.validate_package_evidence", AsyncMock()),
        patch("packages.core.services.handoff_packet.validate_current_evidence", AsyncMock()),
    ):
        yield


def sample():
    content = {
        "summary": {"0": {"text": "Maintained synthetic systems.", "fact_ids": [], "order": 0}},
        "experience": {
            "1": {
                "text": "Operated synthetic infrastructure.",
                "fact_ids": [str(UUID(int=4))],
                "order": 1,
            }
        },
    }
    resume = Resume(
        id=UUID(int=1),
        profile_id=UUID(int=2),
        version=3,
        content=content,
        verification_status="passed",
    )
    document = {
        "schema_version": 1,
        "evidence_fingerprints": {
            "schema_version": 1,
            "facts": {str(UUID(int=4)): {"version": "0" * 64, "sha256": "1" * 64}},
        },
        "profile_id": str(UUID(int=2)),
        "profile_version": 5,
        "contact": {
            "name": "Zoë Example",
            "email": "candidate@example.invalid",
            "phone": "+1 202 555 0100",
            "location": "Example City",
        },
        "links": {"portfolio": "https://example.invalid/" + "a" * 160},
        "records": [
            {
                "section": "experience",
                "source_table": "experiences",
                "source_id": str(UUID(int=3)),
                "fact_ids": [str(UUID(int=4))],
                "fields": {
                    "title": "Systems Engineer",
                    "company_name": "Synthetic Company",
                    "start_date": "2020-01-01",
                    "end_date": "Present",
                },
            },
            {
                "section": "education",
                "source_table": "educations",
                "source_id": str(UUID(int=5)),
                "fact_ids": [],
                "fields": {
                    "degree": "BS",
                    "field_of_study": "Computer Science",
                    "institution": "Example University",
                    "start_date": "2015-01-01",
                    "end_date": "2019-01-01",
                },
            },
            {
                "section": "certifications",
                "source_table": "certifications",
                "source_id": str(UUID(int=6)),
                "fact_ids": [],
                "fields": {
                    "name": "Example Certificate",
                    "issuer": "Example Issuer",
                    "issued_date": "2024-01-01",
                    "expires_date": "2030-01-01",
                    "status": "active",
                },
            },
        ],
    }
    return resume, document


def rendered_doc():
    resume, document = sample()
    return build_document(
        resume_id=str(resume.id),
        resume_version=resume.version,
        content=resume.content,
        export_data=document,
    )


def snapshot(resume, data):
    return {
        "resume_id": str(resume.id),
        "resume_version": resume.version,
        "content_digest": digest(resume.content),
        "export_document": data,
        "export_digest": export_binding(resume, data),
        "export_owner_id": str(UUID(int=9)),
    }


def test_snapshot_binds_content_contact_records_provenance_and_version():
    resume, data = sample()
    approved = snapshot(resume, data)
    assert resume_review.snapshot_matches(approved, resume)
    assert approved_export(approved, resume) == data
    for mutate in (
        lambda r, d: d["contact"].update(email="changed@example.invalid"),
        lambda r, d: d["records"][0]["fields"].update(title="Changed"),
        lambda r, d: d["records"][0].update(source_id=str(UUID(int=33))),
        lambda r, d: r.content["summary"]["0"].update(text="Unreviewed text"),
        lambda r, d: setattr(r, "version", 4),
    ):
        changed_resume, changed_data = sample()
        altered = snapshot(changed_resume, changed_data)
        mutate(changed_resume, changed_data)
        assert not resume_review.snapshot_matches(altered, changed_resume)
        with pytest.raises(ConflictError):
            approved_export(altered, changed_resume)


@pytest.mark.parametrize("value", [{}, {"export_document": {}}, {"export_owner_id": "x"}])
def test_legacy_or_partial_approval_cannot_authorize_contact_export(value):
    resume, _ = sample()
    with pytest.raises(ConflictError):
        approved_export(value, resume)


def test_grouping_preserves_every_claim_and_source_identity():
    doc = rendered_doc()
    assert doc.contact_lines[0] == "Zoë Example"
    experience = next(s for s in doc.sections if s.key == "experience")
    assert experience.entries[0].heading
    assert (
        experience.entries[0].text
        == "Systems Engineer | Synthetic Company | Jan 2020 \u2013 Present"
    )
    assert experience.entries[1].fact_ids == (str(UUID(int=4)),)
    assert [e.text for s in doc.sections for e in s.entries].count(
        "Operated synthetic infrastructure."
    ) == 1
    assert "work_authorization" not in repr(doc)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unknown_or_ambiguous_employer_is_refused_instead_of_misattributed(ambiguous):
    resume, data = sample()
    if ambiguous:
        record = deepcopy(data["records"][0])
        record["source_id"] = str(UUID(int=88))
        data["records"].append(record)
    else:
        data["records"][0]["fact_ids"] = []
    with pytest.raises(MalformedResumeContentError, match="exactly one"):
        build_document(
            resume_id=str(resume.id), resume_version=3, content=resume.content, export_data=data
        )


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_exports_contain_approved_contact_and_structured_records_in_reading_order(kind):
    doc = rendered_doc()
    if kind == "pdf":
        from pypdf import PdfReader

        raw = PdfResumeRenderer(TEMPLATE_VERSION).render(doc)
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages)
    else:
        import docx

        raw = DocxResumeRenderer(TEMPLATE_VERSION).render(doc)
        reopened = docx.Document(io.BytesIO(raw))
        assert not reopened.tables
        assert all(not p.text for s in reopened.sections for p in s.header.paragraphs)
        text = "\n".join(p.text for p in reopened.paragraphs)
    for expected in (
        "Zoë Example",
        "candidate@example.invalid",
        "Systems Engineer",
        "Synthetic Company",
        "Jan 2020",
        "Present",
        "Example University",
        "Computer Science",
        "Example Certificate",
        "Example Issuer",
        "Jan 2030",
    ):
        assert expected in text
    assert (
        text.index("Zoë Example")
        < text.index("Summary")
        < text.index("Experience")
        < text.index("Education")
    )


@pytest.mark.parametrize("text", ["Zoë José Łukasz résumé — naïve", "Ελληνικά", "Кириллица"])
def test_supported_unicode_survives_two_independent_extractors(text):
    from pdfminer.high_level import extract_text
    from pypdf import PdfReader

    doc = build_document(
        resume_id="synthetic",
        resume_version=1,
        content={"summary": {"0": {"text": text, "order": 0}}},
    )
    raw = PdfResumeRenderer(TEMPLATE_VERSION).render(doc)
    assert text in PdfReader(io.BytesIO(raw)).pages[0].extract_text()
    assert text in extract_text(io.BytesIO(raw))
    assert b"/FontFile2" in raw and b"/ToUnicode" in raw


@pytest.mark.parametrize("text", ["中文", "😀", "مرحبا", "abc\x00def", "abc\u202edef"])
def test_unsupported_glyphs_shaping_and_controls_fail_explicitly(text):
    with pytest.raises(RendererUnavailableError, match="U\\+"):
        validate_text(text)


def test_long_token_is_split_without_dropped_or_inserted_characters():
    from reportlab.pdfbase.pdfmetrics import stringWidth

    token = "https://example.invalid/" + "W" * 300
    lines = wrap_text(token, 480)
    assert "".join(lines) == token
    assert len(lines) > 1
    assert all(stringWidth(line, "JobForgeNoto", 10) <= 480 for line in lines)


def test_all_pdf_glyphs_stay_inside_page_bounds_across_multiple_pages():
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTContainer

    resume, data = sample()
    resume.content["summary"] = {
        str(i): {"text": f"Synthetic line {i} " + "W" * 160, "order": i} for i in range(100)
    }
    doc = build_document(
        resume_id=str(resume.id), resume_version=3, content=resume.content, export_data=data
    )
    raw = PdfResumeRenderer(TEMPLATE_VERSION).render(doc)
    pages = list(extract_pages(io.BytesIO(raw)))
    assert len(pages) > 1

    def chars(node):
        if isinstance(node, LTChar):
            yield node
        elif isinstance(node, LTContainer):
            for child in node:
                yield from chars(child)

    for page in pages:
        for glyph in chars(page):
            assert 53 <= glyph.x0 <= glyph.x1 <= 559
            assert 40 <= glyph.y0 <= glyph.y1 <= 770


def test_complete_exports_are_deterministic():
    doc = rendered_doc()
    for renderer in (PdfResumeRenderer(TEMPLATE_VERSION), DocxResumeRenderer(TEMPLATE_VERSION)):
        assert renderer.render(doc) == renderer.render(doc)


def test_manual_packet_contains_both_exact_formats():
    doc = rendered_doc()
    pdf, docx = (
        PdfResumeRenderer(TEMPLATE_VERSION).render(doc),
        DocxResumeRenderer(TEMPLATE_VERSION).render(doc),
    )
    kwargs = {
        "resume_pdf": pdf,
        "resume_docx": docx,
        "cover_letter_text": "Synthetic letter",
        "answers_text": "Synthetic answers",
        "job_metadata": "{}",
        "checklist": "Manual only",
    }
    raw = build_packet_zip(**kwargs)
    assert raw == build_packet_zip(**kwargs)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    assert len(archive.namelist()) == 6
    assert archive.read("resume.pdf") == pdf
    assert archive.read("resume.docx") == docx
    kwargs["resume_docx"] = b""
    with pytest.raises(ConflictError):
        build_packet_zip(**kwargs)


def preview_session(*, fact_status="verified", cert_status="active", expires=None, foreign=False):
    resume, _ = sample()
    owner = User(id=UUID(int=9), role="owner")
    profile = Profile(
        id=resume.profile_id,
        user_id=owner.id,
        full_name="Zoë Example",
        email="candidate@example.invalid",
        version=5,
        links={"portfolio": "https://example.invalid", "unsafe": "javascript:alert(1)"},
        work_authorization="NEVER EXPORT",
    )
    fact = ProfileFact(
        id=UUID(int=4),
        profile_id=resume.profile_id,
        status=fact_status,
        source_table="experiences",
        source_id=UUID(int=3),
    )
    exp = Experience(
        id=UUID(int=3),
        profile_id=resume.profile_id,
        title="Systems Engineer",
        company_name="Synthetic Company",
        start_date=date(2020, 1, 1),
    )
    education = Education(
        id=UUID(int=5), profile_id=resume.profile_id, institution="Example University", degree="BS"
    )
    cert = Certification(
        id=UUID(int=6),
        profile_id=resume.profile_id,
        name="Example Certificate",
        status=cert_status,
        expires_date=expires,
    )
    session = AsyncMock()
    session.get.side_effect = lambda model, identifier, **kwargs: {
        Profile: profile,
        Experience: exp,
        Education: education,
        Certification: cert,
    }.get(model)
    session.scalar.return_value = None if foreign else profile
    session.scalars.side_effect = [
        MagicMock(all=lambda: [fact]),
        MagicMock(all=lambda: [fact]),
        MagicMock(all=lambda: [exp]),
        MagicMock(all=lambda: [education]),
        MagicMock(all=lambda: [cert]),
    ]
    return session, resume, owner


@pytest.mark.asyncio
async def test_preview_uses_only_owner_sources_and_omits_sensitive_fields():
    session, resume, actor = preview_session()
    result = await preview_export(session, resume, actor)
    assert "NEVER EXPORT" not in repr(result) and "javascript:" not in repr(result)
    assert result["document"]["records"][0]["fact_ids"] == [str(UUID(int=4))]
    assert result["digest"] == export_binding(resume, result["document"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expires",
    [("in_progress", None), ("expired", None), ("active", utc_today() - timedelta(days=1))],
)
async def test_unheld_or_expired_certifications_are_never_presented_as_held(status, expires):
    session, resume, actor = preview_session(cert_status=status, expires=expires)
    result = await preview_export(session, resume, actor)
    assert not any(r["section"] == "certifications" for r in result["document"]["records"])


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign,role", [(True, "owner"), (False, "reviewer")])
async def test_foreign_owner_and_reviewer_cannot_preview_contact(foreign, role):
    session, resume, actor = preview_session(foreign=foreign)
    actor.role = role
    with pytest.raises(NotFoundError):
        await preview_export(session, resume, actor)


@pytest.mark.asyncio
async def test_unverified_fact_cannot_be_laundered_into_structured_export():
    session, resume, actor = preview_session(fact_status="draft")
    with pytest.raises(ConflictError, match="inactive evidence"):
        await preview_export(session, resume, actor)


@pytest.mark.asyncio
async def test_changed_preview_is_refused_before_any_approval_write():
    resume, data = sample()
    approval = SimpleNamespace(subject_snapshot={"resume_id": str(resume.id), "resume_version": 3})
    context = SimpleNamespace(
        application=SimpleNamespace(id=UUID(int=8)), resume=resume, claims=[], approval=None
    )
    session = AsyncMock()
    with (
        patch.object(resume_review, "_context", AsyncMock(return_value=context)),
        patch.object(resume_review, "_decidable_approval", AsyncMock(return_value=approval)),
        patch.object(
            resume_review,
            "preview_export",
            AsyncMock(return_value={"document": data, "digest": "new"}),
        ),
        pytest.raises(resume_review.StaleResumeError),
    ):
        await resume_review.approve(
            session,
            application_id=UUID(int=8),
            actor=User(id=UUID(int=9), role="owner"),
            export_digest="old",
        )
    session.flush.assert_not_awaited()
    assert "export_document" not in approval.subject_snapshot


@pytest.mark.asyncio
async def test_manual_package_refuses_missing_format_before_storage():
    resume, data = sample()
    approval = SimpleNamespace(
        id=UUID(int=7), subject_snapshot=snapshot(resume, data), decided_at=None
    )
    session, store = AsyncMock(), AsyncMock()
    session.scalars.return_value = MagicMock(all=lambda: [])
    with (
        patch(
            "packages.core.services.review_completion.review_is_complete",
            AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.review_completion.current_resume_approval",
            AsyncMock(return_value=approval),
        ),
        patch(
            "packages.core.services.handoff_packet.latest_tailored_resume",
            AsyncMock(return_value=resume),
        ),
        pytest.raises(ConflictError, match="Both current PDF and DOCX"),
    ):
        await generate_manual_packet(
            session,
            application=SimpleNamespace(id=UUID(int=8), status="manual_handoff"),
            store=store,
        )
    store.put.assert_not_awaited()


def test_font_asset_is_pinned_and_license_is_present():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "packages/services/rendering/fonts"
    assert hashlib.sha256((root / "NotoSans-Regular.ttf").read_bytes()).hexdigest() == (
        # pragma: allowlist nextline secret (public font SHA-256)
        "f5f552c8c5edb61fe6efb824baf4d4de47b1a8689ab4925ff43f7bd6a4ebece5"
    )
    assert "OFL" in (root / "OFL.txt").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [False, True, "reapproved", "verification"])
async def test_production_artifacts_bind_exact_approval_and_refuse_mid_render_changes(
    tmp_path, invalidate
):
    from packages.core.services.artifacts import RenderContext, StaleApprovalError, render_resume
    from packages.services.rendering.store import FilesystemArtifactStore

    resume, data = sample()
    resume.application_id = UUID(int=8)
    resume.verification_status = "passed"
    approval = SimpleNamespace(
        id=UUID(int=7), subject_snapshot=snapshot(resume, data), status="approved", decided_at=None
    )
    application = SimpleNamespace(id=UUID(int=8), status="approved")
    context = RenderContext(resume, application, approval)
    session = AsyncMock()
    session.add = MagicMock()
    session.scalars.return_value = MagicMock(first=lambda: None)
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    async def refresh(row, **kwargs):
        if invalidate == "verification" and row is resume:
            row.verification_status = "failed"
        if invalidate in (True, "reapproved") and row is approval:
            row.subject_snapshot["export_document"]["contact"]["email"] = "changed@example.invalid"
            if invalidate == "reapproved":
                row.subject_snapshot["export_digest"] = export_binding(
                    resume, row.subject_snapshot["export_document"]
                )

    session.refresh.side_effect = refresh
    with (
        patch("packages.core.services.artifacts.load_context", AsyncMock(return_value=context)),
        patch("packages.core.services.artifacts.record_event", AsyncMock()) as event,
    ):
        if invalidate:
            with pytest.raises(StaleApprovalError):
                await render_resume(
                    session,
                    store=store,
                    renderers=(PdfResumeRenderer(TEMPLATE_VERSION),),
                    resume_id=resume.id,
                )
            session.add.assert_not_called()
            event.assert_not_awaited()
        else:
            produced = await render_resume(
                session,
                store=store,
                renderers=(
                    PdfResumeRenderer(TEMPLATE_VERSION),
                    DocxResumeRenderer(TEMPLATE_VERSION),
                ),
                resume_id=resume.id,
            )
            assert {a.kind for a in produced} == {"resume_pdf", "resume_docx"}
            for artifact in produced:
                assert artifact.approval_id == approval.id
                assert artifact.resume_version == resume.version
                assert artifact.template_version == TEMPLATE_VERSION
                assert (
                    hashlib.sha256(await store.get(artifact.storage_key)).hexdigest()
                    == artifact.sha256
                )
            assert "candidate@example.invalid" not in repr(event.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "checksum", "approval", "metadata"])
async def test_manual_packet_real_bytes_and_fail_closed_persistence(tmp_path, failure):
    from packages.core.models import Artifact, Job
    from packages.services.rendering.store import FilesystemArtifactStore

    resume, data = sample()
    resume.application_id = UUID(int=8)
    resume.verification_status = "passed"
    approval = SimpleNamespace(
        id=UUID(int=7), subject_snapshot=snapshot(resume, data), status="approved", decided_at=None
    )
    application = SimpleNamespace(id=UUID(int=8), status="manual_handoff", job_id=UUID(int=10))
    job = Job(id=application.job_id, title="Synthetic Role", url="https://example.invalid/job")
    cover = SimpleNamespace(id=UUID(int=12), version=1, body_text="Synthetic letter")
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    artifacts = []
    for i, renderer in enumerate(
        (PdfResumeRenderer(TEMPLATE_VERSION), DocxResumeRenderer(TEMPLATE_VERSION))
    ):
        raw = renderer.render(rendered_doc())
        key = f"synthetic/{renderer.extension}"
        await store.put(key, raw, content_type=renderer.content_type)
        artifacts.append(
            Artifact(
                id=UUID(int=20 + i),
                kind=renderer.kind,
                storage_key=key,
                sha256=hashlib.sha256(raw).hexdigest(),
                template_version=TEMPLATE_VERSION,
            )
        )
    if failure == "checksum":
        await store.put(artifacts[0].storage_key, b"corrupt", content_type="application/pdf")
    session = AsyncMock()
    session.add = MagicMock()
    session.scalars.return_value = MagicMock(all=lambda: artifacts)
    session.get.return_value = job
    session.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)

    approval_refreshes = 0

    async def refresh(row, **kwargs):
        nonlocal approval_refreshes
        if row is approval:
            approval_refreshes += 1
            if approval_refreshes == 1:
                return
        if failure == "approval" and row is approval:
            approval.status = "changes_requested"
        if failure == "metadata" and row is approval:
            approval.subject_snapshot["export_document"]["contact"]["email"] = (
                "changed@example.invalid"
            )
            approval.subject_snapshot["export_digest"] = export_binding(
                resume, approval.subject_snapshot["export_document"]
            )

    session.refresh.side_effect = refresh
    with (
        patch(
            "packages.core.services.review_completion.review_is_complete",
            AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.review_completion.current_resume_approval",
            AsyncMock(return_value=approval),
        ),
        patch(
            "packages.core.services.handoff_packet.latest_tailored_resume",
            AsyncMock(return_value=resume),
        ),
        patch(
            "packages.core.services.handoff_packet.latest_cover_letter",
            AsyncMock(return_value=cover),
        ),
        patch(
            "packages.core.services.handoff_packet.existing_questions", AsyncMock(return_value=[])
        ),
    ):
        if failure:
            with pytest.raises(ConflictError):
                await generate_manual_packet(session, application=application, store=store)
            session.add.assert_not_called()
        else:
            packet = await generate_manual_packet(session, application=application, store=store)
            archive = zipfile.ZipFile(io.BytesIO(await store.get(packet.storage_key)))
            assert {"resume.pdf", "resume.docx"} <= set(archive.namelist())
            assert archive.read("resume.pdf") == await store.get(artifacts[0].storage_key)
            assert packet.approval_id == approval.id
            assert packet.template_version.startswith("manual-handoff/2/")


def test_pdf_section_heading_stays_with_first_structured_entry():
    from pypdf import PdfReader

    resume, data = sample()
    for i in range(2, 18):
        resume.content["experience"][str(i)] = {
            "text": (
                f"Synthetic accomplishment {i}: maintained documented infrastructure changes, "
                "reviewed operational checks, and recorded evidence "
                "for a fictional engineering team."
            ),
            "fact_ids": [str(UUID(int=4))],
            "order": i,
        }
    doc = build_document(
        resume_id=str(resume.id), resume_version=3, content=resume.content, export_data=data
    )
    raw = PdfResumeRenderer(TEMPLATE_VERSION).render(doc)
    for page in PdfReader(io.BytesIO(raw)).pages:
        text = page.extract_text()
        if "Education" in text:
            assert "Example University" in text
        if "Certifications" in text:
            assert "Example Certificate" in text


def test_docx_styles_have_no_inherited_title_rule_or_theme_color():
    import docx
    from docx.oxml.ns import qn

    reopened = docx.Document(
        io.BytesIO(DocxResumeRenderer(TEMPLATE_VERSION).render(rendered_doc()))
    )
    assert not list(reopened.styles.element.iter(qn("w:pBdr")))
    assert str(reopened.styles["Title"].font.color.rgb) == "000000"


@pytest.mark.asyncio
async def test_download_hashes_returned_bytes_instead_of_a_second_store_read():
    from apps.api.routers import artifacts as route
    from packages.core.models import Artifact
    from packages.services.rendering.store import ArtifactIntegrityError

    artifact = Artifact(
        id=UUID(int=40),
        resume_id=UUID(int=1),
        kind="resume_pdf",
        storage_key="synthetic/test.pdf",
        template_version="ats-single-column/1",
        content_type="application/pdf",
        sha256=hashlib.sha256(b"approved bytes").hexdigest(),
    )
    session = AsyncMock()
    session.get.side_effect = (
        lambda model, identifier, **kwargs: artifact
        if model is Artifact
        else Resume(id=UUID(int=1), content={})
    )
    store = AsyncMock()
    store.get.return_value = b"corrupted response bytes"
    # The old code verified a second read, which can differ from the response.
    store.verify.return_value = None
    with (
        patch.object(route, "artifact_store", return_value=store),
        pytest.raises(ArtifactIntegrityError, match="checksum"),
    ):
        await route.download_artifact(
            artifact.id, session, MagicMock(), SimpleNamespace(role="owner")
        )
    store.verify.assert_not_called()


@pytest.mark.asyncio
async def test_contact_snapshot_is_owner_only_even_with_a_different_template_name():
    from apps.api.routers import artifacts as route
    from packages.core.models import Approval, Artifact

    artifact = Artifact(
        id=UUID(int=40),
        resume_id=UUID(int=1),
        approval_id=UUID(int=7),
        kind="resume_pdf",
        template_version="future-custom-template/3",
    )
    approval = SimpleNamespace(subject_snapshot={"export_document": {"contact": {}}})
    resume = SimpleNamespace(profile_id=UUID(int=2))
    profile = SimpleNamespace(user_id=UUID(int=9))
    session = AsyncMock()
    session.get.side_effect = lambda model, identifier, **kwargs: {
        Artifact: artifact,
        Approval: approval,
        Resume: resume,
        Profile: profile,
    }[model]
    with patch.object(route, "artifact_store") as storage, pytest.raises(NotFoundError):
        await route.download_artifact(
            artifact.id, session, MagicMock(), SimpleNamespace(role="reviewer", id=UUID(int=9))
        )
    storage.assert_not_called()


def test_pdf_rejects_unsupported_characters_in_custom_section_headings():
    document = build_document(
        resume_id="synthetic",
        resume_version=1,
        content={"未知": {"0": {"text": "Supported Latin text.", "order": 0}}},
    )
    with pytest.raises(RendererUnavailableError, match="Unsupported export character"):
        PdfResumeRenderer(TEMPLATE_VERSION).render(document)


@pytest.mark.asyncio
async def test_historical_text_only_artifact_remains_readable_by_reviewer():
    from apps.api.routers import artifacts as route
    from packages.core.models import Approval, Artifact, Resume

    raw = b"historical synthetic artifact without contact details"
    artifact = Artifact(
        id=UUID(int=40),
        resume_id=UUID(int=1),
        approval_id=UUID(int=7),
        resume_version=1,
        kind="resume_pdf",
        template_version="ats-single-column/1",
        storage_key="synthetic/historical.pdf",
        content_type="application/pdf",
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    approval = SimpleNamespace(
        subject_snapshot={"resume_id": str(UUID(int=1)), "resume_version": 1}, decided_at=None
    )
    session = AsyncMock()
    session.get.side_effect = lambda model, identifier, **kwargs: {
        Artifact: artifact,
        Approval: approval,
        Resume: SimpleNamespace(content={}),
    }[model]
    store = AsyncMock()
    store.get.return_value = raw
    with patch.object(route, "artifact_store", return_value=store):
        response = await route.download_artifact(
            artifact.id, session, MagicMock(), SimpleNamespace(role="reviewer")
        )
    assert response.body == raw
    assert response.headers["cache-control"] == "private, no-store"
