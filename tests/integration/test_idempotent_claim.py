"""Atomic event claiming (EVENTS.md §4.1).

The concurrency tests here need two genuinely independent connections, so
they build their own sessions rather than using the rolled-back `session`
fixture. A single-connection test cannot distinguish an atomic claim from a
SELECT-then-INSERT: both pass.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agents.resume_optimizer.persistence import (
    persist_evaluation_once,
)
from packages.agents.resume_optimizer.prefilter import (
    MatchStatus,
    RequirementMatch,
    compute_score,
)
from packages.core.events import already_processed, claim_event
from packages.core.models import JobStatus
from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration

GROUP = "cg.optimizer"
MATRIX = {"requirements": [{"text": "AWS", "status": "met", "fact_ids": ["f1"]}]}


def breakdown_for(score: float):  # type: ignore[no-untyped-def]
    result = compute_score([RequirementMatch("AWS", True, MatchStatus.MET, ["f1"])])
    result.score = Decimal(str(score))
    return result


@pytest.fixture
async def committed_job(engine):  # type: ignore[no-untyped-def]
    """A job COMMITTED so a second connection can see it.

    **This fixture deliberately leaves its data behind, and does not clean up.**

    The reason is the immutable audit model, not laziness. Once A5 runs, the
    job has `job_evaluations` and `events` children. Both tables are
    append-only: `trg_job_evaluations_append_only` and
    `trg_events_publish_only` reject DELETE, and no role holds a DELETE grant
    on `events` at all. A `DELETE FROM jobs` therefore attempts a cascading
    delete of the evaluations and is correctly refused by the trigger - which
    is exactly what those triggers exist to do.

    The options were: weaken the trigger (never), delete the audit rows
    (defeats the point), or accept residue. Residue is the honest choice, and
    it is safe here because:

    * every identifier is generated per run (uuid7 ids, unique source slug
      and normalized company name), so repeat runs never collide and remain
      deterministic;
    * nothing is partially deleted, so the graph is never left inconsistent -
      the job, its evaluations, its events and their processed_events claims
      all survive together;
    * the rows are exactly the immutable audit trail the architecture says
      should be kept.

    Tests that do not need cross-connection visibility should use the
    `session` fixture instead, which rolls everything back.
    """
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    source_id, company_id, job_id = uuid7(), uuid7(), uuid7()
    suffix = uuid4().hex[:10]
    async with maker() as setup:
        await setup.execute(
            text("INSERT INTO sources (id, name, slug, domain, created_at, updated_at) "
                 "VALUES (:id, 'Claim Test', :slug, 'example.invalid', now(), now())"),
            {"id": source_id, "slug": f"claim-{suffix}"},
        )
        await setup.execute(
            text("INSERT INTO companies (id, name, normalized_name, created_at, updated_at) "
                 "VALUES (:id, 'Example Cloud Corp', :norm, now(), now())"),
            {"id": company_id, "norm": f"claim-co-{suffix}"},
        )
        await setup.execute(
            text("INSERT INTO jobs (id, source_id, company_id, url, title, status, "
                 "created_at, updated_at) VALUES (:id, :src, :co, :url, "
                 "'Cloud Engineer', 'normalized', now(), now())"),
            {"id": job_id, "src": source_id, "co": company_id,
             "url": f"https://example.invalid/j/{suffix}"},
        )
        await setup.commit()

    yield job_id


# ------------------------------------------------------------ claim basics
async def test_first_claim_succeeds(session) -> None:
    event_id = uuid7()
    assert await claim_event(session, event_id, GROUP) is True


async def test_second_claim_in_the_same_transaction_is_declined(session) -> None:
    event_id = uuid7()
    assert await claim_event(session, event_id, GROUP) is True
    assert await claim_event(session, event_id, GROUP) is False


async def test_conflict_does_not_raise(session) -> None:
    """ON CONFLICT DO NOTHING, not a primary-key violation the caller must
    catch."""
    event_id = uuid7()
    await claim_event(session, event_id, GROUP)
    result = await claim_event(session, event_id, GROUP)  # must not raise
    assert result is False


async def test_different_consumer_groups_claim_independently(session) -> None:
    """Two consumer groups both legitimately process the same event."""
    event_id = uuid7()
    assert await claim_event(session, event_id, "cg.optimizer") is True
    assert await claim_event(session, event_id, "cg.monitor") is True


async def test_claim_is_visible_to_the_read_only_helper(session) -> None:
    event_id = uuid7()
    assert await already_processed(session, event_id, GROUP) is False
    await claim_event(session, event_id, GROUP)
    assert await already_processed(session, event_id, GROUP) is True


# ------------------------------------------------------------- A5 wrapper
async def test_sequential_redelivery_is_a_no_op(session, demo_job) -> None:
    await session.execute(
        text("UPDATE jobs SET status = 'normalized' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()
    event_id = uuid7()
    kwargs = dict(
        job_id=demo_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    first = await persist_evaluation_once(
        session, triggering_event_id=event_id, **kwargs  # type: ignore[arg-type]
    )
    second = await persist_evaluation_once(
        session, triggering_event_id=event_id, **kwargs  # type: ignore[arg-type]
    )
    assert first is not None and second is None

    evaluations = (
        await session.execute(
            text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
            {"id": demo_job},
        )
    ).scalar_one()
    evaluated_events = (
        await session.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :id "
                 "AND type = 'job.evaluated'"),
            {"id": demo_job},
        )
    ).scalar_one()
    assert evaluations == 1
    assert evaluated_events == 1


async def test_claim_records_the_outcome(session, demo_job) -> None:
    await session.execute(
        text("UPDATE jobs SET status = 'normalized' WHERE id = :id"), {"id": demo_job}
    )
    await session.flush()
    event_id = uuid7()
    await persist_evaluation_once(
        session, triggering_event_id=event_id, job_id=demo_job, profile_version=1,
        breakdown=breakdown_for(80), match_matrix=MATRIX,
    )
    result = (
        await session.execute(
            text("SELECT result FROM processed_events WHERE event_id = :id"),
            {"id": event_id},
        )
    ).scalar_one()
    assert result == str(JobStatus.SHORTLISTED)


# ---------------------------------------------------------- rollback/retry
async def test_failure_rolls_back_the_claim_and_permits_retry(engine, committed_job) -> None:
    """A claim that outlived a failed handler would strand the event forever."""
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    event_id = uuid7()

    # Attempt 1: claim, then fail before commit.
    async with maker() as attempt:
        assert await claim_event(attempt, event_id, GROUP) is True
        await attempt.rollback()

    # Attempt 2: the claim must be gone.
    async with maker() as retry:
        assert await already_processed(retry, event_id, GROUP) is False
        assert await claim_event(retry, event_id, GROUP) is True
        await retry.rollback()


async def test_evaluation_failure_rolls_back_claim_and_work(engine, committed_job) -> None:
    from packages.core.state_machines import IllegalTransitionError

    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    event_id = uuid7()

    async with maker() as attempt:
        await attempt.execute(
            text("UPDATE jobs SET status = 'applied' WHERE id = :id"),
            {"id": committed_job},
        )
        await attempt.flush()
        with pytest.raises(IllegalTransitionError):
            await persist_evaluation_once(
                attempt, triggering_event_id=event_id, job_id=committed_job,
                profile_version=1, breakdown=breakdown_for(80), match_matrix=MATRIX,
            )
        await attempt.rollback()

    async with maker() as check:
        assert await already_processed(check, event_id, GROUP) is False
        count = (
            await check.execute(
                text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
                {"id": committed_job},
            )
        ).scalar_one()
        assert count == 0


# ------------------------------------------------------------- concurrency
async def test_two_concurrent_transactions_claim_once(engine, committed_job) -> None:
    """The test a single connection cannot perform.

    Two independent transactions race for the same (event_id, consumer_group).
    Exactly one must win, and exactly one evaluation and one job.evaluated
    event may be committed. A SELECT-then-INSERT implementation fails here.
    """
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    event_id = uuid7()
    started = asyncio.Event()

    async def worker(delay: float) -> bool:
        async with maker() as sess:
            await asyncio.sleep(delay)
            result = await persist_evaluation_once(
                sess, triggering_event_id=event_id, job_id=committed_job,
                profile_version=1, breakdown=breakdown_for(80), match_matrix=MATRIX,
            )
            started.set()
            if result is None:
                await sess.rollback()
                return False
            await sess.commit()
            return True

    outcomes = await asyncio.gather(worker(0.0), worker(0.01), return_exceptions=True)
    for outcome in outcomes:
        assert not isinstance(outcome, Exception), outcome
    assert sum(bool(o) for o in outcomes) == 1, f"exactly one winner expected: {outcomes}"

    async with maker() as check:
        evaluations = (
            await check.execute(
                text("SELECT count(*) FROM job_evaluations WHERE job_id = :id"),
                {"id": committed_job},
            )
        ).scalar_one()
        events = (
            await check.execute(
                text("SELECT count(*) FROM events WHERE aggregate_id = :id "
                     "AND type = 'job.evaluated'"),
                {"id": committed_job},
            )
        ).scalar_one()
        claims = (
            await check.execute(
                text("SELECT count(*) FROM processed_events WHERE event_id = :id"),
                {"id": event_id},
            )
        ).scalar_one()
    assert evaluations == 1, "a duplicate evaluation row was committed"
    assert events == 1, "a duplicate job.evaluated event was committed"
    assert claims == 1


async def test_different_events_both_process(engine, committed_job) -> None:
    """Idempotency must not block genuine re-evaluation."""
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as first:
        result = await persist_evaluation_once(
            first, triggering_event_id=uuid7(), job_id=committed_job,
            profile_version=1, breakdown=breakdown_for(60), match_matrix=MATRIX,
        )
        assert result is not None
        await first.commit()

    async with maker() as second:
        result = await persist_evaluation_once(
            second, triggering_event_id=uuid7(), job_id=committed_job,
            profile_version=2, breakdown=breakdown_for(80), match_matrix=MATRIX,
        )
        assert result is not None
        await second.commit()

    async with maker() as check:
        versions = [
            r[0] for r in (
                await check.execute(
                    text("SELECT profile_version FROM job_evaluations "
                         "WHERE job_id = :id ORDER BY profile_version"),
                    {"id": committed_job},
                )
            ).all()
        ]
    assert versions == [1, 2]
