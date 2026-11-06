"""Cowork H3 residual: edits followed by verification must not revive old consent."""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from apps.api.routers import applications as app_route
from apps.api.routers import artifacts as artifact_route
from packages.core.models import Approval, ApprovalKind, Artifact
from packages.core.repositories import FactRepository
from packages.core.services.artifacts import render_resume
from packages.core.services.handoff_packet import generate_manual_packet
from packages.services.rendering.pdf import PdfResumeRenderer
from packages.shared.clock import utc_today
from packages.shared.errors import ConflictError
from tests.integration import test_resume_export as fixtures

inputs = fixtures.inputs
store = fixtures.store
prepare = fixtures.prepare

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]
OPERATIONS = ["new_render", "manual", "cached_manual", "pdf", "docx", "legacy_packet"]
MUTATIONS = [
    "unverify",
    "edit_unverified",
    "edit_reverified",
    "expire_reverified",
    "retire",
    "delete",
]


async def exercise(session, resume, app, owner, store, operation):
    if operation == "new_render":
        return await render_resume(
            session,
            store=store,
            resume_id=resume.id,
            renderers=(PdfResumeRenderer("ats-single-column/2.1/h3-new-render"),),
        )
    if operation in {"manual", "cached_manual"}:
        return await generate_manual_packet(session, application=app, store=store)
    with (
        patch.object(artifact_route, "artifact_store", return_value=store),
        patch.object(app_route, "artifact_store", return_value=store),
    ):
        if operation == "legacy_packet":
            return await app_route.download_packet(app.id, session, MagicMock(), owner)
        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.resume_id == resume.id,
                Artifact.kind == "resume_" + operation,
                Artifact.template_version == "ats-single-column/2.1",
            )
        )
        return await artifact_route.download_artifact(artifact.id, session, MagicMock(), owner)


async def setup(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation
):
    resume, app, owner = await prepare(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
    )
    if operation == "cached_manual":
        await generate_manual_packet(session, application=app, store=store)
    cited = {
        str(fid)
        for section in resume.content.values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    fact = next(f for f in inputs[2] if str(f.id) in cited)
    return resume, app, owner, fact


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("mutation", MUTATIONS)
async def test_six_evidence_mutations_refuse_all_export_paths(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation, mutation
):
    from datetime import timedelta

    resume, app, owner, fact = await setup(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation
    )
    repo = FactRepository(session)
    if mutation == "retire":
        await repo.retire(fact.id)
    elif mutation == "delete":
        await repo.soft_delete(fact.id)
    else:
        await repo.unverify(fact.id)
        if mutation.startswith("edit"):
            await repo.update(fact, text="Synthetic correction: the earlier claim was overstated.")
        if mutation == "expire_reverified":
            await repo.update(fact, valid_to=utc_today() - timedelta(days=1))
        if mutation.endswith("reverified"):
            await repo.verify(fact.id, verified_by=owner.id)
    with pytest.raises(ConflictError):
        await exercise(session, resume, app, owner, store, operation)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("reverify", [False, True])
async def test_unchanged_evidence_succeeds_even_after_reverification(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation, reverify
):
    resume, app, owner, fact = await setup(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation
    )
    if reverify:
        repo = FactRepository(session)
        await repo.unverify(fact.id)
        await repo.verify(fact.id, verified_by=owner.id)
    assert await exercise(session, resume, app, owner, store, operation) is not None


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("damage", ["absent", "invalid"])
async def test_missing_or_invalid_required_fingerprint_fails_closed(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation, damage
):
    from packages.core.services.resume_export import export_binding

    resume, app, owner, fact = await setup(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation
    )
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == app.id, Approval.kind == ApprovalKind.RESUME
        )
    )
    altered = deepcopy(approval.subject_snapshot)
    fp = altered["export_document"].setdefault(
        "evidence_fingerprints", {"schema_version": 1, "facts": {}}
    )
    if damage == "absent":
        fp["facts"].pop(str(fact.id), None)
    else:
        fp["facts"][str(fact.id)] = {"version": "invalid", "sha256": "invalid"}
    # Keep the outer consent digest valid: test the evidence binding itself.
    altered["export_digest"] = export_binding(resume, altered["export_document"])
    approval.subject_snapshot = altered
    await session.flush()
    with pytest.raises(ConflictError):
        await exercise(session, resume, app, owner, store, operation)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("damage", ["missing_container", "bad_schema"])
async def test_fingerprint_container_itself_is_required(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation, damage
):
    from packages.core.services.resume_export import export_binding

    resume, app, owner, _ = await setup(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, operation
    )
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == app.id, Approval.kind == ApprovalKind.RESUME
        )
    )
    altered = deepcopy(approval.subject_snapshot)
    if damage == "missing_container":
        altered["export_document"].pop("evidence_fingerprints")
    else:
        altered["export_document"]["evidence_fingerprints"]["schema_version"] = 99
    altered["export_digest"] = export_binding(resume, altered["export_document"])
    approval.subject_snapshot = altered
    await session.flush()
    with pytest.raises(ConflictError, match="fingerprint"):
        await exercise(session, resume, app, owner, store, operation)


async def test_old_contact_consent_cannot_be_silently_refingerprinted(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.services import resume_review
    from packages.core.services.resume_export import export_binding, preview_export

    resume, app, owner, _ = await setup(
        session, demo_profile, demo_job, demo_application, inputs, store, tmp_path, "manual"
    )
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == app.id, Approval.kind == ApprovalKind.RESUME
        )
    )
    altered = deepcopy(approval.subject_snapshot)
    altered.pop("evidence_fingerprints", None)
    altered["export_document"].pop("evidence_fingerprints")
    altered["export_digest"] = export_binding(resume, altered["export_document"])
    approval.subject_snapshot = altered
    await session.flush()
    before = deepcopy(approval.subject_snapshot)
    preview = await preview_export(session, resume, owner)
    with pytest.raises(ConflictError, match="immutable evidence history"):
        await resume_review.approve(
            session, application_id=app.id, actor=owner, export_digest=preview["digest"]
        )
    assert approval.subject_snapshot == before and resume.version == 1


async def test_dangling_legacy_source_repair_never_relinks_an_approved_resume(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.services.profile_service import ProfileService
    from packages.core.services.resume_export import preview_export
    from packages.shared.ids import uuid7
    from tests.integration import test_legacy_m166_readiness as legacy

    facts = inputs[2]
    repo = FactRepository(session)
    old = facts[0]
    valid_source = old.source_id
    await repo.unverify(old.id)
    await repo.update(old, source_id=uuid7())
    await repo.verify(old.id, verified_by=demo_application["user_id"])
    resume, _, owner = await legacy._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    before = deepcopy(resume.content)
    with pytest.raises(ConflictError, match="repair-source"):
        await preview_export(session, resume, owner)
    await session.refresh(old)
    replacement = await ProfileService(session).repair_fact_source(
        profile_id=old.profile_id,
        fact_id=old.id,
        source_id=valid_source,
        expected_updated_at=old.updated_at,
        note="Owner selected correct historical employment",
        actor=owner,
    )
    await repo.verify(replacement.id, verified_by=owner.id)
    with pytest.raises(ConflictError, match="inactive evidence"):
        await preview_export(session, resume, owner)
    assert resume.content == before and resume.version == 1
    assert replacement.id != old.id


async def test_reviewer_text_approval_cannot_be_laundered_by_edit_then_owner_consent(
    session, demo_profile, demo_job, demo_application, inputs, store, tmp_path
):
    from packages.core.services import resume_review
    from packages.core.services.resume_export import preview_export
    from tests.integration import test_legacy_m166_readiness as legacy

    resume, _, owner = await legacy._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=inputs[2],
        store=store,
        tmp_path=tmp_path,
    )
    approval = await session.scalar(
        select(Approval).where(
            Approval.application_id == demo_application["id"], Approval.kind == ApprovalKind.RESUME
        )
    )
    before = deepcopy(approval.subject_snapshot)
    cited = {
        str(fid)
        for section in resume.content.values()
        for entry in section.values()
        for fid in entry.get("fact_ids", [])
    }
    fact = next(f for f in inputs[2] if str(f.id) in cited)
    repo = FactRepository(session)
    await repo.unverify(fact.id)
    await repo.update(fact, text="Synthetic correction after prose approval")
    await repo.verify(fact.id, verified_by=owner.id)
    preview = await preview_export(session, resume, owner)
    with pytest.raises(ConflictError, match="content changed"):
        await resume_review.approve(
            session,
            application_id=demo_application["id"],
            actor=owner,
            export_digest=preview["digest"],
        )
    assert approval.subject_snapshot == before
