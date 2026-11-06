"""P0 real-database journeys; use a disposable migrated test DB."""

import io
import zipfile
from copy import deepcopy

import pytest
from sqlalchemy import select

from packages.core.models import Application, Approval, ApprovalKind
from packages.core.services.artifacts import NotApprovedError, render_resume
from packages.core.services.handoff_packet import generate_manual_packet
from packages.services.rendering import production_renderers
from tests.integration import test_submission_readiness as fixtures

inputs = fixtures.inputs
store = fixtures.store
pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]


async def prepare(session, demo_profile, demo_job, demo_application, inputs, store, tmp_path):
    _, _, facts = inputs
    resume, _, actor = await fixtures._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    application = await session.get(Application, demo_application["id"])
    await fixtures._trigger_readiness(session, application.id, store=store)
    return resume, application, actor


async def test_current_approved_manual_packet_contains_both_real_formats(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    resume, application, _ = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    packet = await generate_manual_packet(session, application=application, store=store)
    archive = zipfile.ZipFile(io.BytesIO(await store.get(packet.storage_key)))
    import docx
    from pypdf import PdfReader

    assert (
        "Demo Candidate"
        in PdfReader(io.BytesIO(archive.read("resume.pdf"))).pages[0].extract_text()
    )
    paragraphs = [p.text for p in docx.Document(io.BytesIO(archive.read("resume.docx"))).paragraphs]
    assert "Demo Candidate" in paragraphs
    assert any("Example Cloud Corp" in p for p in paragraphs)
    assert packet.resume_version == resume.version
    assert packet.approval_id is not None
    assert (
        await generate_manual_packet(session, application=application, store=store)
    ).id == packet.id


async def test_tampered_export_snapshot_cannot_reuse_existing_artifacts(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    resume, application, _ = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == application.id, Approval.kind == ApprovalKind.RESUME
        )
    )
    changed = deepcopy(approval.subject_snapshot)
    changed["export_document"]["contact"]["name"] = "Unapproved Candidate"
    approval.subject_snapshot = changed
    await session.flush()
    with pytest.raises(NotApprovedError):
        await render_resume(
            session, store=store, renderers=production_renderers(), resume_id=resume.id
        )


@pytest.mark.parametrize("target", ["verification", "render_snapshot", "packet_snapshot"])
async def test_export_rechecks_exact_inputs_after_storage(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, target
):
    """Real JSONB/row reloads must detect a change after bytes were produced."""
    from sqlalchemy import func, update

    from packages.core.models import Artifact, Resume
    from packages.core.services.resume_export import export_binding
    from packages.shared.errors import ConflictError

    if target == "packet_snapshot":
        resume, application, _ = await prepare(
            session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
        )
    else:
        _, _, facts = inputs
        resume, _, _ = await fixtures._approve_everything(
            session,
            demo_profile=demo_profile,
            demo_job=demo_job,
            demo_application=demo_application,
            facts=facts,
            store=store,
            tmp_path=tmp_path,
        )
        application = await session.get(Application, demo_application["id"])
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == application.id, Approval.kind == ApprovalKind.RESUME
        )
    )
    before = await session.scalar(select(func.count()).select_from(Artifact))

    class MutatingStore:
        bucket = store.bucket
        changed = False

        async def get(self, key):
            return await store.get(key)

        async def exists(self, key):
            return await store.exists(key)

        async def verify(self, key, *, sha256):
            await store.verify(key, sha256=sha256)

        async def put(self, key, data, *, content_type):
            await store.put(key, data, content_type=content_type)
            if self.changed:
                return
            self.changed = True
            if target == "verification":
                await session.execute(
                    update(Resume)
                    .where(Resume.id == resume.id)
                    .values(verification_status="failed")
                    .execution_options(synchronize_session=False)
                )
            else:
                altered = deepcopy(approval.subject_snapshot)
                altered["export_document"]["contact"]["email"] = "unapproved@example.invalid"
                altered["export_digest"] = export_binding(resume, altered["export_document"])
                await session.execute(
                    update(Approval)
                    .where(Approval.id == approval.id)
                    .values(subject_snapshot=altered)
                    .execution_options(synchronize_session=False)
                )

    mutating = MutatingStore()
    with pytest.raises(ConflictError):
        if target == "packet_snapshot":
            await generate_manual_packet(session, application=application, store=mutating)
        else:
            # A new template forces generation rather than reusing prepare's artifacts.
            from packages.services.rendering.pdf import PdfResumeRenderer

            await render_resume(
                session,
                store=mutating,
                resume_id=resume.id,
                renderers=(PdfResumeRenderer("ats-single-column/2/race-regression"),),
            )
    assert mutating.changed
    assert await session.scalar(select(func.count()).select_from(Artifact)) == before


async def test_reviewer_text_approval_allows_later_owner_export_consent(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.models import Artifact, User
    from packages.core.services import resume_review
    from packages.core.services.resume_export import preview_export
    from packages.shared.ids import uuid7
    from tests.integration import test_cover_letter_review as clr

    _, _, facts = inputs
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await session.get(User, demo_application["user_id"])
    reviewer = User(id=uuid7(), role="reviewer", email="m166-reviewer@example.invalid")
    session.add(reviewer)
    await session.flush()
    app_id = demo_application["id"]
    await resume_review.approve(session, application_id=app_id, actor=reviewer)
    resume = await resume_review.latest_tailored_resume(session, app_id)
    approval = await resume_review.resume_approval(session, app_id)
    decided_at = approval.decided_at
    preview = await preview_export(session, resume, actor)
    await resume_review.approve(
        session, application_id=app_id, actor=actor, export_digest=preview["digest"]
    )
    await session.refresh(approval)
    assert approval.decided_by == reviewer.id
    assert approval.decided_at == decided_at
    assert approval.subject_snapshot["export_owner_id"] == str(actor.id)
    assert approval.subject_snapshot["content_digest"]
    assert not list(
        (await session.scalars(select(Artifact).where(Artifact.resume_id == resume.id))).all()
    )


@pytest.mark.parametrize("change", ["draft", "retired", "deleted", "expired", "future"])
@pytest.mark.parametrize("operation", ["render", "manual", "cached_manual"])
async def test_evidence_changed_after_consent_blocks_export_and_package(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, change, operation
):
    from datetime import timedelta

    from packages.core.models import FactStatus
    from packages.core.services.resume_review import latest_tailored_resume
    from packages.shared.clock import utc_now, utc_today
    from packages.shared.errors import ConflictError

    resume, application, _ = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    if operation == "cached_manual":
        await generate_manual_packet(session, application=application, store=store)
    _, _, facts = inputs
    cited = {
        str(fid)
        for section in resume.content.values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    fact = next(f for f in facts if str(f.id) in cited)
    if change in {"draft", "retired"}:
        fact.status = FactStatus(change)
    elif change == "deleted":
        fact.deleted_at = utc_now()
    elif change == "expired":
        fact.valid_to = utc_today() - timedelta(days=1)
    else:
        fact.valid_from = utc_today() + timedelta(days=1)
    await session.flush()
    with pytest.raises(ConflictError, match="evidence"):
        if operation == "render":
            await render_resume(
                session, store=store, renderers=production_renderers(), resume_id=resume.id
            )
        else:
            await generate_manual_packet(session, application=application, store=store)
    assert (await latest_tailored_resume(session, application.id)).id == resume.id


async def test_legacy_approved_version_can_render_then_gain_owner_consent(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.services import resume_review
    from packages.core.services.resume_export import preview_export
    from packages.services.rendering.protocol import LEGACY_TEXT_TEMPLATE, TEMPLATE_VERSION
    from tests.integration import test_legacy_m166_readiness as legacy

    _, _, facts = inputs
    resume, _, actor = await legacy._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    old_approval = await resume_review.resume_approval(session, demo_application["id"])
    decided_by, decided_at = old_approval.decided_by, old_approval.decided_at
    legacy_rows = await render_resume(
        session, store=store, renderers=production_renderers(), resume_id=resume.id
    )
    assert {a.template_version for a in legacy_rows} == {LEGACY_TEXT_TEMPLATE}
    await legacy._trigger_readiness(session, demo_application["id"], store=store)
    preview = await preview_export(session, resume, actor)
    await resume_review.approve(
        session, application_id=demo_application["id"], actor=actor, export_digest=preview["digest"]
    )
    current = await render_resume(
        session, store=store, renderers=production_renderers(), resume_id=resume.id
    )
    assert {a.template_version for a in current} == {TEMPLATE_VERSION}
    assert not {a.id for a in current} & {a.id for a in legacy_rows}
    assert old_approval.decided_by == decided_by and old_approval.decided_at == decided_at
    application = await session.get(Application, demo_application["id"])
    packet = await generate_manual_packet(session, application=application, store=store)
    archive = zipfile.ZipFile(io.BytesIO(await store.get(packet.storage_key)))
    for artifact in current:
        ext = artifact.kind.split("_")[-1]
        assert archive.read("resume." + ext) == await store.get(artifact.storage_key)


@pytest.mark.parametrize("change", ["expired", "deleted", "date"])
@pytest.mark.parametrize("operation", ["render", "manual"])
async def test_credential_withdrawal_after_consent_blocks_exports(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, change, operation
):
    from datetime import timedelta

    from packages.core.models import Certification
    from packages.shared.clock import utc_now, utc_today
    from packages.shared.errors import ConflictError
    from packages.shared.ids import uuid7

    cert = Certification(
        id=uuid7(), profile_id=demo_profile, name="Fictional Certificate", status="active"
    )
    session.add(cert)
    await session.flush()
    resume, application, _ = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    if change == "expired":
        cert.status = "expired"
    elif change == "deleted":
        cert.deleted_at = utc_now()
    else:
        cert.expires_date = utc_today() - timedelta(days=1)
    await session.flush()
    with pytest.raises(ConflictError, match="evidence"):
        if operation == "render":
            await render_resume(
                session, store=store, renderers=production_renderers(), resume_id=resume.id
            )
        else:
            await generate_manual_packet(session, application=application, store=store)


async def test_existing_approved_v2_artifact_pair_remains_packageable(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.models import Artifact

    resume, application, _ = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    rows = list(
        (
            await session.scalars(
                select(Artifact).where(
                    Artifact.resume_id == resume.id,
                    Artifact.kind.in_(["resume_pdf", "resume_docx"]),
                )
            )
        ).all()
    )
    # Simulate the persisted template tags of the preceding feature version;
    # approved binding, bytes and checksums remain unchanged.
    for artifact in rows:
        artifact.template_version = "ats-single-column/2"
    await session.flush()
    packet = await generate_manual_packet(session, application=application, store=store)
    archive = zipfile.ZipFile(io.BytesIO(await store.get(packet.storage_key)))
    for artifact in rows:
        assert archive.read("resume." + artifact.kind.split("_")[-1]) == await store.get(
            artifact.storage_key
        )


@pytest.mark.parametrize("download", ["legacy_artifact", "legacy_packet"])
async def test_legacy_downloads_cannot_bypass_withdrawn_evidence(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, download
):
    from unittest.mock import MagicMock, patch

    from apps.api.routers import applications as app_route
    from apps.api.routers import artifacts as artifact_route
    from packages.core.models import Artifact, FactStatus
    from packages.shared.errors import ConflictError
    from tests.integration import test_legacy_m166_readiness as legacy

    _, _, facts = inputs
    resume, _, actor = await legacy._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await legacy._trigger_readiness(session, demo_application["id"], store=store)
    cited = {
        str(fid)
        for section in resume.content.values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    next(f for f in facts if str(f.id) in cited).status = FactStatus.RETIRED
    await session.flush()
    artifact = await session.scalar(
        select(Artifact).where(Artifact.resume_id == resume.id, Artifact.kind == "resume_pdf")
    )
    with (
        patch.object(artifact_route, "artifact_store") as direct,
        patch.object(app_route, "artifact_store") as packet,
        pytest.raises(ConflictError, match="evidence"),
    ):
        if download == "legacy_artifact":
            await artifact_route.download_artifact(artifact.id, session, MagicMock(), actor)
        else:
            await app_route.download_packet(demo_application["id"], session, MagicMock(), actor)
    direct.assert_not_called()
    packet.assert_not_called()
