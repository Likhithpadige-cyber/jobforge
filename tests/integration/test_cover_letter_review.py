"""M10 Phase 1 - grounded cover-letter generation and review/rework, against
a real database.

Mirrors `test_resume_review.py`'s reasoning: the interesting parts - the
`uq_one_pending_approval` partial unique index, the append-only versioning,
the `subject_snapshot` binding across versions - live in PostgreSQL, and a
mocked session would let every one of them pass untested.

No real model is ever called: `CoverLetterClient` runs in `LLMMode.REPLAY`
against cassette files this module writes itself, exactly like every other
integration suite that exercises an LLM-shaped pipeline.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from packages.agents.cover_letter.drafting import prepare_request as draft_request
from packages.agents.llm.runtime import Response, canonical
from packages.agents.resume_optimizer import verification as b5
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.core.models import (
    Application,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    CoverLetter,
    CoverLetterClaim,
    Event,
    User,
)
from packages.core.services import applications as applications_service
from packages.core.services import cover_letters as service
from packages.core.services import resume_review
from packages.shared.config import Settings
from tests.integration import test_resume_read_api as c1
from tests.integration import test_tailoring_context

inputs = test_tailoring_context.inputs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _actor(session, demo_application) -> User:
    return (
        await session.execute(
            select(User).where(User.id == demo_application["user_id"])
        )
    ).scalar_one()


async def _ready_for_review(session, demo_profile, demo_job, demo_application, facts):
    """An application in `awaiting_approval` with a real tailored resume -
    same as `test_resume_review._ready` plus opening review, which now also
    requests cover-letter generation (it does not draft one; that is what
    `_generate_cover_letter` below simulates the worker doing)."""
    await c1._generate(session, demo_profile, demo_job, demo_application["id"], facts)
    await applications_service.prepare_application(
        session, application_id=demo_application["id"], questions=[]
    )
    await session.flush()
    actor = await _actor(session, demo_application)
    await resume_review.enter_review(
        session, application_id=demo_application["id"], actor=actor
    )
    await session.flush()


def _paragraphs(fact_id: str, *, text_suffix: str = "") -> dict:
    return {
        "paragraphs": [
            {
                "text": f"I bring 3 years of verified AWS experience.{text_suffix}",
                "fact_ids": [fact_id],
                "numerals_used": ["3"],
            },
            {
                "text": "This experience is documented and verified on file.",
                "fact_ids": [fact_id],
                "numerals_used": [],
            },
            {
                "text": "I am confident this background suits the role.",
                "fact_ids": [fact_id],
                "numerals_used": [],
            },
        ]
    }


async def _write_cassettes(
    session,
    *,
    client,
    application_id,
    demo_profile,
    demo_job,
    tmp_path,
    fact_id: str,
    note: str | None = None,
    text_suffix: str = "",
    verdicts: list[str] | None = None,
):
    """Write one drafting cassette entry and one B5 cassette entry per
    paragraph, so `generate_cover_letter` runs its real pipeline end to end
    without ever touching the network."""
    application = (
        await session.execute(select(Application).where(Application.id == application_id))
    ).scalar_one()
    context = await load_tailoring_context(
        session, profile_id=application.profile_id, job_id=application.job_id
    )
    output = _paragraphs(fact_id, text_suffix=text_suffix)

    draft_req = draft_request(context, model=client.model, note=note)
    client.cassette_dir.mkdir(parents=True, exist_ok=True)
    (client.cassette_dir / f"{draft_req.digest}.json").write_text(
        Response(
            body=canonical(output),
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal("0.001"),
            stop_reason="tool_use",
        ).model_dump_json()
    )

    by_id = {str(f.id): f for f in context.facts}
    verdicts = verdicts or ["supported"] * len(output["paragraphs"])
    for paragraph, verdict in zip(output["paragraphs"], verdicts, strict=True):
        cited = [by_id[fid] for fid in paragraph["fact_ids"]]
        verify_req = b5.prepare_request(paragraph["text"], cited, client.model)
        (client.cassette_dir / f"{verify_req.digest}.json").write_text(
            Response(
                body=canonical(
                    {"verdict": verdict, "explanation": "Matches the cited fact."}
                ),
                input_tokens=40,
                output_tokens=20,
                cost_usd=Decimal("0.0005"),
                stop_reason="tool_use",
            ).model_dump_json()
        )
    return context


async def _generate_cover_letter(
    session, *, application_id, demo_profile, demo_job, tmp_path, fact_id,
    event_id, note=None, text_suffix="", verdicts=None,
):
    """Simulates the worker consuming one `cover_letter.generation.requested`
    event - the real `generate_cover_letter` pipeline, replayed offline."""
    client = service.CoverLetterClient(Settings(), cassette_dir=tmp_path)
    await _write_cassettes(
        session, client=client, application_id=application_id,
        demo_profile=demo_profile, demo_job=demo_job, tmp_path=tmp_path,
        fact_id=fact_id, note=note, text_suffix=text_suffix, verdicts=verdicts,
    )
    return await service.generate_cover_letter(
        session, application_id=application_id, event_id=event_id,
        client=client, note=note,
    )


async def _cover_letters(session, application_id) -> list[CoverLetter]:
    return list(
        (
            await session.execute(
                select(CoverLetter)
                .where(CoverLetter.application_id == application_id)
                .order_by(CoverLetter.version)
            )
        )
        .scalars()
        .all()
    )


async def _approvals(session, application_id) -> list[Approval]:
    return list(
        (
            await session.execute(
                select(Approval)
                .where(
                    Approval.application_id == application_id,
                    Approval.kind == ApprovalKind.COVER_LETTER,
                )
                .order_by(Approval.requested_at, Approval.id)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def test_entering_review_requests_cover_letter_generation(
    session, demo_profile, demo_job, demo_application, inputs
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)

    requested = list(
        (
            await session.execute(
                select(Event).where(
                    Event.type == "cover_letter.generation.requested",
                    Event.correlation_id == app_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(requested) == 1
    assert requested[0].payload["application_id"] == str(app_id)


async def test_a_grounded_cover_letter_passes_and_opens_review(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)

    cover_letter = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )

    assert cover_letter.verification_status == "passed"
    assert cover_letter.version == 1
    claims = list(
        (
            await session.execute(
                select(CoverLetterClaim).where(
                    CoverLetterClaim.cover_letter_id == cover_letter.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(claims) == 3
    assert all(str(c.fact_ids[0]) == str(facts[0].id) for c in claims)

    approvals = await _approvals(session, app_id)
    assert len(approvals) == 1
    assert approvals[0].status is ApprovalStatus.PENDING
    assert approvals[0].subject_snapshot["cover_letter_id"] == str(cover_letter.id)
    assert approvals[0].subject_snapshot["cover_letter_version"] == 1


async def test_unsupported_claim_fails_verification_and_opens_no_review(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    """The Claim Verifier's own judgement, not the grounding/numeral guard:
    a paragraph that cites a real fact but that B5 judges unsupported must
    still fail the whole version closed."""
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)

    cover_letter = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
        verdicts=["unsupported", "supported", "supported"],
    )

    assert cover_letter.verification_status == "failed"
    assert await _approvals(session, app_id) == []


async def test_generation_event_replay_produces_no_duplicate_version(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)

    first = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )
    second = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )

    assert second.id == first.id
    assert len(await _cover_letters(session, app_id)) == 1
    assert len(await _approvals(session, app_id)) == 1


# ---------------------------------------------------------------------------
# Review and rework
# ---------------------------------------------------------------------------


async def test_v1_request_changes_v2_prior_version_preserved(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)

    v1 = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )
    v1_content_before = dict(v1.content)

    state = await service.request_changes_cover_letter(
        session, application_id=app_id, actor=actor, note="Mention the years explicitly."
    )
    assert state.review_status == "changes_requested"
    assert (await service.cover_letter_review_state(session, app_id)).can_request_changes is False

    v2 = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=101),
        note="Mention the years explicitly.", text_suffix=" Precisely three years.",
    )

    # Prior version preserved, unmutated.
    v1_after = (
        await session.execute(select(CoverLetter).where(CoverLetter.id == v1.id))
    ).scalar_one()
    assert v1_after.content == v1_content_before
    assert v1_after.version == 1
    assert v2.version == 2
    assert v2.id != v1.id

    letters = await _cover_letters(session, app_id)
    assert [c.version for c in letters] == [1, 2]

    approvals = await _approvals(session, app_id)
    assert len(approvals) == 2
    v1_approval = next(a for a in approvals if a.subject_snapshot["cover_letter_version"] == 1)
    v2_approval = next(a for a in approvals if a.subject_snapshot["cover_letter_version"] == 2)
    assert v1_approval.status is ApprovalStatus.CHANGES_REQUESTED
    assert v2_approval.status is ApprovalStatus.PENDING

    # Stale v1 approval cannot authorize v2.
    assert not service.cover_letter_snapshot_matches(v1_approval.subject_snapshot, v2)
    assert service.cover_letter_snapshot_matches(v2_approval.subject_snapshot, v2)
    current = await service.cover_letter_approval(session, app_id)
    assert current.id == v2_approval.id


async def test_approve_v2_after_rework(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)

    await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )
    await service.request_changes_cover_letter(
        session, application_id=app_id, actor=actor, note="Tighten the opening."
    )
    v2 = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=101),
        note="Tighten the opening.",
    )

    state = await service.approve_cover_letter(session, application_id=app_id, actor=actor)

    assert state.review_status == "approved"
    assert state.cover_letter_id == v2.id
    # `applications.status` is untouched by cover-letter approval/rework.
    application = (
        await session.execute(select(Application).where(Application.id == app_id))
    ).scalar_one()
    assert application.status is ApplicationStatus.AWAITING_APPROVAL


async def test_approve_is_idempotent(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )

    first = await service.approve_cover_letter(session, application_id=app_id, actor=actor)
    approved_events_before = len(
        list(
            (
                await session.execute(
                    select(Event).where(
                        Event.type == "approval.granted", Event.correlation_id == app_id
                    )
                )
            )
            .scalars()
            .all()
        )
    )
    second = await service.approve_cover_letter(session, application_id=app_id, actor=actor)

    assert second.approval_id == first.approval_id
    approved_events_after = len(
        list(
            (
                await session.execute(
                    select(Event).where(
                        Event.type == "approval.granted", Event.correlation_id == app_id
                    )
                )
            )
            .scalars()
            .all()
        )
    )
    assert approved_events_after == approved_events_before


async def test_request_changes_without_a_note_is_refused(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)
    await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )

    with pytest.raises(service.MissingNoteError):
        await service.request_changes_cover_letter(
            session, application_id=app_id, actor=actor, note="   "
        )


async def test_rework_limit_is_enforced(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)

    for i in range(service.MAX_REWORK_CYCLES):
        await _generate_cover_letter(
            session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
            tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100 + i),
            note=(f"round {i}" if i else None), text_suffix=" Revised again.",
        )
        await service.request_changes_cover_letter(
            session, application_id=app_id, actor=actor, note=f"round {i}"
        )

    with pytest.raises(service.CoverLetterReworkLimitReachedError):
        await service.request_changes_cover_letter(
            session, application_id=app_id, actor=actor, note="one too many"
        )


async def test_current_version_resolution(
    session, demo_profile, demo_job, demo_application, inputs, tmp_path
):
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await _ready_for_review(session, demo_profile, demo_job, demo_application, facts)
    actor = await _actor(session, demo_application)

    assert await service.latest_cover_letter(session, app_id) is None

    v1 = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=100),
    )
    assert (await service.latest_cover_letter(session, app_id)).id == v1.id

    await service.request_changes_cover_letter(
        session, application_id=app_id, actor=actor, note="Adjust tone."
    )
    v2 = await _generate_cover_letter(
        session, application_id=app_id, demo_profile=demo_profile, demo_job=demo_job,
        tmp_path=tmp_path, fact_id=str(facts[0].id), event_id=UUID(int=101),
        note="Adjust tone.",
    )
    latest = await service.latest_cover_letter(session, app_id)
    assert latest.id == v2.id
    assert latest.version == 2
