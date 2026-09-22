"""C3 - regeneration after a reviewer requested changes, against a real DB.

The whole pipeline runs for real here: B0 loads, B1 selects with the reviewer's
note in its untrusted payload, B2 drafts, B3 summarises, B5 verifies every
claim, B6 persists. Only the four model calls are replayed, from cassettes this
module writes itself - which is what makes the adversarial cases meaningful.
A test that stubbed out B2 could not show that the numeral guard refuses a
figure the reviewer asked for, because the guard lives inside B2.

The note travels exactly as far as B1 and no further. That is the property
most of this file exists to pin, from both directions: the guidance reaches
selection, and nothing downstream is relaxed to accommodate it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from packages.agents.llm.runtime import Response, StructuredClient, canonical
from packages.agents.resume_optimizer import drafting, summary, verification
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import NumeralGuardError
from packages.agents.resume_optimizer.handoff import bullet_refs, sentence_refs
from packages.agents.resume_optimizer.revision import (
    RevisionNotAvailableError,
    guidance_payload,
    load_revision_request,
    revision_fingerprint,
)
from packages.agents.resume_optimizer.selection import (
    prepare_request as prepare_b1_request,
)
from packages.agents.resume_optimizer.selection import validate_selection
from packages.core.db.audit_boundary import AuditContext
from packages.core.db.inference_audit import IndependentInferenceAudit
from packages.core.models import (
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    Approval,
    ApprovalStatus,
    ClaimVerdict,
    Event,
    Resume,
    ResumeClaim,
)
from packages.core.repositories.revision_attempts import RevisionAttemptRepository
from packages.core.services import applications as applications_service
from packages.core.services import resume_review, resume_revision, resume_rework
from packages.core.services.resume_revision import (
    FINGERPRINT_KEY,
    generate_revision,
)
from packages.shared.config import Settings
from tests.integration import test_resume_review as c2
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs


pytestmark = [
    pytest.mark.integration, pytest.mark.safety, pytest.mark.usefixtures("audit_boundary_engine")
]

TIGHTEN = "Tighten the summary; it over-claims scope."


def _response(body: dict) -> Response:
    return Response(
        body=canonical(body),
        input_tokens=50,
        output_tokens=20,
        cost_usd=Decimal("0.001"),
        stop_reason="tool_use",
    )


async def _changes_requested(session, demo_profile, demo_job, demo_application, facts,
                             note=TIGHTEN):
    """Drive C1+C2 for real until the application sits in `changes_requested`.

    Built through C2 review actions rather than by writing rows: the
    approval this revision will be loaded from has to be the one C2 produces,
    subject snapshot and all, or the staleness check being tested here would be
    testing a fixture.
    """
    await c2._ready(session, demo_profile, demo_job, demo_application, facts)
    actor = await c2._actor(session, demo_application)
    await c2._start(session, demo_application["id"], actor)
    await c2._request_changes(session, demo_application["id"], actor, note)
    return actor


async def _stage_cassettes(
    session,
    tmp_path,
    client,
    request,
    *,
    bullet_text: str,
    bullet_numerals: list[str] | None = None,
    sentences: list[str] | None = None,
    verdict: str = "supported",
):
    """Pre-record every model response the revision will ask for.

    The four requests are computable in advance because each stage's
    `prepare_request` is pure and each depends only on what the previous stage
    returned - which is decided here. So the run is fully determined, and any
    difference in what the pipeline actually asks for shows up as a replay miss
    rather than as a silently different result.
    """
    context = await load_tailoring_context(
        session, profile_id=request.profile_id, job_id=request.job_id
    )
    fact_id = str(context.facts[0].id)

    b1_request, _ = prepare_b1_request(
        context, client.model, revision=guidance_payload(request)
    )
    b1_body = {
        "selections": [
            {
                "fact_id": fact_id,
                "section": "experience",
                "order": 1,
                "emphasis": "normal",
            }
        ]
    }
    (tmp_path / f"{b1_request.digest}.json").write_text(
        _response(b1_body).model_dump_json()
    )

    selection = validate_selection(
        canonical(b1_body), context, prompt_version=b1_request.prompt_version
    )

    b2_request = drafting.prepare_request(context, selection, client.model)
    b2_body = {
        "bullets": [
            {
                "text": bullet_text,
                "fact_ids": [fact_id],
                "numerals_used": bullet_numerals or [],
            }
        ]
    }
    (tmp_path / f"{b2_request.digest}.json").write_text(
        _response(b2_body).model_dump_json()
    )

    b3_request = summary.prepare_request(context, selection, client.model)
    texts = sentences or [
        "Cloud engineer with verified AWS depth.",
        "Focused on infrastructure delivery.",
    ]
    b3_body = {
        "sentences": [
            {"text": text, "fact_ids": [fact_id], "numerals_used": []}
            for text in texts
        ]
    }
    (tmp_path / f"{b3_request.digest}.json").write_text(
        _response(b3_body).model_dump_json()
    )

    # B5 runs once per claim, so a cassette is needed for each.
    bullets = tuple(drafting.DraftedBullet(**b) for b in b2_body["bullets"])
    sentence_models = tuple(summary.SummarySentence(**s) for s in b3_body["sentences"])
    refs = bullet_refs(bullets, selection) + sentence_refs(sentence_models)
    for ref in refs:
        cited = [f for f in context.facts if str(f.id) in ref.fact_ids]
        b5_request = verification.prepare_request(ref.claim_text, cited, client.model)
        reason = (
            "Matches the cited fact."
            if verdict == "supported"
            else "The cited fact does not support this."
        )
        (tmp_path / f"{b5_request.digest}.json").write_text(
            _response({"verdict": verdict, "explanation": reason}).model_dump_json()
        )
    return context, selection


async def _revise(session, tmp_path, demo_application, actor, **staging):
    """Exercise T3 with a durable attempt and committed, lease-owned T2 state."""
    app_id = demo_application["id"]
    actor_id = actor.id
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    request = await load_revision_request(session, app_id)
    await _stage_cassettes(session, tmp_path, client, request, **staging)
    attempt, request = await resume_rework._t1_decide_and_record(
        session, application_id=app_id, approval_id=request.approval_id,
        actor=actor, instructions=request.instructions,
    )
    # Explicit T2 prerequisites; T3 must neither acquire a lease nor decide reuse.
    await resume_review._load_application(session, app_id, lock=True)
    settings = Settings()
    epoch = await RevisionAttemptRepository(session).acquire(
        attempt.id, owner="test-t3", lease_seconds=settings.revision_lease_ttl_seconds,
        max_attempts=settings.revision_max_attempts,
    )
    assert epoch is not None
    await resume_revision.begin_generation(
        session, application_id=app_id, request=request, actor_id=actor_id
    )
    await session.commit()
    result = await generate_revision(
        session, application_id=app_id, client=client, request=request,
        writer=IndependentInferenceAudit(context=AuditContext(
            attempt_id=attempt.id, lease_epoch=epoch, application_id=app_id,
            approval_id=request.approval_id,
        )),
    )
    if result.application_status is ApplicationStatus.RESUME_READY:
        # M9. A reworked resume lands back at resume_ready like any other
        # generation; the owner re-triggers question preparation the same
        # explicit way they did the first time (no auto mode) before review
        # can reopen. Zero questions here too - this suite tests resume
        # rework, not M9's own extraction.
        await applications_service.prepare_application(
            session, application_id=app_id, questions=[]
        )
    return result


async def _rework(session, tmp_path, demo_application, actor, **staging):
    """Exercise decisions and reuse through the real orchestration entry point."""
    app_id = demo_application["id"]
    request = await load_revision_request(
        session, app_id, require_changes_requested=False
    )
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    if staging:
        await _stage_cassettes(session, tmp_path, client, request, **staging)
    return await resume_rework.request_changes_and_regenerate(
        session, application_id=app_id, approval_id=request.approval_id,
        actor=actor, note=request.instructions, client=client,
    )


async def _resumes(session, application_id) -> list[Resume]:
    return list(
        (
            await session.execute(
                select(Resume)
                .where(Resume.application_id == application_id, Resume.kind == "tailored")
                .order_by(Resume.version)
            )
        )
        .scalars()
        .all()
    )


async def _application_status(session, application_id) -> ApplicationStatus:
    return (
        (
            await session.execute(
                select(Application).where(Application.id == application_id)
            )
        )
        .scalar_one()
        .status
    )


# ---------------------------------------------------------------------------
# The revision input boundary
# ---------------------------------------------------------------------------


async def test_the_reviewer_note_is_loaded_from_the_database(
    session, demo_profile, demo_job, demo_application, inputs
):
    """The note a revision runs against is the one a human stored.

    Nothing in the call supplies it, which is the point: there is no argument
    a caller could use to substitute different instructions.
    """
    _base, _evaluation, facts = inputs
    await _changes_requested(session, demo_profile, demo_job, demo_application, facts)

    request = await load_revision_request(session, demo_application["id"])

    assert request.instructions == TIGHTEN
    assert request.prior_resume_version == 1
    approval = (
        await session.execute(
            select(Approval).where(Approval.id == request.approval_id)
        )
    ).scalar_one()
    assert approval.status is ApprovalStatus.CHANGES_REQUESTED
    assert approval.subject_snapshot["resume_id"] == str(request.prior_resume_id)


async def test_the_prior_claims_are_carried_without_their_verdicts(
    session, demo_profile, demo_job, demo_application, inputs
):
    """B1 is shown what the last version said, not that it was approved."""
    _base, _evaluation, facts = inputs
    await _changes_requested(session, demo_profile, demo_job, demo_application, facts)

    request = await load_revision_request(session, demo_application["id"])
    payload = guidance_payload(request)

    assert len(payload["prior_claims"]) == 3
    for claim in payload["prior_claims"]:
        assert set(claim) == {"section", "path", "claim_text", "fact_ids"}


async def test_a_revision_is_refused_outside_changes_requested(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    await c2._ready(session, demo_profile, demo_job, demo_application, facts)

    with pytest.raises(RevisionNotAvailableError, match="changes_requested"):
        await load_revision_request(session, demo_application["id"])


async def test_a_foreign_newer_version_blocks_a_revision(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The C2 staleness rule, the other way round.

    A decision made about v1 cannot drive a revision once some *other* v2
    exists: the human read text that is no longer what would be submitted.

    The check lives in the service rather than in the loader, and deliberately
    so: the loader cannot tell a foreign v2 from the v2 this very revision
    produced, and conflating those two is what would make a retry look stale.
    """
    _base, _evaluation, facts = inputs
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    await c2._supersede_with_a_newer_version(session, demo_application["id"])

    # The loader still answers "what would this be built from" - that question
    # is about the version the human read, and it has not changed.
    request = await load_revision_request(session, demo_application["id"])
    assert request.prior_resume_version == 1

    with pytest.raises(RevisionNotAvailableError, match="already exists"):
        await _rework(session, tmp_path, demo_application, actor)


# ---------------------------------------------------------------------------
# The happy path and versioning
# ---------------------------------------------------------------------------


async def test_a_revision_produces_a_new_verified_version(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )

    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
        sentences=["Cloud engineer with AWS depth.", "Focused on delivery."],
    )

    assert result.version == 2
    assert result.prior_resume_version == 1
    assert result.verification_status == "passed"
    # generate_revision's own result is captured before the M9 gate the
    # _revise test helper runs immediately after it - resume_ready is what
    # generation itself produced.
    assert result.application_status is ApplicationStatus.RESUME_READY
    assert result.reused is False
    # By the time _revise returns, M9's zero-question prepare has already
    # advanced the application past the point generate_revision left it.
    assert (
        await _application_status(session, app_id)
    ) is ApplicationStatus.QUESTIONS_PENDING


async def test_v1_and_its_claims_survive_untouched(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """Append-only means append-only. v1 is evidence of what was reviewed."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    v1 = (await _resumes(session, app_id))[0]
    before = {
        "id": v1.id,
        "version": v1.version,
        "content": dict(v1.content),
        "status": v1.verification_status,
        "claims": sorted(
            (c.path, c.claim_text, c.verdict) for c in await c2._claims(session, v1.id)
        ),
    }

    await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    versions = await _resumes(session, app_id)
    assert [r.version for r in versions] == [1, 2]
    after = versions[0]
    assert after.id == before["id"]
    assert after.content == before["content"]
    assert after.verification_status == before["status"]
    assert (
        sorted((c.path, c.claim_text, c.verdict) for c in await c2._claims(session, after.id))
        == before["claims"]
    )


async def test_v2_gets_its_own_claims_and_records_what_it_was_revised_from(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    request = await load_revision_request(session, app_id)

    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work, tightened.",
    )

    v1, v2 = await _resumes(session, app_id)
    v1_claims = await c2._claims(session, v1.id)
    v2_claims = await c2._claims(session, v2.id)
    assert {c.id for c in v1_claims}.isdisjoint({c.id for c in v2_claims})
    assert any(
        c.claim_text == "Delivered AWS infrastructure work, tightened."
        for c in v2_claims
    )
    provenance = v2.diff_vs_base
    assert provenance[FINGERPRINT_KEY] == revision_fingerprint(request)
    assert provenance["revised_from_version"] == 1
    assert provenance["changes_requested_approval_id"] == str(request.approval_id)
    assert result.claim_count == len(v2_claims)


async def test_the_rework_transition_is_recorded(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """changes_requested -> resume_generating -> resume_ready, all in history."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )

    await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    rows = (
        (
            await session.execute(
                select(ApplicationStatusHistory).where(
                    ApplicationStatusHistory.application_id == app_id
                )
            )
        )
        .scalars()
        .all()
    )
    edges = {(row.from_status, row.to_status) for row in rows}
    assert (
        ApplicationStatus.CHANGES_REQUESTED,
        ApplicationStatus.RESUME_GENERATING,
    ) in edges
    assert (
        ApplicationStatus.RESUME_GENERATING,
        ApplicationStatus.RESUME_READY,
    ) in edges
    rework = next(
        row for row in rows if row.to_status is ApplicationStatus.RESUME_GENERATING
        and row.from_status is ApplicationStatus.CHANGES_REQUESTED
    )
    assert rework.actor_type == "human"
    assert rework.actor_id == str(actor.id)


# ---------------------------------------------------------------------------
# The truth boundary
# ---------------------------------------------------------------------------


async def test_a_reviewer_cannot_buy_a_numeral_the_evidence_lacks(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """"Add that I increased revenue by 40%" - and the model obliges.

    The note is honoured all the way into B2's output, and the numeral guard
    still refuses it, because the guard compares against the cited fact's
    `structured` payload and never against what anybody asked for. This is the
    single most important test in C3: it proves the boundary holds even when
    every earlier stage cooperates with the reviewer.
    """
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session,
        demo_profile,
        demo_job,
        demo_application,
        facts,
        note="Add that I increased revenue by 40%",
    )

    with pytest.raises(NumeralGuardError):
        await _revise(
            session,
            tmp_path,
            demo_application,
            actor,
            bullet_text="Increased revenue by 40% on AWS infrastructure.",
            bullet_numerals=["40%"],
        )

    # Nothing was written: no v2, and v1 is still what it was.
    assert len(await _resumes(session, app_id)) == 1

    # And it is recorded as what it is. B2 classifies a numeral with no
    # supporting fact as `fabrication` at critical severity - the same verdict
    # it would reach for a poisoned job description, because a reviewer asking
    # for it changes nothing about what it is.
    violations = (
        (
            await session.execute(
                select(Event).where(
                    Event.type == "guardrail.violation.detected",
                    Event.aggregate_id == demo_job,
                )
            )
        )
        .scalars()
        .all()
    )
    failures = [e.payload.get("failure", e.payload) for e in violations]
    assert any(
        f.get("kind") == "fabrication" and f.get("step") == "B2" for f in failures
    ), failures


async def test_an_unsupported_claim_fails_verification_rather_than_passing(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """A title the facts do not support reaches B5, and B5 refuses it.

    The version is still written - a failed attempt is a durable record, as in
    B6 - but it lands in `verification_failed` with the claim marked, never in
    `resume_ready`.
    """
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session,
        demo_profile,
        demo_job,
        demo_application,
        facts,
        note="Change my title to Senior Cloud Architect",
    )

    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Served as Senior Cloud Architect for the platform.",
        verdict="unsupported",
    )

    assert result.verification_status == "failed"
    assert result.application_status is ApplicationStatus.VERIFICATION_FAILED
    v2 = (await _resumes(session, app_id))[1]
    assert v2.verification_status == "failed"
    assert any(
        c.verdict is ClaimVerdict.UNSUPPORTED for c in await c2._claims(session, v2.id)
    )
    # v1 is untouched and the human's decision still stands.
    assert (await _resumes(session, app_id))[0].verification_status == "passed"
    approval = (
        await session.execute(
            select(Approval).where(Approval.application_id == app_id)
        )
    ).scalar_one()
    assert approval.status is ApprovalStatus.CHANGES_REQUESTED


async def test_the_factual_demand_is_reported_not_silently_dropped(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The reviewer is told which part of their note could not be honoured."""
    _base, _evaluation, facts = inputs
    actor = await _changes_requested(
        session,
        demo_profile,
        demo_job,
        demo_application,
        facts,
        note="Say I have 8 years of AWS",
    )

    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    assert "asserts_experience" in result.unsupported_instruction_kinds
    assert "asserts_duration" in result.unsupported_instruction_kinds


async def test_the_note_reaches_b1_and_nothing_downstream(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The containment claim, asserted against the actual prompts.

    B1's request carries the note; B2's and B3's do not, and their prompts
    still truthfully state that no untrusted data is present. If a future
    change started threading the note into drafting, this fails.
    """
    _base, _evaluation, facts = inputs
    note = "Lead with the infrastructure work."
    await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts, note=note
    )
    request = await load_revision_request(session, demo_application["id"])
    client = StructuredClient(Settings(), cassette_dir=tmp_path)
    context, selection = await _stage_cassettes(
        session,
        tmp_path,
        client,
        request,
        bullet_text="Delivered AWS infrastructure work.",
    )

    b1_request, _ = prepare_b1_request(
        context, client.model, revision=guidance_payload(request)
    )
    assert "infrastructure work" in b1_request.user

    b2_request = drafting.prepare_request(context, selection, client.model)
    b3_request = summary.prepare_request(context, selection, client.model)
    assert note not in b2_request.user
    assert note not in b3_request.user
    assert "reviewer_instructions" not in b2_request.user
    assert "reviewer_instructions" not in b3_request.user


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_a_retried_revision_returns_the_same_version(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The retry finds its own earlier result rather than producing v3."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    first = await _rework(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    second = await _rework(session, tmp_path, demo_application, actor)
    assert second.resume_id == first.resume_id
    assert second.resume_version == first.resume_version == 2
    assert len(await _resumes(session, app_id)) == 2


async def test_a_retry_adds_no_history_or_events(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    await _rework(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )
    history = await c2._history_count(session, app_id)
    events = (
        await session.execute(
            select(func.count(Event.id)).where(Event.aggregate_id == app_id)
        )
    ).scalar_one()

    reused = await _rework(session, tmp_path, demo_application, actor)
    assert reused.resume_version == 2

    assert await c2._history_count(session, app_id) == history
    assert (
        await session.execute(
            select(func.count(Event.id)).where(Event.aggregate_id == app_id)
        )
    ).scalar_one() == events


async def test_the_fingerprint_is_what_recognises_a_retry(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """Not "is there a newer version" - that would confuse two different things.

    A later revision of a later version is a genuinely new revision and must be
    allowed to produce v3. Only the same application, at the same prior
    version, under the same recorded decision, is the same revision.
    """
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    request = await load_revision_request(session, app_id)
    await _rework(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    v2 = (await _resumes(session, app_id))[1]
    assert v2.diff_vs_base[FINGERPRINT_KEY] == revision_fingerprint(request)
    # A row without the marker is not this revision's work.
    assert resume_revision.FINGERPRINT_KEY not in (
        (await _resumes(session, app_id))[0].diff_vs_base or {}
    )

    reused = await _rework(session, tmp_path, demo_application, actor)
    assert reused.resume_id == v2.id
    assert reused.resume_version == v2.version
    assert len(await _resumes(session, app_id)) == 2


# ---------------------------------------------------------------------------
# The next review cycle
# ---------------------------------------------------------------------------


async def test_v2_inherits_no_approval_and_needs_a_fresh_review(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """A new version is un-reviewed, whatever was decided about the old one."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )

    approvals = await c2._approvals(session, app_id)
    assert len(approvals) == 1
    assert approvals[0].status is ApprovalStatus.CHANGES_REQUESTED

    state = await c2.service.review_state(session, app_id)
    assert state.review_status == "changes_requested"
    assert state.can_start_review is True
    assert state.can_approve is False


async def test_starting_review_again_binds_a_new_approval_to_v2(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The old decision stays bound to v1; the new one is about v2 alone."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    v1 = (await _resumes(session, app_id))[0]
    old_approval = (await c2._approvals(session, app_id))[0]
    old_snapshot = dict(old_approval.subject_snapshot)

    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )
    await c2._start(session, app_id, actor)

    approvals = await c2._approvals(session, app_id)
    assert len(approvals) == 2
    historical = next(a for a in approvals if a.id == old_approval.id)
    fresh = next(a for a in approvals if a.id != old_approval.id)

    assert historical.subject_snapshot == old_snapshot
    assert historical.subject_snapshot["resume_id"] == str(v1.id)
    assert historical.status is ApprovalStatus.CHANGES_REQUESTED
    assert fresh.status is ApprovalStatus.PENDING
    assert fresh.subject_snapshot["resume_id"] == str(result.resume_id)
    assert fresh.subject_snapshot["resume_version"] == 2


async def test_v2_can_be_approved_independently(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )
    result = await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Delivered AWS infrastructure work.",
    )
    await c2._start(session, app_id, actor)

    state = await c2._approve(session, app_id, actor, note="Better.")

    assert state.application_status is ApplicationStatus.APPROVED
    assert state.resume_version == 2
    assert state.resume_id == result.resume_id
    # The v1 decision is still exactly what the human recorded.
    approvals = await c2._approvals(session, app_id)
    assert approvals[0].status is ApprovalStatus.CHANGES_REQUESTED
    assert approvals[0].changes_requested == TIGHTEN


async def test_a_failed_revision_leaves_no_claims_marked_passed(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """Defence in depth on the one thing that must never be wrong."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    actor = await _changes_requested(
        session, demo_profile, demo_job, demo_application, facts
    )

    await _revise(
        session,
        tmp_path,
        demo_application,
        actor,
        bullet_text="Served as Senior Cloud Architect.",
        verdict="unsupported",
    )

    v2 = (await _resumes(session, app_id))[1]
    passed = (
        await session.execute(
            select(func.count(ResumeClaim.id)).where(
                ResumeClaim.resume_id == v2.id,
                ResumeClaim.verdict == ClaimVerdict.SUPPORTED,
            )
        )
    ).scalar_one()
    assert v2.verification_status == "failed"
    assert passed == 0
