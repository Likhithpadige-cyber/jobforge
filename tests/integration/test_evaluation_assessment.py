"""M5 database contract, migration and transactional persistence regression tests."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.api.schemas.jobs import EvaluationRead
from packages.agents.resume_optimizer.persistence import persist_evaluation
from packages.core.models import Event, Job, JobEvaluation, JobStatus
from packages.shared.sql_script import split_sql_statements
from tests.integration.test_evaluation_persistence import breakdown_for

pytestmark = [pytest.mark.integration, pytest.mark.safety]
SQL = Path(__file__).resolve().parents[2] / "migrations" / "sql"


async def run_sql(session, direction):
    for statement in split_sql_statements(
        (SQL / f"0007_evaluation_assessment.{direction}.sql").read_text()
    ):
        await session.execute(text(statement))


@pytest.fixture
async def migration_table(session):
    schema = f"m5_{uuid4().hex}"
    await session.execute(text(f'CREATE SCHEMA "{schema}"'))
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await session.execute(
        text(
            "CREATE TABLE job_evaluations (id integer PRIMARY KEY, score numeric(5,2) NOT NULL, "
            "recommendation text NOT NULL, evidence text NOT NULL)"
        )
    )
    await session.execute(
        text("INSERT INTO job_evaluations VALUES (1, 80, 'strong', 'historical evidence')")
    )
    return session


async def test_migration_backfills_and_reverses_without_rewriting_history(migration_table):
    db = migration_table
    await run_sql(db, "up")
    row = (await db.execute(text("SELECT * FROM job_evaluations"))).mappings().one()
    assert dict(row) == {
        "id": 1,
        "score": 80,
        "recommendation": "strong",
        "evidence": "historical evidence",
        "assessment_status": "assessed",
    }
    await run_sql(db, "down")
    row = (
        (await db.execute(text("SELECT id, score, recommendation, evidence FROM job_evaluations")))
        .mappings()
        .one()
    )
    assert dict(row) == {
        "id": 1,
        "score": 80,
        "recommendation": "strong",
        "evidence": "historical evidence",
    }


async def test_downgrade_refuses_unresolved_history_without_loss(migration_table):
    db = migration_table
    await run_sql(db, "up")
    await db.execute(
        text(
            "INSERT INTO job_evaluations VALUES (2, NULL, NULL, 'available evidence', "
            "'insufficient_evidence')"
        )
    )
    with pytest.raises(DBAPIError, match="Cannot downgrade: unresolved evaluations"):
        async with db.begin_nested():
            await run_sql(db, "down")
    rows = (await db.execute(text("SELECT * FROM job_evaluations ORDER BY id"))).mappings().all()
    assert len(rows) == 2
    assert rows[1]["score"] is None
    assert rows[1]["evidence"] == "available evidence"
    assert rows[1]["assessment_status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    ("status", "score", "recommendation"),
    [
        ("assessed", None, None),
        ("assessed", 80, None),
        ("assessed", None, "strong"),
        ("insufficient_evidence", 0, None),
        ("insufficient_evidence", None, "no"),
        ("insufficient_evidence", 80, "strong"),
        ("other", None, None),
    ],
)
async def test_database_rejects_invalid_combinations(
    session,
    demo_job,
    status,
    score,
    recommendation,
):
    with pytest.raises(IntegrityError, match="assessment_contract"):
        async with session.begin_nested():
            session.add(
                JobEvaluation(
                    job_id=demo_job,
                    profile_version=1,
                    match_matrix={},
                    assessment_status=status,
                    score=score,
                    recommendation=recommendation,
                )
            )
            await session.flush()


@pytest.mark.parametrize("status", ["assessed", "insufficient_evidence"])
async def test_persistence_and_api_contract(session, demo_job, status):
    job = await session.get(Job, demo_job)
    job.status = JobStatus.NORMALIZED
    result = await persist_evaluation(
        session,
        job_id=demo_job,
        profile_version=1,
        match_matrix={"available": "evidence"},
        assessment_status=status,
        breakdown=breakdown_for(80) if status == "assessed" else None,
        insufficient_reasons=["seniority alignment is unknown"] if status != "assessed" else None,
    )
    row = await session.get(JobEvaluation, result.evaluation_id)
    body = EvaluationRead.model_validate(row).model_dump(mode="json")
    assert body["assessment_status"] == status
    assert body["match_matrix"] == {"available": "evidence"}
    if status == "assessed":
        assert body["score"] == 80
        assert body["recommendation"] is not None
        assert job.status == JobStatus.SHORTLISTED
    else:
        assert body["score"] is body["recommendation"] is None
        assert job.status == JobStatus.EVALUATED
        assert "seniority alignment is unknown" in body["rationale"]
        assert result.events_emitted == ["job.evaluated"]
        events = list(
            (
                await session.scalars(
                    select(Event).where(
                        Event.aggregate_id == demo_job,
                        Event.type.in_(["job.shortlisted", "job.discarded"]),
                    )
                )
            ).all()
        )
        assert not events
