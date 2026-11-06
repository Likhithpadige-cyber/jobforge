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
