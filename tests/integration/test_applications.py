"""M9 - application preparation, against a real database.

Synthetic content throughout. Nothing in this file resembles a real resume,
a real candidate or a real employer.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select, text

from packages.core.models import (
    Application,
    ApplicationAnswer,
    ApplicationQuestion,
    ApplicationStatus,
    ApprovalKind,
    ApprovalStatus,
    Event,
    User,
)
from packages.core.models.enums import AnswerSource, QuestionClass
from packages.core.services import applications as service
from packages.core.services.applications import (
    ApplicationStateError,
    IncompleteAnswersError,
    RawQuestion,
    approve_answers,
    edit_answer,
    prepare_application,
    request_application_questions,
    request_changes_answers,
)
from packages.core.state_machines.application import transition_application
from packages.shared.ids import uuid7
from tests.integration import test_resume_read_api, test_tailoring_context
from tests.integration import test_resume_review as c2

inputs = test_tailoring_context.inputs

pytestmark = [pytest.mark.integration, pytest.mark.safety]


# --------------------------------------------------------------------- helpers


async def _actor(session, demo_application) -> User:
    return await c2._actor(session, demo_application)


async def _ready(session, demo_profile, demo_job, demo_application, facts):
    """An application at `resume_ready`, without M9's own zero-question
    fast-forward - these tests exercise `prepare_application` themselves."""
    _base, _evaluation, real_facts = facts
    return await test_resume_read_api._generate(
        session, demo_profile, demo_job, demo_application["id"], real_facts
    )


async def _questions(session, application_id) -> list[ApplicationQuestion]:
    return await service.existing_questions(session, application_id)


async def _set_accepted_types(session, profile_id, types) -> None:
    await session.execute(
        text("UPDATE profiles SET accepted_employment_types = :types WHERE id = :id"),
        {"types": types, "id": profile_id},
    )


async def _set_job_employment_type(session, job_id, employment_type) -> None:
    await session.execute(
        text("UPDATE jobs SET employment_type = :t WHERE id = :id"),
        {"t": employment_type, "id": job_id},
    )


# ------------------------------------------------------------------ the pipeline


async def test_zero_questions_still_passes_through_questions_pending(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    created = await prepare_application(session, application_id=app_id, questions=[])

    assert created == []
    application = await session.get(Application, app_id)
    assert application.status is ApplicationStatus.QUESTIONS_PENDING
    extracted = (
        await session.scalars(
            select(Event).where(
                Event.type == "application.questions.extracted",
                Event.aggregate_id == app_id,
            )
        )
    ).all()
    assert len(extracted) == 1
    assert extracted[0].payload["total"] == 0


async def test_sensitive_question_is_never_auto_answered(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(
        session,
        application_id=app_id,
        questions=[
            RawQuestion(
                question_text="Are you legally authorized to work in the United States?",
                input_type="boolean",
            )
        ],
    )

    questions = await _questions(session, app_id)
    assert len(questions) == 1
    assert questions[0].question_class is QuestionClass.SENSITIVE
    answer = questions[0].answer
    assert answer is not None
    assert answer.source is AnswerSource.BLANK
    assert answer.value is None
    assert answer.requires_human is True
    assert answer.fact_ids == []


async def test_verified_fact_grounds_an_agent_answer(
    session, demo_profile, demo_job, demo_application, inputs, verified_fact
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(
        session,
        application_id=app_id,
        questions=[
            RawQuestion(
                question_text="Have you worked as a Demo Engineer at Example Cloud Corp?",
                input_type="boolean",
            )
        ],
    )

    questions = await _questions(session, app_id)
    answer = questions[0].answer
    assert answer.source is AnswerSource.AGENT
    assert answer.fact_ids == [verified_fact]
    assert answer.requires_human is False


async def test_a_foreign_profiles_fact_is_never_cited(
    session, demo_profile, demo_job, demo_application, inputs, demo_user
):
    """A verified fact belonging to a DIFFERENT profile must never ground an
    answer for this one - the exact cross-candidate leak this design forbids."""
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    other_profile_id, other_experience_id, foreign_fact_id = uuid7(), uuid7(), uuid7()
    other_user_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO users (id, email, role, created_at, updated_at) VALUES "
            "(:id, :email, 'owner', now(), now())"
        ),
        {"id": other_user_id, "email": f"other-{uuid4().hex[:8]}@example.invalid"},
    )
    await session.execute(
        text(
            "INSERT INTO profiles (id, user_id, full_name, created_at, updated_at) "
            "VALUES (:id, :user_id, 'Someone Else', now(), now())"
        ),
        {"id": other_profile_id, "user_id": other_user_id},
    )
    await session.execute(
        text(
            "INSERT INTO experiences (id, profile_id, company_name, title, "
            "start_date, created_at, updated_at) VALUES "
            "(:id, :profile_id, 'Other Corp', 'Other Engineer', DATE '2022-01-01', "
            "now(), now())"
        ),
        {"id": other_experience_id, "profile_id": other_profile_id},
    )
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, "
            "source_table, source_id, status, verified_by, verified_at, "
            "created_at, updated_at) VALUES (:id, :profile_id, 'experience', "
            "'Other Engineer at Other Corp', 'experiences', :source_id, "
            "'verified', :verified_by, now(), now(), now())"
        ),
        {
            "id": foreign_fact_id, "profile_id": other_profile_id,
            "source_id": other_experience_id, "verified_by": other_user_id,
        },
    )
    await session.flush()

    await prepare_application(
        session,
        application_id=app_id,
        questions=[
            RawQuestion(question_text="Have you worked as an Other Engineer at Other Corp?",
                       input_type="boolean")
        ],
    )
    questions = await _questions(session, app_id)
    answer = questions[0].answer
    assert foreign_fact_id not in answer.fact_ids
    assert answer.source is not AnswerSource.AGENT


async def test_an_unverified_fact_is_never_cited(
    session, demo_profile, demo_job, demo_application, inputs, draft_fact
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(
        session,
        application_id=app_id,
        questions=[
            RawQuestion(question_text="Do you have the demo claim skill?", input_type="boolean")
        ],
    )
    questions = await _questions(session, app_id)
    answer = questions[0].answer
    assert draft_fact not in answer.fact_ids
    assert answer.source is AnswerSource.BLANK
    assert answer.requires_human is True


async def test_a_deleted_fact_is_never_cited(
    session, demo_profile, demo_job, demo_application, inputs, verified_fact
):
    await session.execute(
        text("UPDATE profile_facts SET deleted_at = now() WHERE id = :id"),
        {"id": verified_fact},
    )
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(
        session,
        application_id=app_id,
        questions=[
            RawQuestion(question_text="Have you worked as a Demo Engineer at Example Cloud Corp?",
                       input_type="boolean")
        ],
    )
    questions = await _questions(session, app_id)
    answer = questions[0].answer
    assert verified_fact not in answer.fact_ids
    assert answer.source is AnswerSource.BLANK


# ------------------------------------------------------- employment-type gate


async def test_explicit_accepted_employment_type_advances(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _set_accepted_types(session, demo_profile, ["full_time"])
    await _set_job_employment_type(session, demo_job, "full_time")
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(session, application_id=app_id, questions=[])

    questions = await _questions(session, app_id)
    assert len(questions) == 1
    assert questions[0].question_class is QuestionClass.DERIVED
    assert questions[0].answer.value == "true"
    assert questions[0].answer.requires_human is False


async def test_explicit_rejected_employment_type_does_not_auto_advance(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _set_accepted_types(session, demo_profile, ["full_time"])
    await _set_job_employment_type(session, demo_job, "contract")
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(session, application_id=app_id, questions=[])

    questions = await _questions(session, app_id)
    assert questions[0].answer.value == "false"
    assert questions[0].answer.requires_human is True
    assert questions[0].question_class is QuestionClass.JUDGEMENT


async def test_missing_employment_type_is_unresolved(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _set_accepted_types(session, demo_profile, ["full_time"])
    # demo_job's employment_type is NULL by default (conftest never sets it).
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(session, application_id=app_id, questions=[])

    questions = await _questions(session, app_id)
    assert questions[0].answer.value is None
    assert questions[0].answer.requires_human is True
    assert questions[0].question_class is QuestionClass.UNCERTAIN


async def test_no_preference_set_skips_the_gate_entirely(
    session, demo_profile, demo_job, demo_application, inputs
):
    """NULL accepted_employment_types = no filtering at all - not hard-coded
    globally, opt-in per candidate."""
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]

    await prepare_application(session, application_id=app_id, questions=[])

    questions = await _questions(session, app_id)
    assert questions == []


# ------------------------------------------------------------------ idempotency


async def test_replaying_prepare_creates_no_duplicate_questions_or_answers(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    raw = [RawQuestion(question_text="What is your email address?", input_type="text")]

    first = await prepare_application(session, application_id=app_id, questions=raw)
    await session.flush()
    extracted_before = (
        await session.scalars(
            select(Event).where(
                Event.type == "application.questions.extracted", Event.aggregate_id == app_id
            )
        )
    ).all()

    second = await prepare_application(session, application_id=app_id, questions=raw)

    assert [q.id for q in first] == [q.id for q in second]
    rows = (
        await session.scalars(
            select(ApplicationQuestion).where(ApplicationQuestion.application_id == app_id)
        )
    ).all()
    assert len(rows) == 1
    answer_rows = (
        await session.scalars(
            select(ApplicationAnswer).where(ApplicationAnswer.application_id == app_id)
        )
    ).all()
    assert len(answer_rows) == 1
    extracted_after = (
        await session.scalars(
            select(Event).where(
                Event.type == "application.questions.extracted", Event.aggregate_id == app_id
            )
        )
    ).all()
    assert len(extracted_after) == len(extracted_before) == 1
    application = await session.get(Application, app_id)
    assert application.status is ApplicationStatus.QUESTIONS_PENDING


async def test_prepare_refuses_outside_resume_ready(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(session, application_id=app_id, questions=[])
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()

    # A replay after status has moved on must still short-circuit rather
    # than error, matching render_resume's replay discipline - including
    # (this is the case that used to fail) when the prior run produced zero
    # questions, so there is nothing to find by row count alone.
    result = await prepare_application(session, application_id=app_id, questions=[])
    assert result == []


async def test_reworked_resume_with_stale_prior_questions_is_refused_not_mixed(
    session, demo_profile, demo_job, demo_application, inputs
):
    """A second `resume_ready` episode with real rows already prepared for
    the first must fail loudly - `application_questions` has no per-round
    binding, so silently creating a second set would mix two rounds'
    answers with no way to tell them apart."""
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="What is your email address?", input_type="text")],
    )
    application = await session.get(Application, app_id)
    # Simulate a rework landing the application back at resume_ready without
    # clearing the (in this design, unversioned) prior round's rows.
    await session.execute(
        text("UPDATE applications SET status = 'resume_ready' WHERE id = :id"),
        {"id": app_id},
    )
    await session.refresh(application)

    with pytest.raises(ApplicationStateError, match="already has 1 prepared"):
        await prepare_application(
            session, application_id=app_id,
            questions=[RawQuestion(question_text="What is your phone number?", input_type="text")],
        )


async def test_request_application_questions_requires_owner(
    session, demo_profile, demo_job, demo_application, inputs
):
    from packages.shared.errors import ForbiddenError

    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    reviewer_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO users (id, email, role, created_at, updated_at) VALUES "
            "(:id, :email, 'reviewer', now(), now())"
        ),
        {"id": reviewer_id, "email": f"reviewer-{uuid4().hex[:8]}@example.invalid"},
    )
    await session.flush()
    reviewer = await session.get(User, reviewer_id)

    with pytest.raises(ForbiddenError):
        await request_application_questions(
            session, actor=reviewer, application_id=app_id, questions=[]
        )


async def test_request_application_questions_emits_the_owner_supplied_capture(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    owner = await _actor(session, demo_application)

    result = await request_application_questions(
        session,
        actor=owner,
        application_id=app_id,
        questions=[RawQuestion(question_text="What is your city?", input_type="text")],
    )
    await session.flush()

    event = await session.get(Event, result.event_id)
    assert event.type == "application.questions.requested"
    assert event.actor_type == "human"
    assert event.payload["questions"][0]["question_text"] == "What is your city?"


# ------------------------------------------------------ human editing / review


async def test_human_edit_preserves_the_agent_proposal(
    session, demo_profile, demo_job, demo_application, inputs, verified_fact
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="Have you worked as a Demo Engineer at "
                               "Example Cloud Corp?", input_type="boolean")],
    )
    questions = await _questions(session, app_id)
    answer_id = questions[0].answer.id
    original_value = questions[0].answer.value
    actor = await _actor(session, demo_application)

    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()

    edited = await edit_answer(
        session, application_id=app_id, answer_id=answer_id, actor=actor,
        value="A human-provided correction.",
    )
    assert edited.source is AnswerSource.HUMAN
    assert edited.answered_by == actor.id
    assert edited.edited_from_agent_value == original_value
    assert edited.requires_human is False


async def test_approve_refuses_while_a_required_answer_is_unresolved(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="What is your favorite color?", input_type="text")],
    )
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()
    actor = await _actor(session, demo_application)

    with pytest.raises(IncompleteAnswersError):
        await approve_answers(session, application_id=app_id, actor=actor)


async def test_approve_succeeds_once_every_required_answer_is_resolved(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="What is your favorite color?", input_type="text")],
    )
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()
    actor = await _actor(session, demo_application)
    questions = await _questions(session, app_id)
    await edit_answer(
        session, application_id=app_id, answer_id=questions[0].answer.id,
        actor=actor, value="Blue.",
    )
    await session.flush()

    approval = await approve_answers(session, application_id=app_id, actor=actor)

    assert approval.kind is ApprovalKind.ANSWERS
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.decided_by == actor.id
    assert approval.subject_snapshot["questions"][0]["value"] == "Blue."
    granted = (
        await session.scalars(
            select(Event).where(Event.type == "approval.granted", Event.aggregate_id == approval.id)
        )
    ).all()
    assert len(granted) == 1


async def test_approve_revalidates_cited_facts_immediately_before_the_snapshot(
    session, demo_profile, demo_job, demo_application, inputs, verified_fact
):
    """A fact retired between drafting and approving must refuse the decision -
    the same immediately-before-persist discipline M8's render service uses."""
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="Have you worked as a Demo Engineer at "
                               "Example Cloud Corp?", input_type="boolean")],
    )
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()
    actor = await _actor(session, demo_application)

    await session.execute(
        text("UPDATE profile_facts SET status = 'retired' WHERE id = :id"),
        {"id": verified_fact},
    )
    await session.flush()

    with pytest.raises(ApplicationStateError):
        await approve_answers(session, application_id=app_id, actor=actor)


async def test_request_changes_does_not_move_application_status(
    session, demo_profile, demo_job, demo_application, inputs
):
    await _ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session, application_id=app_id,
        questions=[RawQuestion(question_text="What is your favorite color?", input_type="text")],
    )
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.flush()
    actor = await _actor(session, demo_application)
    questions = await _questions(session, app_id)
    await edit_answer(
        session, application_id=app_id, answer_id=questions[0].answer.id,
        actor=actor, value="Green.",
    )
    await session.flush()

    approval = await request_changes_answers(
        session, application_id=app_id, actor=actor, note="Please double-check this."
    )

    assert approval.status is ApprovalStatus.CHANGES_REQUESTED
    application = await session.get(Application, app_id)
    assert application.status is ApplicationStatus.AWAITING_APPROVAL


# --------------------------------------------------------------- the endpoints


async def test_anonymous_callers_cannot_prepare_or_read(app_client) -> None:
    listing = await app_client.get(f"/api/v1/applications/{uuid7()}/questions")
    prep = await app_client.post(f"/api/v1/applications/{uuid7()}/prepare", json={})
    assert listing.status_code == 401
    assert prep.status_code == 401


async def test_a_missing_application_answers_404_on_questions(db_app_client) -> None:
    response = await db_app_client.get(f"/api/v1/applications/{uuid7()}/questions")
    assert response.status_code == 404
    assert response.json()["detail"] == "application not found"
