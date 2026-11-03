"""A4 through the real A5 persistence path."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from packages.agents.resume_optimizer.evidence import build_match_matrix_for_profile
from packages.agents.resume_optimizer.persistence import persist_evaluation
from packages.agents.resume_optimizer.prefilter import (
    compute_score,
)
from packages.agents.resume_optimizer.requirements import (
    Necessity,
    Requirement,
    RequirementKind,
)
from packages.shared.clock import utc_now
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration


def req(body: str, **kwargs) -> Requirement:
    return Requirement(
        text=body,
        necessity=kwargs.pop("necessity", Necessity.REQUIRED),
        kind=kwargs.pop("kind", RequirementKind.SKILL),
        **kwargs,
    )


@pytest.fixture
async def normalized_job(session, demo_job):  # type: ignore[no-untyped-def]
    await session.execute(
        text("UPDATE jobs SET status = 'normalized' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()
    return demo_job


@pytest.fixture
async def verified_fact(session, demo_profile, demo_experience, demo_user):  # type: ignore[no-untyped-def]
    fact_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, structured, "
            "source_table, source_id, status, verified_by, verified_at, "
            "created_at, updated_at) VALUES (:id, :pid, 'experience', "
            "'Managed AWS EC2, IAM and VPC infrastructure', '{}'::jsonb, "
            "'experiences', :sid, 'verified', :vb, :va, now(), now())"
        ),
        {
            "id": fact_id,
            "pid": demo_profile,
            "sid": demo_experience,
            "vb": demo_user,
            "va": utc_now(),
        },
    )
    await session.flush()
    return fact_id


async def _evaluate(session, profile_id, job_id, requirements):  # type: ignore[no-untyped-def]
    matrix = await build_match_matrix_for_profile(session, profile_id, requirements)
    breakdown = compute_score(matrix.to_requirement_matches())
    result = await persist_evaluation(
        session,
        job_id=job_id,
        profile_version=1,
        breakdown=breakdown,
        match_matrix=matrix.as_dict(),
    )
    return result, matrix


async def test_rationale_is_persisted(session, demo_profile, normalized_job, verified_fact) -> None:
    result, _ = await _evaluate(
        session, demo_profile, normalized_job, [req("AWS"), req("Kubernetes")]
    )
    stored = (
        await session.execute(
            text("SELECT rationale FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).scalar_one()
    assert stored
    assert "match" in stored.lower()


async def test_rationale_names_real_requirements_only(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    result, _ = await _evaluate(
        session, demo_profile, normalized_job, [req("AWS"), req("Kubernetes")]
    )
    stored = (
        await session.execute(
            text("SELECT rationale FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).scalar_one()
    assert "AWS" in stored
    assert "Kubernetes" in stored
    for invented in ("Terraform", "Docker", "PhD"):
        assert invented not in stored


@pytest.mark.safety
async def test_rationale_agrees_with_the_score(session, demo_profile, normalized_job) -> None:
    """No verified facts at all: the prose must not claim coverage."""
    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    stored = (
        await session.execute(
            text("SELECT rationale, score FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).one()
    assert "Fully covered" not in stored.rationale
    assert "No verified fact supported any requirement" in stored.rationale


async def test_explicit_rationale_is_respected(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    """Existing callers that supply their own text keep it."""
    matrix = await build_match_matrix_for_profile(session, demo_profile, [req("AWS")])
    result = await persist_evaluation(
        session,
        job_id=normalized_job,
        profile_version=1,
        breakdown=compute_score(matrix.to_requirement_matches()),
        match_matrix=matrix.as_dict(),
        rationale="Supplied by the caller.",
    )
    stored = (
        await session.execute(
            text("SELECT rationale FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).scalar_one()
    assert stored == "Supplied by the caller."


async def test_rationale_is_reproducible_from_the_stored_row(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    """The structured form is a pure function of columns already stored, so
    it never needed its own column."""
    from packages.agents.resume_optimizer.rationale import build_rationale

    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    row = (
        await session.execute(
            text(
                "SELECT rationale, match_matrix, score, recommendation, gaps "
                "FROM job_evaluations WHERE id = :id"
            ),
            {"id": result.evaluation_id},
        )
    ).one()
    rebuilt = build_rationale(
        row.match_matrix,
        score=Decimal(str(row.score)),
        recommendation=str(row.recommendation),
        gaps=list(row.gaps),
    )
    assert rebuilt.summary == row.rationale


async def test_model_and_prompt_version_stay_null(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    """Nothing on this path uses a model; recording one would misdescribe it."""
    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    row = (
        await session.execute(
            text("SELECT model, prompt_version FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).one()
    assert row.model is None
    assert row.prompt_version is None


async def test_evaluation_rows_remain_append_only(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        await session.execute(
            text("UPDATE job_evaluations SET rationale = 'rewritten' WHERE id = :id"),
            {"id": result.evaluation_id},
        )
        await session.flush()
    assert "append-only" in str(exc.value)
    await session.rollback()


async def test_endpoint_returns_the_rationale(
    db_app_client, session, demo_profile, normalized_job, verified_fact
) -> None:
    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    await session.commit()
    response = await db_app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")
    assert response.status_code == 200
    body = response.json()
    assert body["rationale"]
    assert body["id"] == str(result.evaluation_id)


async def test_status_transition_unchanged_by_a4(
    session, demo_profile, normalized_job, verified_fact
) -> None:
    result, _ = await _evaluate(session, demo_profile, normalized_job, [req("AWS")])
    assert result.from_status.value == "normalized"
    assert result.to_status.value in ("evaluated", "shortlisted", "discarded")
    assert "job.evaluated" in result.events_emitted
