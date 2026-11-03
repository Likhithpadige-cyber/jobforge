"""T1-T5 mechanics against real PostgreSQL (C3_HARDENING_DESIGN.md D.2-D.6).

Each repository method is one guarded `UPDATE ... WHERE`, so the thing worth
testing is the predicate, not the Python around it: does a second worker get
0 rows, does the fence actually fence, is the evidence-stale budget consumed
exactly once. Those answers live in the database, so the tests go there.

Deliberately below the HTTP layer. The pipeline needs cassettes, a fake Redis
and an app instance; the state machine needs none of that, and mixing them
would mean a lease bug could only ever be found through a generation failure.

Every deliberately-failing statement runs in `session.begin_nested()`, so the
savepoint unwinds rather than the fixture.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.core.models import RevisionFailureClass, RevisionOutcome
from packages.core.repositories.revision_attempts import RevisionAttemptRepository

pytestmark = [pytest.mark.integration, pytest.mark.safety]

MAX_ATTEMPTS = 3
BUDGET = 1
TTL = 300


async def _repo(session):  # type: ignore[no-untyped-def]
    return RevisionAttemptRepository(session)


async def _row(session, attempt_id):  # type: ignore[no-untyped-def]
    return (
        await session.execute(
            text(
                "SELECT status, lease_owner, lease_epoch, attempt_count, "
                "       evidence_stale_retry_count, retry_exhausted, recovered_at "
                "  FROM resume_revision_attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        )
    ).one()


# =====================================================================
# T2 - acquisition and fencing
# =====================================================================
async def test_acquire_takes_the_lease_and_increments_once(session, revision_attempt) -> None:
    repo = await _repo(session)
    leased = await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    status, owner, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, owner, epoch, count) == ("running", "w1", 1, 1)
    # The epoch the CALLER got back, not just the one in the row. Every test
    # here used to check the latter via raw SQL and none checked the former,
    # so an `acquire()` that returned a stale epoch was invisible to all of
    # them while breaking every fenced statement the service issued after it.
    assert leased == epoch


async def test_a_second_worker_cannot_take_a_live_lease(session, revision_attempt) -> None:
    """The predicate is the lock: no read-then-write, so both cannot win."""
    repo = await _repo(session)
    assert await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        is None
    )
    _, owner, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (owner, epoch, count) == ("w1", 1, 1)


async def test_an_expired_lease_is_taken_over_and_the_epoch_advances(
    session, revision_attempt
) -> None:
    """C.1 row 3. The epoch bump is what fences the previous owner out."""
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
            "interval '1 second' WHERE id = :id"
        ),
        {"id": revision_attempt["id"]},
    )
    leased = await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    _, owner, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (owner, epoch, count) == ("w2", 2, 2)
    assert leased == epoch


async def test_the_ceiling_refuses_a_further_acquisition(session, revision_attempt) -> None:
    repo = await _repo(session)
    for i in range(MAX_ATTEMPTS):
        assert await repo.acquire(
            revision_attempt["id"],
            owner=f"w{i}",
            lease_seconds=TTL,
            max_attempts=MAX_ATTEMPTS,
        )
        await session.execute(
            text(
                "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
                "interval '1 second' WHERE id = :id"
            ),
            {"id": revision_attempt["id"]},
        )
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w9", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        is None
    )
    *_, count, _, _, _ = await _row(session, revision_attempt["id"])
    assert count == MAX_ATTEMPTS


async def test_a_fenced_out_worker_writes_nothing(session, revision_attempt, demo_resume) -> None:
    """The invariant the whole epoch mechanism exists for.

    w1 is taken over mid-flight. Every subsequent statement it issues carries
    its stale epoch and must match 0 rows - not raise, not partially apply.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
            "interval '1 second' WHERE id = :id"
        ),
        {"id": revision_attempt["id"]},
    )
    await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )

    assert not await repo.succeed(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="stale",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        is None
    )
    assert not await repo.renew(revision_attempt["id"], owner="w1", epoch=1, lease_seconds=TTL)
    status, owner, epoch, *_ = await _row(session, revision_attempt["id"])
    assert (status, owner, epoch) == ("running", "w2", 2)


async def test_renew_extends_without_moving_epoch_or_count(session, revision_attempt) -> None:
    """D.5. Nothing schedules this yet - generation is in-process - but the
    contract is real and the fence depends on it not advancing anything."""
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.renew(revision_attempt["id"], owner="w1", epoch=1, lease_seconds=TTL * 2)
    status, owner, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, owner, epoch, count) == ("running", "w1", 1, 1)


# =====================================================================
# T4 / T4' - outcomes
# =====================================================================
async def test_success_links_the_result(session, revision_attempt, demo_resume) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.succeed(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    row = (
        await session.execute(
            text(
                "SELECT status, result_resume_id, result_version, outcome, "
                "       finished_at IS NOT NULL "
                "  FROM resume_revision_attempts WHERE id = :id"
            ),
            {"id": revision_attempt["id"]},
        )
    ).one()
    assert row == ("succeeded", demo_resume, 2, "passed", True)


async def test_a_failed_verification_is_a_succeeded_attempt(
    session, revision_attempt, demo_resume
) -> None:
    """The single most important distinction in the design.

    The job did what it should; the content did not verify. Recording it as a
    technical failure would make "the model broke" and "the resume overclaimed"
    indistinguishable, and only one of those is worth retrying.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.succeed(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.FAILED_VERIFICATION,
    )
    status, *_ = await _row(session, revision_attempt["id"])
    assert status == "succeeded"


async def test_a_retryable_failure_returns_the_attempt_to_accepted(
    session, revision_attempt
) -> None:
    """C.1 row 6. The lease clears, but `attempt_count` does NOT move here -
    acquisition is the only place it advances."""
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="timeout",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    status, owner, _, count, stale, exhausted, _ = await _row(session, revision_attempt["id"])
    assert (status, owner, count, stale, exhausted) == ("accepted", None, 1, 0, False)


async def test_a_terminal_failure_stays_failed(session, revision_attempt) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.BUDGET_EXCEEDED,
            detail="over budget",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "failed"
    )
    status, *_ = await _row(session, revision_attempt["id"])
    assert status == "failed"


async def test_the_evidence_stale_budget_is_consumed_exactly_once(
    session, revision_attempt
) -> None:
    """Gate 1, end to end through the real statement.

    The first stale failure is retryable and spends the budget; the second is
    terminal. Crucially the decision and the consumption are one statement, so
    two reports racing cannot both spend it.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.EVIDENCE_STALE,
            detail="moved",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    *_, stale, _, _ = await _row(session, revision_attempt["id"])
    assert stale == 1

    await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w2",
            epoch=2,
            failure_class=RevisionFailureClass.EVIDENCE_STALE,
            detail="moved again",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "failed"
    )
    status, _, _, count, stale, *_ = await _row(session, revision_attempt["id"])
    assert (status, count, stale) == ("failed", 2, 1)


async def test_the_epoch_acquire_returns_is_the_one_that_fences(session, revision_attempt) -> None:
    """The round trip, end to end, with no hand-written epoch anywhere.

    Every other test in this file passes `epoch=1` as a literal, which is
    correct and proves nothing about the value a caller actually holds. This
    one fences `fail()` with exactly what `acquire()` handed back, which is
    what the service does - and is the only assertion that would have caught
    an `acquire()` returning a pre-acquisition epoch.
    """
    repo = await _repo(session)
    first = await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert first is not None
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=first,
            failure_class=RevisionFailureClass.PERSISTENCE_FAILED,
            detail="boom",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )

    # And again after a takeover, where the epoch has genuinely moved.
    second = await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert second is not None and second != first
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w2",
            epoch=second,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="slow",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    # The first owner's epoch is now dead, exactly as fencing promises.
    await repo.acquire(
        revision_attempt["id"], owner="w3", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=first,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="stale owner",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        is None
    )


# =====================================================================
# T5 - recovery, once
# =====================================================================
async def test_recovery_is_marked_once(session, revision_attempt) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await repo.fail(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        failure_class=RevisionFailureClass.LEASE_LOST,
        detail="lost",
        max_attempts=MAX_ATTEMPTS,
        evidence_stale_budget=BUDGET,
    )
    assert await repo.mark_recovered(revision_attempt["id"])
    assert not await repo.mark_recovered(revision_attempt["id"])
    *_, recovered = await _row(session, revision_attempt["id"])
    assert recovered is not None


async def test_an_accepted_attempt_is_never_stamped_recovered(session, revision_attempt) -> None:
    """`recovered_at` is failed-only, and stays failed-only.

    This is the Batch 1 contract and nothing below widens it. The application
    side of recovery is a *separate* question, answered by `recovery_state()`
    and pinned in the tests that follow: an `accepted` attempt can release the
    application without ever earning a `recovered_at` stamp.
    """
    repo = await _repo(session)
    assert not await repo.mark_recovered(revision_attempt["id"])


# ---------------------------------------------------------------------
# T5 - does this attempt still hold the application?
# ---------------------------------------------------------------------
async def test_a_never_run_attempt_holds_no_application(session, revision_attempt) -> None:
    """`accepted` with no recorded failure never took the application."""
    repo = await _repo(session)
    assert await repo.recovery_state(revision_attempt["id"]) == ("accepted", False)


async def test_a_live_worker_is_never_recovered_out_from_under(session, revision_attempt) -> None:
    """The guard that matters most here.

    A `running` attempt with a live lease has a worker inside T3 right now.
    Moving its application to `changes_requested` would let a second request
    start while the first is still generating - two pipelines, one approval.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.recovery_state(revision_attempt["id"]) == ("running", False)


async def test_a_retryable_failure_releases_the_application_without_a_stamp(
    session, revision_attempt
) -> None:
    """The case that was stranding applications.

    The lease is clear and a failure is on record, so nothing is working on
    this application and it must be returned to `changes_requested` - but the
    attempt is `accepted`, not `failed`, so `recovered_at` must stay NULL and
    the very same attempt must remain acquirable.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.PERSISTENCE_FAILED,
            detail="boom",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    assert await repo.recovery_state(revision_attempt["id"]) == ("accepted", True)

    status, owner, _, count, _, _, recovered = await _row(session, revision_attempt["id"])
    assert (status, owner, count, recovered) == ("accepted", None, 1, None)
    assert not await repo.mark_recovered(revision_attempt["id"])

    # ...and it is still the same attempt, re-acquirable, counting up.
    again = await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert again == 2
    status, _, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, epoch, count) == ("running", 2, 2)


async def test_a_terminal_failure_releases_the_application_and_is_stamped(
    session, revision_attempt
) -> None:
    """Terminal recovery semantics, unchanged from Batch 1."""
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await repo.fail(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        failure_class=RevisionFailureClass.BUDGET_EXCEEDED,
        detail="over budget",
        max_attempts=MAX_ATTEMPTS,
        evidence_stale_budget=BUDGET,
    )
    assert await repo.recovery_state(revision_attempt["id"]) == ("failed", True)
    assert await repo.mark_recovered(revision_attempt["id"])
    assert not await repo.mark_recovered(revision_attempt["id"])
    *_, recovered = await _row(session, revision_attempt["id"])
    assert recovered is not None
    # Still true afterwards: the stamp records that recovery happened, it is
    # not a latch that makes the attempt look live again.
    assert await repo.recovery_state(revision_attempt["id"]) == ("failed", True)


async def test_a_succeeded_attempt_holds_no_application(
    session, revision_attempt, demo_resume
) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.succeed(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    assert await repo.recovery_state(revision_attempt["id"]) == ("succeeded", False)


# =====================================================================
# The five re-entry cases, against the real trigger matrix
# =====================================================================
async def test_case_1_retryable_failure_with_budget_stays_nonterminal(
    session, revision_attempt
) -> None:
    """(1) Retryable + budget remains => the SAME attempt, still eligible.

    The attempt must never pass through terminal `failed` on its way to a
    retry: `failed` is terminal by trigger, so an implementation that wrote it
    first and re-drove afterwards would be refused by the database - correctly.
    `accepted` is where a retryable failure belongs, and `acquire()` accepts it.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w1",
            epoch=1,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="timeout",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    # Same row, re-acquirable, count moves only on acquisition.
    leased = await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert leased == 2
    status, owner, epoch, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, owner, epoch, count) == ("running", "w2", 2, 2)


async def test_case_2_exhausted_budget_becomes_terminal_failed(session, revision_attempt) -> None:
    """(2) Budget spent => `failed`, and `failed` is the end of the line."""
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await repo.fail(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        failure_class=RevisionFailureClass.EVIDENCE_STALE,
        detail="moved",
        max_attempts=MAX_ATTEMPTS,
        evidence_stale_budget=BUDGET,
    )
    await repo.acquire(
        revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w2",
            epoch=2,
            failure_class=RevisionFailureClass.EVIDENCE_STALE,
            detail="moved again",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "failed"
    )
    assert (
        await repo.acquire(
            revision_attempt["id"],
            owner="w3",
            lease_seconds=TTL,
            max_attempts=MAX_ATTEMPTS,
        )
        is None
    )


async def test_case_3_a_terminal_failed_attempt_is_never_reacquired(
    session, revision_attempt
) -> None:
    """(3) Replay against a terminal attempt: refused, twice over.

    `acquire()`'s predicate excludes `failed`, and the trigger forbids
    `failed -> running` outright - so even a direct write cannot resurrect it.
    Belt and braces, deliberately: this is the invariant Batch 1's
    `test_failed_is_never_resurrected` protects.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await repo.fail(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        failure_class=RevisionFailureClass.BUDGET_EXCEEDED,
        detail="over",
        max_attempts=MAX_ATTEMPTS,
        evidence_stale_budget=BUDGET,
    )
    for _ in range(2):
        assert (
            await repo.acquire(
                revision_attempt["id"],
                owner="w2",
                lease_seconds=TTL,
                max_attempts=MAX_ATTEMPTS,
            )
            is None
        )
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await session.execute(
                text(
                    "UPDATE resume_revision_attempts SET status = 'running', "
                    "lease_owner = 'w2', lease_expires_at = now() + interval '5 min' "
                    "WHERE id = :id"
                ),
                {"id": revision_attempt["id"]},
            )
    assert "not permitted" in str(exc.value).lower()
    status, *_ = await _row(session, revision_attempt["id"])
    assert status == "failed"


async def test_case_4_reconciles_a_result_committed_before_t4(
    session, revision_attempt, demo_resume
) -> None:
    """(4) T3 committed, process died, T4 never ran.

    The attempt is stranded in `running` with a dead lease while a perfectly
    good resume exists. Re-entry adopts it: no new version, no new attempt,
    `attempt_count` unchanged because reconciliation is bookkeeping rather
    than a fresh try.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"],
        owner="dead",
        lease_seconds=TTL,
        max_attempts=MAX_ATTEMPTS,
    )
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
            "interval '1 second' WHERE id = :id"
        ),
        {"id": revision_attempt["id"]},
    )
    assert await repo.reconcile_succeeded(
        revision_attempt["id"],
        owner="reconciler",
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    status, _, _, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, count) == ("succeeded", 1)


async def test_case_4b_reconciliation_respects_a_live_lease(
    session, revision_attempt, demo_resume
) -> None:
    """A live lease means the original worker may still run its own T4.

    Two writers must not both decide one attempt's outcome, so reconciliation
    declines rather than racing.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"],
        owner="alive",
        lease_seconds=TTL,
        max_attempts=MAX_ATTEMPTS,
    )
    assert not await repo.reconcile_succeeded(
        revision_attempt["id"],
        owner="reconciler",
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    status, owner, *_ = await _row(session, revision_attempt["id"])
    assert (status, owner) == ("running", "alive")


async def test_case_4c_reconciles_from_accepted_without_a_forbidden_jump(
    session, revision_attempt, demo_resume
) -> None:
    """T4' reported a retryable failure after T3 had already committed.

    The attempt sits in `accepted` with a durable resume behind it. The
    trigger forbids `accepted -> succeeded`, so reconciliation claims the row
    into `running` first - two legal transitions, no weakening.
    """
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    await repo.fail(
        revision_attempt["id"],
        owner="w1",
        epoch=1,
        failure_class=RevisionFailureClass.MODEL_TIMEOUT,
        detail="died after T3",
        max_attempts=MAX_ATTEMPTS,
        evidence_stale_budget=BUDGET,
    )
    status, *_ = await _row(session, revision_attempt["id"])
    assert status == "accepted"

    assert await repo.reconcile_succeeded(
        revision_attempt["id"],
        owner="reconciler",
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    status, _, _, count, *_ = await _row(session, revision_attempt["id"])
    assert (status, count) == ("succeeded", 1)


async def test_case_5_reconciliation_creates_no_second_version(
    session, revision_attempt, demo_application, demo_resume
) -> None:
    """(5) The whole point: one request, one resume version.

    Counts tailored resumes for the application before and after
    reconciliation. `reconcile_succeeded` writes only to the attempt row - it
    has no path to `resumes` at all - and this asserts that rather than
    assuming it.
    """
    repo = await _repo(session)

    async def _versions() -> list[int]:
        rows = await session.execute(
            text(
                "SELECT version FROM resumes WHERE application_id = :app "
                " AND kind = 'tailored' ORDER BY version"
            ),
            {"app": demo_application["id"]},
        )
        return [row[0] for row in rows]

    before = await _versions()
    await repo.acquire(
        revision_attempt["id"],
        owner="dead",
        lease_seconds=TTL,
        max_attempts=MAX_ATTEMPTS,
    )
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
            "interval '1 second' WHERE id = :id"
        ),
        {"id": revision_attempt["id"]},
    )
    await repo.reconcile_succeeded(
        revision_attempt["id"],
        owner="reconciler",
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    assert await _versions() == before


# =====================================================================
# M12 Phase 1 - operational generation pause
# =====================================================================
async def _expire(session, attempt_id) -> None:  # type: ignore[no-untyped-def]
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET lease_expires_at = now() - "
            "interval '1 second' WHERE id = :id"
        ),
        {"id": attempt_id},
    )


async def _pause_fields(session, attempt_id):  # type: ignore[no-untyped-def]
    return (
        await session.execute(
            text(
                "SELECT status, lease_owner, lease_expires_at, lease_epoch, attempt_count, "
                "       failure_class, failure_detail, finished_at "
                "  FROM resume_revision_attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        )
    ).one()


async def test_pause_release_returns_to_accepted_without_a_failure_or_new_count(
    session, revision_attempt
) -> None:
    repo = await _repo(session)
    epoch = await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert epoch == 1
    assert await repo.release_for_generation_pause(revision_attempt["id"], owner="w1", epoch=1)
    status, owner, expires, epoch, count, failure, detail, finished = await _pause_fields(
        session, revision_attempt["id"]
    )
    assert (status, owner, expires, epoch, count, failure, finished) == (
        "accepted",
        None,
        None,
        1,
        1,
        None,
        None,
    )
    assert detail is not None

    # The resumed acquisition continues the paused one: fenced as usual (epoch
    # advances), but no second count is charged and the marker is consumed.
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        == 2
    )
    status, owner, _, epoch, count, failure, detail, _ = await _pause_fields(
        session, revision_attempt["id"]
    )
    assert (status, owner, epoch, count, failure, detail) == (
        "running",
        "w2",
        2,
        1,
        None,
        None,
    )

    # Ordinary retryable failures are still charged on the next acquisition.
    assert (
        await repo.fail(
            revision_attempt["id"],
            owner="w2",
            epoch=2,
            failure_class=RevisionFailureClass.MODEL_TIMEOUT,
            detail="timeout",
            max_attempts=MAX_ATTEMPTS,
            evidence_stale_budget=BUDGET,
        )
        == "accepted"
    )
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w3", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        == 3
    )
    *_, count, _, _, _ = await _pause_fields(session, revision_attempt["id"])
    assert count == 2


async def test_pause_release_rejects_stale_owner_and_epoch(session, revision_attempt) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert not await repo.release_for_generation_pause(
        revision_attempt["id"], owner="intruder", epoch=1
    )
    assert not await repo.release_for_generation_pause(revision_attempt["id"], owner="w1", epoch=7)
    # w1 is taken over mid-flight; its pause cleanup must not touch w2's lease.
    await _expire(session, revision_attempt["id"])
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        == 2
    )
    assert not await repo.release_for_generation_pause(revision_attempt["id"], owner="w1", epoch=1)
    status, owner, expires, epoch, count, failure, detail, _ = await _pause_fields(
        session, revision_attempt["id"]
    )
    assert (status, owner, epoch, count, failure, detail) == (
        "running",
        "w2",
        2,
        2,
        None,
        None,
    )
    assert expires is not None


async def test_pause_release_happens_once_and_never_after_an_outcome(
    session, revision_attempt, demo_resume
) -> None:
    repo = await _repo(session)
    await repo.acquire(
        revision_attempt["id"], owner="w1", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert await repo.release_for_generation_pause(revision_attempt["id"], owner="w1", epoch=1)
    # A duplicate cleanup by the same owner finds no running lease.
    assert not await repo.release_for_generation_pause(revision_attempt["id"], owner="w1", epoch=1)
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="w2", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        == 2
    )
    assert await repo.succeed(
        revision_attempt["id"],
        owner="w2",
        epoch=2,
        result_resume_id=demo_resume,
        result_version=2,
        outcome=RevisionOutcome.PASSED,
    )
    assert not await repo.release_for_generation_pause(revision_attempt["id"], owner="w2", epoch=2)
    status, *_ = await _row(session, revision_attempt["id"])
    assert status == "succeeded"


async def test_a_pause_on_the_last_permitted_acquisition_stays_recoverable(
    session, revision_attempt
) -> None:
    repo = await _repo(session)
    for i in range(MAX_ATTEMPTS - 1):
        await repo.acquire(
            revision_attempt["id"],
            owner=f"w{i}",
            lease_seconds=TTL,
            max_attempts=MAX_ATTEMPTS,
        )
        await _expire(session, revision_attempt["id"])
    last = await repo.acquire(
        revision_attempt["id"], owner="last", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
    )
    assert last == MAX_ATTEMPTS
    assert await repo.release_for_generation_pause(revision_attempt["id"], owner="last", epoch=last)
    # Without the pause marker an accepted row at the ceiling is unacquirable.
    assert (
        await repo.acquire(
            revision_attempt["id"], owner="resumed", lease_seconds=TTL, max_attempts=MAX_ATTEMPTS
        )
        == MAX_ATTEMPTS + 1
    )
    status, owner, _, _, count, *_ = await _pause_fields(session, revision_attempt["id"])
    assert (status, owner, count) == ("running", "resumed", MAX_ATTEMPTS)
