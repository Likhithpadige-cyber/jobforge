"""A5 — evaluation persistence, status transition, event emission.

Runs against a real PostgreSQL: the append-only trigger on job_evaluations,
the events immutability trigger, and the transactional outbox guarantee are
all database behaviour that cannot be faked.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from packages.agents.resume_optimizer.persistence import (
    Thresholds,
    decide_status,
    persist_evaluation,
    persist_evaluation_once,
)
from packages.agents.resume_optimizer.prefilter import (
    MatchStatus,
    RequirementMatch,
    compute_score,
)
from packages.core.models import JobStatus, Recommendation
from packages.shared.errors import NotFoundError
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration

MATRIX = {"requirements": [{"text": "AWS", "status": "met", "fact_ids": ["f1"]}]}


def breakdown_for(score: float):  # type: ignore[no-untyped-def]
    """Build a ScoreBreakdown with a chosen final score."""
    result = compute_score(
        [RequirementMatch("AWS", True, MatchStatus.MET, ["f1"])],
        seniority_alignment=1.0,
        domain_alignment=1.0,
        location_fit=1.0,
    )
    result.score = Decimal(str(score))
    return result


@pytest.fixture
async def normalized_job(session, demo_job):  # type: ignore[no-untyped-def]
    await session.execute(
        text("UPDATE jobs SET status = 'normalized' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()
    return demo_job


# ------------------------------------------------------------ pure decision
@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100, JobStatus.SHORTLISTED),
        (65, JobStatus.SHORTLISTED),
        (64.99, JobStatus.EVALUATED),
        (55, JobStatus.EVALUATED),
        (54.99, JobStatus.DISCARDED),
        (0, JobStatus.DISCARDED),
    ],
)
def test_threshold_bands(score: float, expected: JobStatus) -> None:
    status, _ = decide_status(Decimal(str(score)), Thresholds())
    assert status is expected


def test_borderline_is_not_auto_discarded() -> None:
    """AGENT_DESIGN.md §3.3: scores between the floor and the shortlist
    threshold go to a human, they are not thrown away."""
    status, reason = decide_status(Decimal("60"), Thresholds())
    assert status is JobStatus.EVALUATED
    assert reason is None


def test_inverted_thresholds_are_rejected() -> None:
    from packages.shared.errors import ValidationError

    with pytest.raises(ValidationError):
        Thresholds(shortlist=Decimal("50"), review_floor=Decimal("70"))


# --------------------------------------------------------------- persistence
async def test_persists_the_evaluation_row(session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    row = (
        await session.execute(
            text("SELECT job_id, score, recommendation, profile_version "
                 "FROM job_evaluations WHERE id = :id"),
            {"id": result.evaluation_id},
        )
    ).one()
    assert row.job_id == normalized_job
    assert row.score == Decimal("80.00")
    assert row.profile_version == 1


async def test_shortlists_above_the_threshold(session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    assert result.to_status is JobStatus.SHORTLISTED
    status = (
        await session.execute(text("SELECT status FROM jobs WHERE id = :id"),
                              {"id": normalized_job})
    ).scalar_one()
    assert status == "shortlisted"


async def test_discards_below_the_floor_with_a_reason(session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(20), match_matrix=MATRIX,
    )
    assert result.to_status is JobStatus.DISCARDED
    row = (
        await session.execute(
            text("SELECT status, discard_reason FROM jobs WHERE id = :id"),
            {"id": normalized_job},
        )
    ).one()
    assert row.status == "discarded"
    assert "review floor" in row.discard_reason


async def test_borderline_stays_evaluated_for_a_human(session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    assert result.to_status is JobStatus.EVALUATED
    assert result.borderline is True


# ------------------------------------------------------------------- events
async def test_emits_job_evaluated(session, normalized_job) -> None:
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    rows = (
        await session.execute(
            text("SELECT type, payload, actor_type, aggregate_type "
                 "FROM events WHERE aggregate_id = :id ORDER BY occurred_at"),
            {"id": normalized_job},
        )
    ).all()
    types = [r.type for r in rows]
    assert "job.evaluated" in types
    evaluated = next(r for r in rows if r.type == "job.evaluated")
    assert evaluated.actor_type == "agent"
    assert evaluated.aggregate_type == "job"
    assert evaluated.payload["evaluation_id"] == str(result.evaluation_id)
    assert evaluated.payload["score"] == 80.0
    assert "gaps" in evaluated.payload


async def test_emits_the_transition_event(session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    types = {
        r[0] for r in (
            await session.execute(
                text("SELECT type FROM events WHERE aggregate_id = :id"),
                {"id": normalized_job},
            )
        ).all()
    }
    assert types == {"job.evaluated", "job.shortlisted"}


async def test_discard_emits_job_discarded(session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(10), match_matrix=MATRIX,
    )
    types = {
        r[0] for r in (
            await session.execute(
                text("SELECT type FROM events WHERE aggregate_id = :id"),
                {"id": normalized_job},
            )
        ).all()
    }
    assert types == {"job.evaluated", "job.discarded"}


async def test_borderline_emits_only_job_evaluated(session, normalized_job) -> None:
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    types = {
        r[0] for r in (
            await session.execute(
                text("SELECT type FROM events WHERE aggregate_id = :id"),
                {"id": normalized_job},
            )
        ).all()
    }
    assert types == {"job.evaluated"}


async def test_events_are_written_unpublished(session, normalized_job) -> None:
    """The outbox relay publishes; the producer never does."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    published = (
        await session.execute(
            text("SELECT bool_or(published) FROM events WHERE aggregate_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()
    assert published is False


async def test_correlation_id_ties_the_events_together(session, normalized_job) -> None:
    correlation = uuid7()
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
        correlation_id=correlation,
    )
    ids = {
        r[0] for r in (
            await session.execute(
                text("SELECT correlation_id FROM events WHERE aggregate_id = :id"),
                {"id": normalized_job},
            )
        ).all()
    }
    assert ids == {correlation}


# -------------------------------------------------------------- idempotency
async def test_repeated_delivery_creates_one_evaluation(session, normalized_job) -> None:
    """At-least-once delivery must not produce two evaluations."""
    event_id = uuid7()
    kwargs = dict(
        job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    first = await persist_evaluation_once(
        session, triggering_event_id=event_id, **kwargs  # type: ignore[arg-type]
    )
    second = await persist_evaluation_once(
        session, triggering_event_id=event_id, **kwargs  # type: ignore[arg-type]
    )
    assert first is not None
    assert second is None, "redelivery must be a no-op"

    count = (
        await session.execute(
            text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
            {"id": normalized_job},
        )
    ).scalar_one()
    assert count == 1


async def test_genuine_re_evaluation_creates_a_new_row(session, normalized_job) -> None:
    """job_evaluations is append-only by design: a newer profile version is a
    new row, not an overwrite (DATABASE_DESIGN.md §5.6)."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=2,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    versions = [
        r[0] for r in (
            await session.execute(
                text("SELECT profile_version FROM job_evaluations "
                     "WHERE job_id = :id ORDER BY profile_version"),
                {"id": normalized_job},
            )
        ).all()
    ]
    assert versions == [1, 2]


# ----------------------------------------------------------- error handling
async def test_missing_job_raises(session) -> None:
    with pytest.raises(NotFoundError):
        await persist_evaluation(
            session, job_id=uuid7(), profile_version=1,
            breakdown=breakdown_for(80), match_matrix=MATRIX,
        )


async def test_illegal_transition_rolls_everything_back(session, demo_job) -> None:
    """A job already applied cannot be re-evaluated. The evaluation row and
    the events must not survive the failed transition."""
    from packages.core.state_machines import IllegalTransitionError

    await session.execute(
        text("UPDATE jobs SET status = 'applied' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()

    with pytest.raises(IllegalTransitionError):
        await persist_evaluation(
            session, job_id=demo_job, profile_version=1,
            breakdown=breakdown_for(80), match_matrix=MATRIX,
        )
    await session.rollback()

    count = (
        await session.execute(
            text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
            {"id": demo_job},
        )
    ).scalar_one()
    assert count == 0


# ------------------------------------------------------ security boundaries
@pytest.mark.safety
async def test_evaluation_rows_remain_append_only(session, normalized_job) -> None:
    """The append-only trigger must still reject an update to an evaluation."""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        await session.execute(
            text("UPDATE job_evaluations SET score = 99 WHERE id = :id"),
            {"id": result.evaluation_id},
        )
        await session.flush()
    assert "append-only" in str(exc.value)
    await session.rollback()


@pytest.mark.safety
async def test_emitted_events_remain_immutable(session, normalized_job) -> None:
    """A5 writes events through the outbox; the hardened guard still applies."""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    event_id = (
        await session.execute(
            text("SELECT id FROM events WHERE aggregate_id = :id LIMIT 1"),
            {"id": normalized_job},
        )
    ).scalar_one()
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        await session.execute(
            text("UPDATE events SET payload = '{}'::jsonb WHERE id = :id"),
            {"id": event_id},
        )
        await session.flush()
    assert "payload" in str(exc.value)
    await session.rollback()


def test_a5_performs_no_inference() -> None:
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "packages/agents/resume_optimizer/persistence.py"
    )
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import | ast.ImportFrom):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            imports |= {m.split(".")[0] for m in mods if m}
    assert not (imports & {"anthropic", "openai", "httpx", "requests"})


# ------------------------------------------------- documented lifecycle path
@pytest.mark.parametrize(
    ("score", "expected_final", "expected_events"),
    [
        (80, JobStatus.SHORTLISTED, ["job.evaluated", "job.shortlisted"]),
        (60, JobStatus.EVALUATED, ["job.evaluated"]),
        (20, JobStatus.DISCARDED, ["job.evaluated", "job.discarded"]),
    ],
)
async def test_lifecycle_sequence(
    session, normalized_job, score: float, expected_final: JobStatus,
    expected_events: list[str],
) -> None:
    """normalized -> evaluated -> (shortlisted | discarded), or stop at
    evaluated when borderline. ARCHITECTURE.md §7.1."""
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(score), match_matrix=MATRIX,
    )
    assert result.from_status is JobStatus.NORMALIZED, "from_status is the status before A5"
    assert result.to_status is expected_final
    assert result.events_emitted == expected_events, "events follow the lifecycle order"

    status = (
        await session.execute(text("SELECT status FROM jobs WHERE id = :id"),
                              {"id": normalized_job})
    ).scalar_one()
    assert status == str(expected_final)


def test_shortlisted_is_unreachable_without_passing_through_evaluated() -> None:
    """The proof that A5 cannot skip the state: the graph has no such edge,
    so reaching `shortlisted` from `normalized` requires the intermediate
    move. If A5 ever jumped straight there, transition() would raise."""
    from packages.core.state_machines import can_transition

    assert can_transition(JobStatus.NORMALIZED, JobStatus.SHORTLISTED) is False
    assert can_transition(JobStatus.NORMALIZED, JobStatus.DISCARDED) is True
    assert can_transition(JobStatus.NORMALIZED, JobStatus.EVALUATED) is True
    assert can_transition(JobStatus.EVALUATED, JobStatus.SHORTLISTED) is True


async def test_transition_is_called_in_the_documented_order(
    session, normalized_job, monkeypatch
) -> None:
    """Records the actual sequence of transition() calls A5 makes."""
    import packages.agents.resume_optimizer.persistence as module

    calls: list[tuple[str, str]] = []
    real = module.transition

    def recording(job, target, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((str(job.status), str(target)))
        return real(job, target, **kwargs)

    monkeypatch.setattr(module, "transition", recording)
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    assert calls == [("normalized", "evaluated"), ("evaluated", "shortlisted")]


async def test_re_evaluation_from_shortlisted_is_allowed(session, normalized_job) -> None:
    """A job already shortlisted may be re-evaluated against a new profile
    version; the documented graph permits shortlisted -> evaluated."""
    await persist_evaluation(
        session, job_id=normalized_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    result = await persist_evaluation(
        session, job_id=normalized_job, profile_version=2,
        breakdown=breakdown_for(60), match_matrix=MATRIX,
    )
    assert result.from_status is JobStatus.SHORTLISTED
    assert result.to_status is JobStatus.EVALUATED
