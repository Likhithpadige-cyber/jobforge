"""M10 Phase 2 - submission readiness, the policy fan-out, the submission
record, and the handoff packet, against a real database.

Reuses the M9/Phase-1 fixtures and helpers rather than re-deriving them:
`_ready_for_review`/`_generate_cover_letter` build a real application through
`awaiting_approval` with a real tailored resume and a real, claim-verified
cover letter exactly as `test_cover_letter_review.py` already does.

No worker process runs in these tests. Readiness is triggered the same way
Phase 1's tests trigger cover-letter generation: by loading the actual
`approval.granted` event a decision emitted and handing it to the handler
directly - the real pipeline, replayed synchronously.
"""

# Frozen backward-compatibility contract from private main 6945454.
# Original text-only approval calls and assertions remain unchanged.

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

from packages.core.models import (
    Application,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Artifact,
    Event,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
    User,
)
from packages.core.services import applications as apps_service
from packages.core.services import cover_letters as cl_service
from packages.core.services import resume_review
from packages.core.services import submission_readiness as sr
from packages.core.services.artifacts import render_resume
from packages.core.services.handoff_packet import (
    ResumeNotRenderedError,
    current_packet,
    generate_handoff_packet,
)
from packages.services.rendering import production_renderers
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.ids import uuid7
from tests.integration import test_cover_letter_review as clr
from tests.integration import test_resume_read_api as c1
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]

DOMAIN = "example.invalid"  # matches demo_job's URL (tests/integration/conftest.py)


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(tmp_path / "artifacts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _actor(session, demo_application) -> User:
    return (
        await session.execute(select(User).where(User.id == demo_application["user_id"]))
    ).scalar_one()


async def _get_application(session, application_id) -> Application:
    return (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one()


async def _approve_everything(
    session, *, demo_profile, demo_job, demo_application, facts, store, tmp_path
):
    """Resume approved and rendered, cover letter generated and approved,
    zero questions - an application ready for the readiness gate to decide."""
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    app_id = demo_application["id"]
    actor = await _actor(session, demo_application)

    cover_letter = await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)

    resume = await resume_review.latest_tailored_resume(session, app_id)
    await resume_review.approve(session, application_id=app_id, actor=actor)
    await render_resume(
        session,
        store=store,
        renderers=production_renderers(),
        resume_id=resume.id,
    )
    await session.flush()
    return resume, cover_letter, actor


async def _latest_approval_granted_event(session, application_id) -> Event:
    return (
        await session.execute(
            select(Event)
            .where(Event.type == "approval.granted", Event.correlation_id == application_id)
            .order_by(Event.occurred_at.desc(), Event.id.desc())
            .limit(1)
        )
    ).scalar_one()


async def _trigger_readiness(session, application_id, *, store) -> str:
    event = await _latest_approval_granted_event(session, application_id)
    return await sr.check_readiness_on_approval(session, event, store=store)


async def _allow_via_email(session):
    """The simplest ALLOWED verdict: `email` is permitted unconditionally
    once a policy row names it (`evaluate_submission` never gates it on
    `automation_allowed`, unlike `official_api`)."""
    session.add(
        SitePolicy(
            id=uuid7(),
            domain=DOMAIN,
            submission_method=SubmissionMethod.EMAIL,
            automation_allowed=False,
        )
    )
    await session.flush()


async def _require_human(session):
    session.add(
        SitePolicy(
            id=uuid7(),
            domain=DOMAIN,
            submission_method=SubmissionMethod.HUMAN_HANDOFF,
        )
    )
    await session.flush()


async def _block_explicitly(session):
    session.add(
        SitePolicy(
            id=uuid7(),
            domain=DOMAIN,
            submission_method=SubmissionMethod.BLOCKED,
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Readiness approval combinations
# ---------------------------------------------------------------------------


async def test_incomplete_approval_set_never_reaches_approved(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """M10 Phase 2 stabilization (part A): `resume_review.approve()` no
    longer transitions `applications.status` by itself - the shared
    completion gate (`review_completion.maybe_mark_application_approved`)
    only does so once RESUME, COVER_LETTER, and ANSWERS-if-required are all
    current-approved. Granting RESUME alone, with COVER_LETTER still
    undecided, must leave the application at `awaiting_approval` - never a
    transitional `approved`-but-incomplete state, and never a readiness
    computation, since `check_readiness_on_approval`'s own gate requires
    `approved` first."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    # Cover letter generated but not yet approved; approve only the resume.
    await resume_review.approve(session, application_id=app_id, actor=actor)

    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.AWAITING_APPROVAL

    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "not-approved"
    assert (await session.execute(select(Submission))).first() is None


async def test_zero_question_readiness_needs_no_answers_approval(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """The M9 interaction this phase must preserve: RESUME + COVER_LETTER
    approved and zero prepared questions is sufficient - no ANSWERS approval
    is required, and none was ever created to require."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    assert (
        await session.execute(
            select(Approval).where(
                Approval.application_id == app_id, Approval.kind == ApprovalKind.ANSWERS
            )
        )
    ).first() is None

    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.SUBMISSION_READY


# ---------------------------------------------------------------------------
# Staleness protection
# ---------------------------------------------------------------------------


async def test_stale_cover_letter_approval_cannot_satisfy_readiness(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """Two things in one test, because the second needs the first's setup.

    First, the real M9/Phase-1 rework cycle this phase must preserve: v1
    generated, changes requested, v2 generated, v2 approved - all of it
    happens before RESUME is ever approved, because `resume_review.approve`
    is the one action that closes the `awaiting_approval` window for every
    approval kind at once (a pre-existing property of the system, unchanged
    by this phase), so a cover-letter decision cannot be made after it.

    Second, the actual staleness check readiness relies on: there is no
    organic way in the current system to make a *real, granted* cover-letter
    approval stale afterward (nothing can create a v3 once resume approval
    has closed the window either), so - exactly like
    `test_stale_resume_approval_cannot_satisfy_readiness` - the binding
    check itself is isolated by corrupting the granted approval's own
    snapshot and restoring it, proving `cover_letter_snapshot_matches` is
    genuinely load-bearing rather than assumed to always pass.
    """
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)

    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    await cl_service.request_changes_cover_letter(
        session, application_id=app_id, actor=actor, note="Mention more detail."
    )
    cover_letter = await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=901),
        note="Mention more detail.",
        text_suffix=" More detail.",
    )
    assert cover_letter.version == 2
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)

    resume = await resume_review.latest_tailored_resume(session, app_id)
    await resume_review.approve(session, application_id=app_id, actor=actor)
    await render_resume(session, store=store, renderers=production_renderers(), resume_id=resume.id)

    # The real v2 approval, current and correct, satisfies readiness.
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    assert (await _get_application(session, app_id)).status is ApplicationStatus.SUBMISSION_READY

    # Now isolate the binding check: corrupt the granted approval's snapshot
    # to claim v1 instead of the current v2, and prove it can no longer
    # satisfy a fresh readiness computation.
    approval = (
        await session.execute(
            select(Approval).where(
                Approval.application_id == app_id,
                Approval.kind == ApprovalKind.COVER_LETTER,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
    ).scalar_one()
    correct_snapshot = dict(approval.subject_snapshot)
    approval.subject_snapshot = {**correct_snapshot, "cover_letter_version": 1}
    await session.flush()
    application = await _get_application(session, app_id)
    assert not await sr._readiness_satisfied(session, application)

    # Restored, the current-version approval satisfies it again.
    approval.subject_snapshot = correct_snapshot
    await session.flush()
    assert await sr._readiness_satisfied(session, application)


async def test_stale_resume_approval_cannot_satisfy_readiness(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """An approval bound to a resume id/version that is no longer current
    (`snapshot_matches` fails) must not satisfy the RESUME leg."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)

    # A genuine approval first - real status transition, real trigger
    # satisfaction - then corrupt only the snapshot afterward to simulate
    # what a stale binding would look like. There is no organic path in the
    # current system to make a real resume approval stale (C3 rework only
    # ever reaches `resume_generating` from `changes_requested`, which a
    # granted RESUME approval already precludes), so this isolates the
    # staleness check itself exactly as `test_resume_review.py`'s own
    # `approve_refuses_a_stale_resume` does.
    resume = await resume_review.latest_tailored_resume(session, app_id)
    await resume_review.approve(session, application_id=app_id, actor=actor)
    await render_resume(session, store=store, renderers=production_renderers(), resume_id=resume.id)

    approval = (
        await session.execute(
            select(Approval).where(
                Approval.application_id == app_id, Approval.kind == ApprovalKind.RESUME
            )
        )
    ).scalar_one()
    approval.subject_snapshot = {
        **approval.subject_snapshot,
        "resume_version": resume.version + 1,
    }
    await session.flush()

    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "incomplete"


async def test_stale_answers_snapshot_cannot_complete_review(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """M10 Phase 2 stabilization (part A/E): an ANSWERS approval whose
    content no longer matches the current answer (edited after approval)
    must not complete review - even when RESUME and COVER_LETTER are both
    genuinely current-approved. Fully organic: editing an answer stays legal
    while `awaiting_approval` (`edit_answer`'s own precondition), which is
    exactly the state the application must remain in - proving order
    independence and staleness protection in one flow, ANSWERS -> RESUME
    (refused) -> ANSWERS again (completes it)."""
    from packages.core.services import applications as apps_service

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)

    await c1._generate(session, demo_profile, demo_job, app_id, facts)
    await apps_service.prepare_application(
        session,
        application_id=app_id,
        questions=[
            apps_service.RawQuestion(question_text="What city do you live in?", input_type="text"),
        ],
    )
    await session.flush()
    actor = await _actor(session, demo_application)
    await resume_review.enter_review(session, application_id=app_id, actor=actor)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)
    await apps_service.approve_answers(session, application_id=app_id, actor=actor)

    # Edit while still `awaiting_approval` - `edit_answer`'s own precondition
    # - so the ANSWERS approval already granted now describes stale content.
    questions = await apps_service.existing_questions(session, app_id)
    await apps_service.edit_answer(
        session,
        application_id=app_id,
        answer_id=questions[0].answer.id,
        actor=actor,
        value="A different city entirely",
    )

    # RESUME is the last of the three original decisions, but with ANSWERS
    # now stale it must NOT complete the set - the application stays
    # awaiting_approval, and rendering (which requires an approved-or-later
    # status) correctly refuses.
    await resume_review.approve(session, application_id=app_id, actor=actor)
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.AWAITING_APPROVAL
    assert not await sr._readiness_satisfied(session, application)

    # Re-deciding ANSWERS with the now-current content is what completes the
    # set - RESUME and COVER_LETTER were already genuinely satisfied.
    await apps_service.approve_answers(session, application_id=app_id, actor=actor)
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.APPROVED
    assert await sr._readiness_satisfied(session, application)

    resume = await resume_review.latest_tailored_resume(session, app_id)
    await render_resume(session, store=store, renderers=production_renderers(), resume_id=resume.id)
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"


# ---------------------------------------------------------------------------
# Policy branches
# ---------------------------------------------------------------------------


async def test_allowed_stays_at_submission_ready(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.SUBMISSION_READY

    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    assert submission.attempt == 1
    assert submission.method is SubmissionMethod.EMAIL
    assert submission.result is SubmissionResult.PENDING
    assert submission.started_at is None
    assert submission.finished_at is None
    assert submission.http_status is None
    assert submission.external_reference is None
    assert submission.blocked_reason is None
    assert submission.policy_id is not None

    ready_events = list(
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.ready", Event.correlation_id == app_id
                )
            )
        ).scalars()
    )
    assert len(ready_events) == 1
    assert ready_events[0].payload["submission_method"] == "email"
    assert ready_events[0].payload["target_domain"] == DOMAIN

    assert (
        await session.execute(
            select(Event).where(
                Event.type.in_(
                    [
                        "submission.requested",
                        "submission.started",
                        "submission.succeeded",
                        "submission.failed",
                        "submission.handoff.required",
                    ]
                ),
                Event.correlation_id == app_id,
            )
        )
    ).first() is None


async def test_requires_human_reaches_manual_handoff(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _require_human(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.MANUAL_HANDOFF

    changed = list(
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.status.changed", Event.correlation_id == app_id
                )
            )
        ).scalars()
    )
    to_statuses = [e.payload["to_status"] for e in changed]
    assert "submission_ready" in to_statuses
    assert "manual_handoff" in to_statuses


async def test_blocked_reaches_policy_blocked(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _block_explicitly(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.POLICY_BLOCKED

    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    assert submission.blocked_reason == "ingestion_method_blocked"  # METHOD_BLOCKED

    blocked_events = list(
        (
            await session.execute(
                select(Event).where(Event.type == "policy.blocked", Event.correlation_id == app_id)
            )
        ).scalars()
    )
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["action"] == "submit"
    assert blocked_events[0].payload["domain"] == DOMAIN


async def test_missing_policy_fails_safe_to_blocked(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """No `site_policies` row at all - the existing gate's own fail-safe
    default, not a new behavior invented here."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.POLICY_BLOCKED
    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    assert submission.blocked_reason == "no_policy_for_domain"
    assert submission.policy_id is None


# ---------------------------------------------------------------------------
# Idempotency / recovery
# ---------------------------------------------------------------------------


async def test_replayed_approval_granted_after_readiness_is_a_safe_noop(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    first = await _trigger_readiness(session, app_id, store=store)
    assert first == "completed"

    submissions_before = (
        (await session.execute(select(Submission).where(Submission.application_id == app_id)))
        .scalars()
        .all()
    )
    events_before = len(
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.ready", Event.correlation_id == app_id
                )
            )
        )
        .scalars()
        .all()
    )

    second = await _trigger_readiness(session, app_id, store=store)
    assert second == "not-approved"

    submissions_after = (
        (await session.execute(select(Submission).where(Submission.application_id == app_id)))
        .scalars()
        .all()
    )
    events_after = len(
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.ready", Event.correlation_id == app_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(submissions_after) == len(submissions_before) == 1
    assert events_after == events_before == 1


async def test_submission_record_uniqueness(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """`compute_readiness` itself refuses a second attempt-1 row even if
    called directly a second time, matching the DB's own
    `uq_submissions_application_id_attempt` index."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    application = await _get_application(session, app_id)
    await sr.compute_readiness(session, application=application, store=store)
    await sr.compute_readiness(session, application=application, store=store)

    rows = (
        (await session.execute(select(Submission).where(Submission.application_id == app_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Handoff packet
# ---------------------------------------------------------------------------


async def test_packet_generation_is_deterministic_and_bound_to_current_versions(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"

    application = await _get_application(session, app_id)
    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    assert submission.packet_artifact_id is not None

    packet = await current_packet(session, app_id)
    assert packet is not None
    assert packet.id == submission.packet_artifact_id
    assert packet.kind == "handoff_packet"
    assert packet.content_type == "application/zip"

    # Replay with unchanged source material reuses the same artifact.
    again = await generate_handoff_packet(
        session,
        application=application,
        store=store,
        method="email",
        blocked_reason=None,
    )
    assert again.id == packet.id
    packet_rows = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.application_id == app_id, Artifact.kind == "handoff_packet"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(packet_rows) == 1

    import io
    import zipfile

    data = await store.get(packet.storage_key)
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert names == {
        "00-CHECKLIST.txt",
        "resume.pdf",
        "cover_letter.txt",
        "answers.txt",
        "job.json",
    }


async def test_packet_generation_without_a_rendered_resume_is_refused(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """The real race this guards: `resume.render.requested` fires the
    instant the resume is approved, but nothing here runs the render
    worker, so no `resume_pdf` artifact exists yet."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=900),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)
    await resume_review.approve(session, application_id=app_id, actor=actor)
    # No render_resume() call - the artifact does not exist yet.

    await _allow_via_email(session)
    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.SUBMISSION_READY

    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    # Readiness itself is never blocked on the packet.
    assert submission.packet_artifact_id is None
    assert await current_packet(session, app_id) is None

    with pytest.raises(ResumeNotRenderedError):
        await generate_handoff_packet(
            session,
            application=application,
            store=store,
            method="email",
            blocked_reason=None,
        )


async def test_packet_checklist_reflects_the_readiness_outcome(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _require_human(session)
    await _approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await _trigger_readiness(session, app_id, store=store)
    packet = await current_packet(session, app_id)
    data = await store.get(packet.storage_key)

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        checklist = zf.read("00-CHECKLIST.txt").decode()
    assert "Human action required" in checklist
    assert "does not and will not submit it automatically" in checklist


# ---------------------------------------------------------------------------
# Approval order independence (M10 Phase 2 stabilization, parts A/E)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        ("resume", "cover_letter", "answers"),
        ("cover_letter", "answers", "resume"),
        ("answers", "resume", "cover_letter"),
    ],
)
async def test_approval_order_does_not_matter(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store, order
):
    """Every required approval, in every order, ends at the same place:
    `approved` only once the last one is decided, whichever kind that is."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await c1._generate(session, demo_profile, demo_job, app_id, facts)
    await apps_service.prepare_application(
        session,
        application_id=app_id,
        questions=[
            apps_service.RawQuestion(question_text="What city do you live in?", input_type="text"),
        ],
    )
    await session.flush()
    actor = await _actor(session, demo_application)
    await resume_review.enter_review(session, application_id=app_id, actor=actor)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=800),
    )

    for step, kind in enumerate(order):
        if kind == "resume":
            await resume_review.approve(session, application_id=app_id, actor=actor)
        elif kind == "cover_letter":
            await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)
        elif kind == "answers":
            await apps_service.approve_answers(session, application_id=app_id, actor=actor)

        application = await _get_application(session, app_id)
        if step < 2:
            assert (
                application.status is ApplicationStatus.AWAITING_APPROVAL
            ), f"order {order}: completed early after step {step} ({kind})"
        else:
            assert (
                application.status is ApplicationStatus.APPROVED
            ), f"order {order}: did not complete after the final step ({kind})"


@pytest.mark.parametrize(
    "order",
    [
        ("resume", "cover_letter"),
        ("cover_letter", "resume"),
    ],
)
async def test_zero_question_approval_order_does_not_matter(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store, order
):
    """The zero-question equivalent: only two approvals are ever required,
    and either can be last."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=801),
    )

    for step, kind in enumerate(order):
        if kind == "resume":
            await resume_review.approve(session, application_id=app_id, actor=actor)
        else:
            await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)

        application = await _get_application(session, app_id)
        if step == 0:
            assert application.status is ApplicationStatus.AWAITING_APPROVAL
        else:
            assert application.status is ApplicationStatus.APPROVED


async def test_final_approval_causes_approved_exactly_once(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """No duplicate `application.status.changed` to `approved`, and replaying
    the completing decision (idempotent at the approval layer) does not
    attempt a second transition."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=802),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)
    await resume_review.approve(session, application_id=app_id, actor=actor)

    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.APPROVED

    approved_history = (
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.status.changed",
                    Event.correlation_id == app_id,
                    Event.payload["to_status"].as_string() == "approved",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(approved_history) == 1

    # Replaying the completing decision (resume approve, idempotent by its
    # own snapshot check) must not attempt a second transition.
    await resume_review.approve(session, application_id=app_id, actor=actor)
    approved_history_after = (
        (
            await session.execute(
                select(Event).where(
                    Event.type == "application.status.changed",
                    Event.correlation_id == app_id,
                    Event.payload["to_status"].as_string() == "approved",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(approved_history_after) == 1


# ---------------------------------------------------------------------------
# Packet retry (M10 Phase 2 stabilization, part C)
# ---------------------------------------------------------------------------


async def test_packet_becomes_available_through_resume_rendered_retry(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """The deterministic retry path: readiness runs before the resume PDF
    exists (packet deferred, `packet_artifact_id` NULL), then the existing
    `resume.rendered` event - the same one the render worker always emits -
    is what fills it in, with no new event vocabulary and no reliance on a
    user repeatedly polling `GET /packet`."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _allow_via_email(session)
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await clr._generate_cover_letter(
        session,
        application_id=app_id,
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=UUID(int=803),
    )
    await cl_service.approve_cover_letter(session, application_id=app_id, actor=actor)
    await resume_review.approve(session, application_id=app_id, actor=actor)
    # No render_resume() call yet - readiness must run before the PDF exists.

    outcome = await _trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    submission = (
        await session.execute(select(Submission).where(Submission.application_id == app_id))
    ).scalar_one()
    assert submission.packet_artifact_id is None

    # Now render the resume - the real event this emits is what the worker
    # would react to.
    resume = await resume_review.latest_tailored_resume(session, app_id)
    await render_resume(session, store=store, renderers=production_renderers(), resume_id=resume.id)
    rendered_event = (
        await session.execute(
            select(Event)
            .where(Event.type == "resume.rendered", Event.correlation_id == app_id)
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one()

    result = await sr.retry_packet_generation(session, rendered_event, store=store)
    assert result == "completed"

    await session.refresh(submission)
    assert submission.packet_artifact_id is not None
    packet = await current_packet(session, app_id)
    assert packet is not None
    assert packet.id == submission.packet_artifact_id

    # A second delivery of the same event (redelivery) is a safe no-op.
    result_again = await sr.retry_packet_generation(session, rendered_event, store=store)
    assert result_again == "nothing-to-retry"
    packets = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.application_id == app_id, Artifact.kind == "handoff_packet"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(packets) == 1


async def test_resume_rendered_before_readiness_is_a_safe_noop(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path, store
):
    """The far more common case: `resume.rendered` fires long before the
    application is anywhere near `submission_ready` (every ordinary resume
    render does this, including the very first one). `retry_packet_
    generation` must not create a `submissions` row or a packet prematurely
    - there is nothing to retry yet, because readiness has not run."""
    from packages.core.events import emit

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    application = await _get_application(session, app_id)
    assert application.status is ApplicationStatus.AWAITING_APPROVAL
    resume = await resume_review.latest_tailored_resume(session, app_id)

    # A synthetic resume.rendered, matching the real documented payload
    # shape - the ordinary render triggered by generation itself, well
    # before this application is anywhere near submission_ready.
    rendered_event = await emit(
        session,
        type="resume.rendered",
        aggregate_type="resume",
        aggregate_id=resume.id,
        correlation_id=app_id,
        actor_type="agent",
        payload={
            "resume_id": str(resume.id),
            "application_id": str(app_id),
            "resume_version": resume.version,
            "approval_id": None,
            "artifacts": [],
        },
    )
    result = await sr.retry_packet_generation(session, rendered_event, store=store)
    assert result == "not-ready"
    assert (await session.execute(select(Submission))).first() is None
    assert (
        await session.execute(
            select(Artifact).where(
                Artifact.application_id == app_id, Artifact.kind == "handoff_packet"
            )
        )
    ).first() is None
