"""GET /api/v1/jobs/{job_id}/evaluation — read-only latest evaluation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from packages.agents.resume_optimizer.persistence import persist_evaluation
from packages.agents.resume_optimizer.prefilter import (
    MatchStatus,
    RequirementMatch,
    compute_score,
)
from packages.core.repositories import JobEvaluationRepository
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration

MATRIX = {"requirements": [{"text": "AWS", "status": "met", "fact_ids": ["f1"]}]}


def breakdown_for(score: float):  # type: ignore[no-untyped-def]
    result = compute_score([RequirementMatch("AWS", True, MatchStatus.MET, ["f1"])])
    result.score = Decimal(str(score))
    return result


@pytest.fixture
async def normalized_job(session, demo_job):  # type: ignore[no-untyped-def]
    await session.execute(
        text("UPDATE jobs SET status = 'normalized' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()
    return demo_job


# ------------------------------------------------------------- repository
async def test_latest_returns_none_when_never_evaluated(session, demo_job) -> None:
    assert await JobEvaluationRepository(session).latest_for_job(demo_job) is None


async def test_latest_returns_the_most_recent_of_several(session, normalized_job) -> None:
    """Evaluations are append-only, so re-scoring adds a row. The endpoint
    must return the newest, not the first."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=2,
        breakdown=breakdown_for(88), match_matrix=MATRIX,
    )
    latest = await JobEvaluationRepository(session).latest_for_job(normalized_job)
    assert latest is not None
    assert latest.profile_version == 2
    assert latest.score == Decimal("88.00")


async def test_history_is_available_newest_first(session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=2,
        breakdown=breakdown_for(88), match_matrix=MATRIX,
    )
    history = await JobEvaluationRepository(session).list_for_job(normalized_job)
    assert [e.profile_version for e in history] == [2, 1]


# --------------------------------------------------------------- endpoint
async def test_returns_the_latest_evaluation(app_client, session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(87), match_matrix=MATRIX,
    )
    await session.commit()

    response = await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(result.evaluation_id)
    assert body["job_id"] == str(normalized_job)
    assert body["score"] == 87.0
    assert body["recommendation"] == "strong"
    assert body["profile_version"] == 1


async def test_response_contains_no_fabricated_evidence(
    app_client, session, normalized_job
) -> None:
    """A2 does not exist. The response must not imply it does."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(87), match_matrix={},
    )
    await session.commit()

    body = (await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")).json()
    for forbidden in ("evidence", "supporting_facts", "fact_ids", "matched_facts"):
        assert forbidden not in body, f"response claims {forbidden!r}"
    # An empty matrix means "not computed", never "computed and found nothing".
    assert body["match_matrix"] == {}


async def test_missing_evaluation_returns_404_problem_details(
    app_client, session, normalized_job
) -> None:
    await session.commit()
    response = await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "not been evaluated" in response.json()["detail"]


async def test_nonexistent_job_returns_404(app_client) -> None:
    response = await app_client.get(f"/api/v1/jobs/{uuid7()}/evaluation")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


async def test_malformed_job_id_is_rejected(app_client) -> None:
    response = await app_client.get("/api/v1/jobs/not-a-uuid/evaluation")
    assert response.status_code == 422


# ------------------------------------------------------- read-only proof
@pytest.mark.safety
async def test_get_creates_no_evaluation(app_client, session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(87), match_matrix=MATRIX,
    )
    await session.commit()

    before = (
        await session.execute(
            text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()
    for _ in range(3):
        await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")
    after = (
        await session.execute(
            text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()
    assert after == before


@pytest.mark.safety
async def test_get_does_not_change_job_status(app_client, session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(87), match_matrix=MATRIX,
    )
    await session.commit()
    before = (
        await session.execute(text("SELECT status FROM jobs WHERE id = :id"),
                              {"id": normalized_job})
    ).scalar_one()

    await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")

    after = (
        await session.execute(text("SELECT status FROM jobs WHERE id = :id"),
                              {"id": normalized_job})
    ).scalar_one()
    assert after == before


@pytest.mark.safety
async def test_get_emits_no_event(app_client, session, normalized_job) -> None:
    """A read must never append to the outbox."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(87), match_matrix=MATRIX,
    )
    await session.commit()
    before = (
        await session.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()

    await app_client.get(f"/api/v1/jobs/{normalized_job}/evaluation")

    after = (
        await session.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()
    assert after == before
