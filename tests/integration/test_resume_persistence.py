"""B6 against a real database: rows, events, idempotency, rollback.

The transaction convention matters here. The `session` fixture holds one outer
transaction that every fixture row lives in, so a test that needs to undo only
B6's writes takes a savepoint and rolls back through *its* handle - rolling the
session back would discard the application the test is about.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.drafting import DraftedBullet
from packages.agents.resume_optimizer.handoff import bind, bullet_refs, sentence_refs
from packages.agents.resume_optimizer.resume_persistence import (
    WrongApplicationStateError,
    persist_resume,
    persist_resume_once,
)
from packages.agents.resume_optimizer.selection import Selection, SelectionResult
from packages.agents.resume_optimizer.summary import SummarySentence
from packages.agents.resume_optimizer.verification import VerificationResult
from packages.core.models import (
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    ClaimVerdict,
    Event,
    Resume,
    ResumeClaim,
)
from packages.core.state_machines.application import transition_application
from packages.shared.ids import uuid7
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = pytest.mark.integration

B6_EVENTS = (
    "resume.generated",
    "resume.render.requested",
    "resume.verification.failed",
)


async def _ready(session, application_id):
    """Put the application where B6 expects to find it, via the state machine."""
    application = (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one()
    await transition_application(
        session,
        application,
        ApplicationStatus.RESUME_GENERATING,
        actor_id="resume_optimizer",
    )
    await session.flush()
    return application


def _fixture(facts):
    """B1/B2/B3 shapes built on the one verified, non-deleted fact."""
    fact_id = str(facts[0].id)
    selection = SelectionResult(
        selections=(Selection(fact_id=fact_id, section="experience", order=1, emphasis="normal"),),
        evaluation_gaps=(),
        material_gaps=(),
        requires_human_review=False,
        prompt_version="b1-test",
    )
    bullets = (
        DraftedBullet(
            text="Delivered AWS infrastructure work.",
            fact_ids=[fact_id],
            numerals_used=[],
        ),
    )
    sentences = (
        SummarySentence(text="Cloud engineer with verified AWS depth.", fact_ids=[fact_id]),
        SummarySentence(text="Focused on infrastructure delivery.", fact_ids=[fact_id]),
    )
    return selection, bullets, sentences


def _verified(bullets, sentences, selection, context, verdicts=None):
    refs = bullet_refs(bullets, selection) + sentence_refs(sentences)
    by_id = {str(f.id): f for f in context.facts}
    out = []
    for index, ref in enumerate(refs):
        verdict = ClaimVerdict.SUPPORTED
        explanation = "Matches the cited fact."
        if verdicts and index in verdicts:
            verdict, explanation = verdicts[index]
        out.append(
            bind(
                ref,
                VerificationResult(verdict=verdict, explanation=explanation, prompt_version="b5"),
                [by_id[fid] for fid in ref.fact_ids],
            )
        )
    return tuple(out)


async def _setup(session, demo_profile, demo_job, demo_application, facts):
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    selection, bullets, sentences = _fixture(facts)
    await _ready(session, demo_application["id"])
    return context, selection, bullets, sentences


async def _events(session, application_id, *types):
    rows = (
        (
            await session.execute(
                select(Event).where(Event.aggregate_id == application_id, Event.type.in_(types))
            )
        )
        .scalars()
        .all()
    )
    return rows


async def _counts(session, application_id):
    resumes = (
        await session.execute(
            select(func.count(Resume.id)).where(Resume.application_id == application_id)
        )
    ).scalar_one()
    claims = (
        await session.execute(
            select(func.count(ResumeClaim.id))
            .join(Resume)
            .where(Resume.application_id == application_id)
        )
    ).scalar_one()
    history = (
        await session.execute(
            select(func.count(ApplicationStatusHistory.id)).where(
                ApplicationStatusHistory.application_id == application_id
            )
        )
    ).scalar_one()
    events = (
        await session.execute(
            select(func.count(Event.id)).where(
                Event.aggregate_id == application_id, Event.type.in_(B6_EVENTS)
            )
        )
    ).scalar_one()
    return resumes, claims, history, events


# --- success path -----------------------------------------------------------


async def test_supported_resume_persists_completely(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context)

    result = await persist_resume(
        session,
        application_id=app_id,
        verified=verified,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
        agent_run_id=None,
        model="claude-sonnet-5",
        prompt_version="b2/b3",
        cost_usd=Decimal("0.004"),
    )
    await session.flush()

    assert result.version == 1
    assert result.verification_status == "passed"
    assert result.claim_count == 3 and result.verified_count == 3

    resume = (
        await session.execute(select(Resume).where(Resume.id == result.resume_id))
    ).scalar_one()
    assert resume.kind == "tailored"
    assert resume.verification_status == "passed"
    assert resume.model == "claude-sonnet-5"
    assert resume.summary_text and "Cloud engineer" in resume.summary_text

    claims = (
        (
            await session.execute(
                select(ResumeClaim)
                .where(ResumeClaim.resume_id == result.resume_id)
                .order_by(ResumeClaim.path)
            )
        )
        .scalars()
        .all()
    )
    assert len(claims) == 3  # one bullet + two sentences
    bullet_claim = next(c for c in claims if c.section == "experience")
    assert bullet_claim.path == "/experience/0"
    assert bullet_claim.claim_text == "Delivered AWS infrastructure work."
    assert [str(f) for f in bullet_claim.fact_ids] == [str(facts[0].id)]
    assert bullet_claim.verdict is ClaimVerdict.SUPPORTED
    assert bullet_claim.verdict_reason == "Matches the cited fact."
    assert bullet_claim.verified_at is not None
    assert {c.path for c in claims if c.section == "summary"} == {
        "/summary/0",
        "/summary/1",
    }


async def test_success_transitions_and_emits(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    result = await persist_resume(
        session,
        application_id=app_id,
        verified=_verified(bullets, sentences, selection, context),
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )
    await session.flush()

    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    assert application.status is ApplicationStatus.RESUME_READY

    # The transition is identified by what it *is*, not by where it landed in
    # the result set. `created_at` defaults to `now()`, which in PostgreSQL is
    # transaction time - identical for every row written inside one
    # transaction - so `ORDER BY created_at` leaves these two rows tied and
    # their relative order unspecified. `history[-1]` was therefore a coin
    # toss between `created -> resume_generating` and the one under test.
    #
    # Chronological order comes from `id` instead. These are UUIDv7 values
    # generated in `packages.shared.ids`, whose counter advances even within a
    # millisecond and never emits a lower timestamp, so the primary key is
    # strictly monotonic per process - a real sequencing column, unlike a
    # transaction-wide timestamp.
    #
    # Asserting the whole sequence is also stricter than the old positional
    # check: it pins that the earlier row still exists, that B6 added exactly
    # one row, and that neither is duplicated.
    history = (
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
    assert [(row.from_status, row.to_status) for row in sorted(history, key=lambda r: r.id)] == [
        (ApplicationStatus.CREATED, ApplicationStatus.RESUME_GENERATING),
        (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.RESUME_READY),
    ]
    generating_to_ready = next(
        row
        for row in history
        if row.from_status is ApplicationStatus.RESUME_GENERATING
        and row.to_status is ApplicationStatus.RESUME_READY
    )
    assert generating_to_ready.actor_type == "agent"

    generated = await _events(session, app_id, "resume.generated")
    render = await _events(session, app_id, "resume.render.requested")
    assert len(generated) == 1 and len(render) == 1
    assert generated[0].payload["version"] == 1
    assert generated[0].payload["claim_count"] == 3
    assert generated[0].payload["verified_count"] == 3
    assert generated[0].payload["resume_id"] == str(result.resume_id)
    assert render[0].payload["formats"] == ["pdf", "docx"]
    assert not await _events(session, app_id, "resume.verification.failed")


async def test_success_event_payloads_carry_no_jd_or_profile(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    await persist_resume(
        session,
        application_id=app_id,
        verified=_verified(bullets, sentences, selection, context),
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )
    await session.flush()
    blob = json.dumps([e.payload for e in await _events(session, app_id, *B6_EVENTS)])
    assert str(context.job.get("description_raw") or "\x00") not in blob
    assert "Persisted A4 rationale." not in blob
    assert str(demo_profile) not in blob


# --- failure path -----------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (ClaimVerdict.OVERREACH, "Inflated beyond the cited fact."),
        (ClaimVerdict.UNSUPPORTED, "Introduces tooling not in evidence."),
    ],
)
async def test_negative_verdict_still_persists_everything(
    session, demo_profile, demo_job, demo_application, inputs, verdict, reason
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context, verdicts={0: (verdict, reason)})

    result = await persist_resume(
        session,
        application_id=app_id,
        verified=verified,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )
    await session.flush()

    # The generated text is kept, not discarded - a human has to review it.
    assert result.verification_status == "failed"
    assert result.claim_count == 3 and result.verified_count == 2
    claims = (
        (
            await session.execute(
                select(ResumeClaim).where(ResumeClaim.resume_id == result.resume_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(claims) == 3
    failed = next(c for c in claims if c.verdict is verdict)
    assert failed.verdict_reason == reason
    assert failed.claim_text == "Delivered AWS infrastructure work."
    assert sum(1 for c in claims if c.verdict is ClaimVerdict.SUPPORTED) == 2

    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    assert application.status is ApplicationStatus.VERIFICATION_FAILED

    failed_events = await _events(session, app_id, "resume.verification.failed")
    assert len(failed_events) == 1
    payload = failed_events[0].payload
    assert payload["resume_id"] == str(result.resume_id)
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["verdict"] == str(verdict)
    assert payload["failures"][0]["reason"] == reason
    assert payload["failures"][0]["path"] == "/experience/0"

    # No rendering of an unverified resume, and no automatic redraft.
    assert not await _events(session, app_id, "resume.render.requested")
    assert not await _events(session, app_id, "resume.generated")


# --- gate rejections --------------------------------------------------------


async def test_wrong_starting_state_rejected(
    session, demo_profile, demo_job, demo_application, inputs
):
    """B6 does not move `created` itself; it refuses to run."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    selection, bullets, sentences = _fixture(facts)
    verified = _verified(bullets, sentences, selection, context)

    with pytest.raises(WrongApplicationStateError):
        await persist_resume(
            session,
            application_id=app_id,
            verified=verified,
            bullets=bullets,
            sentences=sentences,
            context=context,
            selection=selection,
        )
    assert await _counts(session, app_id) == (0, 0, 0, 0)


async def test_incomplete_coverage_rejected(
    session, demo_profile, demo_job, demo_application, inputs
):
    from packages.agents.resume_optimizer.handoff import HandoffError

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    partial = _verified(bullets, sentences, selection, context)[:-1]

    with pytest.raises(HandoffError):
        await persist_resume(
            session,
            application_id=app_id,
            verified=partial,
            bullets=bullets,
            sentences=sentences,
            context=context,
            selection=selection,
        )
    resumes, claims, _history, events = await _counts(session, app_id)
    assert (resumes, claims, events) == (0, 0, 0)


async def test_stale_handoff_rejected(session, demo_profile, demo_job, demo_application, inputs):
    from dataclasses import replace

    from packages.agents.resume_optimizer.handoff import HandoffError

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = list(_verified(bullets, sentences, selection, context))
    verified[0] = replace(verified[0], input_digest="0" * 64)

    with pytest.raises(HandoffError):
        await persist_resume(
            session,
            application_id=app_id,
            verified=tuple(verified),
            bullets=bullets,
            sentences=sentences,
            context=context,
            selection=selection,
        )
    resumes, claims, _history, events = await _counts(session, app_id)
    assert (resumes, claims, events) == (0, 0, 0)


async def test_duplicate_handoff_rejected(
    session, demo_profile, demo_job, demo_application, inputs
):
    from packages.agents.resume_optimizer.handoff import HandoffError

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context)

    with pytest.raises(HandoffError):
        await persist_resume(
            session,
            application_id=app_id,
            verified=(*verified, verified[0]),
            bullets=bullets,
            sentences=sentences,
            context=context,
            selection=selection,
        )
    resumes, claims, _history, events = await _counts(session, app_id)
    assert (resumes, claims, events) == (0, 0, 0)


# --- idempotency ------------------------------------------------------------


async def test_exact_retry_is_idempotent(session, demo_profile, demo_job, demo_application, inputs):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context)
    trigger = uuid7()
    kwargs = {
        "application_id": app_id,
        "verified": verified,
        "bullets": bullets,
        "sentences": sentences,
        "context": context,
        "selection": selection,
    }

    first = await persist_resume_once(session, triggering_event_id=trigger, **kwargs)
    await session.flush()
    assert first is not None and first.version == 1
    after_first = await _counts(session, app_id)

    # Same delivery again: claimed already, so no work happens at all.
    second = await persist_resume_once(session, triggering_event_id=trigger, **kwargs)
    await session.flush()
    assert second is None
    assert await _counts(session, app_id) == after_first
    assert after_first[0] == 1  # one resume
    assert after_first[1] == 3  # three claims
    assert after_first[3] == 2  # generated + render.requested


async def test_new_attempt_creates_next_version_leaving_the_old_untouched(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context)
    kwargs = {
        "application_id": app_id,
        "verified": verified,
        "bullets": bullets,
        "sentences": sentences,
        "context": context,
        "selection": selection,
    }

    first = await persist_resume_once(session, triggering_event_id=uuid7(), **kwargs)
    await session.flush()
    original = (
        await session.execute(select(Resume).where(Resume.id == first.resume_id))
    ).scalar_one()
    original_content = json.dumps(original.content, sort_keys=True)

    # A genuinely new attempt arrives as a different triggering event. The
    # application is back in resume_generating for the regeneration.
    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    application.status = ApplicationStatus.RESUME_GENERATING  # test-only reset
    await session.flush()

    second = await persist_resume_once(session, triggering_event_id=uuid7(), **kwargs)
    await session.flush()

    assert second is not None and second.version == 2
    assert second.resume_id != first.resume_id
    refreshed = (
        await session.execute(select(Resume).where(Resume.id == first.resume_id))
    ).scalar_one()
    assert refreshed.version == 1
    assert json.dumps(refreshed.content, sort_keys=True) == original_content
    resumes, claims, _history, _events = await _counts(session, app_id)
    assert resumes == 2 and claims == 6


# --- transactionality -------------------------------------------------------


async def test_rollback_removes_every_b6_write_together(
    session, demo_profile, demo_job, demo_application, inputs
):
    """A failure after the claims are inserted must undo all five writes.

    The savepoint is rolled back through its own handle: `session.rollback()`
    would discard the application fixture row this test depends on.
    """
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    context, selection, bullets, sentences = await _setup(
        session, demo_profile, demo_job, demo_application, facts
    )
    verified = _verified(bullets, sentences, selection, context)
    before = await _counts(session, app_id)

    savepoint = await session.begin_nested()
    await persist_resume(
        session,
        application_id=app_id,
        verified=verified,
        bullets=bullets,
        sentences=sentences,
        context=context,
        selection=selection,
    )
    await session.flush()
    during = await _counts(session, app_id)
    assert during[0] == 1 and during[1] == 3  # rows really were written
    await savepoint.rollback()

    session.expire_all()  # read the database, not the identity map
    assert await _counts(session, app_id) == before
    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    assert application.status is ApplicationStatus.RESUME_GENERATING
