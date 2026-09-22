"""B0 database loading, isolation, deterministic selection, and no writes."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import event

from packages.agents.resume_optimizer.context import (
    BaseResumeNotFoundError,
    EvaluationNotFoundError,
    IncompleteEvaluationError,
    JobNotFoundError,
    MalformedMatchMatrixError,
    load_tailoring_context,
)
from packages.agents.resume_optimizer.evidence import build_match_matrix_for_profile
from packages.agents.resume_optimizer.requirements import Necessity, Requirement, RequirementKind
from packages.core.models import JobEvaluation, ProfileFact, Resume
from packages.shared.clock import utc_now
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration


@pytest.fixture
async def inputs(session, demo_profile, demo_job, demo_user, demo_experience):
    facts = []
    for status, deleted in [
        ("verified", False),
        ("draft", False),
        ("retired", False),
        ("verified", True),
    ]:
        fact = ProfileFact(
            id=uuid7(),
            profile_id=demo_profile,
            fact_type="experience",
            text=f"AWS {status} {deleted}",
            structured={"nested": {"years": 3}},
            source_table="experiences",
            source_id=demo_experience,
            status=status,
            verified_by=demo_user,
            verified_at=utc_now(),
            deleted_at=utc_now() if deleted else None,
        )
        session.add(fact)
        facts.append(fact)
    base = Resume(
        id=uuid7(),
        profile_id=demo_profile,
        kind="base",
        version=2,
        content={"sections": [{"name": "Experience"}]},
    )
    session.add(base)
    await session.flush()
    matrix = await build_match_matrix_for_profile(
        session,
        demo_profile,
        [
            Requirement(text="AWS", necessity=Necessity.REQUIRED, kind=RequirementKind.SKILL),
        ],
    )
    evaluation = JobEvaluation(
        id=uuid7(),
        job_id=demo_job,
        profile_version=1,
        score=Decimal("71.23"),
        recommendation="moderate",
        match_matrix=matrix.as_dict(),
        score_breakdown={"persisted": 71.23},
        rationale="Persisted A4 rationale.",
        gaps=[],
    )
    session.add(evaluation)
    await session.flush()
    return base, evaluation, facts


async def test_load_is_detached_read_only_and_deterministic(
    session, demo_profile, demo_job, inputs
):
    base, evaluation, facts = inputs
    # A newer insertion with a lower version must not displace the base.
    session.add(Resume(id=uuid7(), profile_id=demo_profile, kind="base", version=1, content={}))
    await session.flush()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", record)
    try:
        first = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
        second = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", record)
    assert first == second
    assert all(stmt.lstrip().upper().startswith("SELECT") for stmt in statements)
    assert first.base_resume_id == base.id
    assert [fact.id for fact in first.facts] == [facts[0].id]
    assert first.evaluation_id == evaluation.id
    assert first.score == Decimal("71.23")
    assert first.rationale == evaluation.rationale
    assert first.score_breakdown == evaluation.score_breakdown
    before = deepcopy(evaluation.match_matrix)
    first.match_matrix["requirements"].clear()
    first.base_resume_structure["sections"].clear()
    first.facts[0].structured["nested"]["years"] = 99
    assert evaluation.match_matrix == before
    assert base.content["sections"]
    assert facts[0].structured["nested"]["years"] == 3
    assert not session.dirty and not session.new


@pytest.mark.parametrize(
    "case,error",
    [
        ("job", JobNotFoundError),
        ("evaluation", EvaluationNotFoundError),
        ("base", BaseResumeNotFoundError),
    ],
)
async def test_missing_inputs(session, demo_profile, demo_job, case, error):
    if case == "base":
        session.add(
            JobEvaluation(
                id=uuid7(),
                job_id=demo_job,
                profile_version=1,
                score=0,
                recommendation="no",
                match_matrix={"requirements": []},
                rationale="Gap",
            )
        )
        await session.flush()
    with pytest.raises(error):
        await load_tailoring_context(
            session, profile_id=demo_profile, job_id=uuid7() if case == "job" else demo_job
        )


@pytest.mark.parametrize(
    "matrix,rationale,error",
    [
        ({}, "A4", MalformedMatchMatrixError),
        ({"requirements": []}, None, IncompleteEvaluationError),
        ({"requirements": []}, "  ", IncompleteEvaluationError),
    ],
)
async def test_latest_evaluation_fails_without_fallback(
    session, demo_profile, demo_job, inputs, matrix, rationale, error
):
    _, old, _ = inputs
    session.add(
        JobEvaluation(
            id=uuid7(),
            job_id=demo_job,
            profile_version=2,
            created_at=old.created_at,
            score=12,
            recommendation="no",
            match_matrix=matrix,
            rationale=rationale,
        )
    )
    await session.flush()
    with pytest.raises(error):
        await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)


async def test_retired_citation_fails_closed(session, demo_profile, demo_job, inputs):
    _, _, facts = inputs
    facts[0].status = "retired"
    await session.flush()
    with pytest.raises(MalformedMatchMatrixError):
        await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)


async def test_profile_cannot_load_another_profiles_base(session, demo_job, inputs):
    with pytest.raises(BaseResumeNotFoundError):
        await load_tailoring_context(session, profile_id=uuid7(), job_id=demo_job)


async def test_load_does_not_autoflush_pending_changes(session, demo_profile, demo_job, inputs):
    pending = Resume(id=uuid7(), profile_id=demo_profile, kind="base", version=99, content={})
    session.add(pending)
    result = await load_tailoring_context(session, profile_id=demo_profile, job_id=demo_job)
    assert result.base_resume_id == inputs[0].id
    assert pending in session.new
