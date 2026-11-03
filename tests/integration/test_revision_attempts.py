"""Durable revision attempts: constraints, triggers and the lifecycle table.

Batch 1 of C3_HARDENING_DESIGN.md Q.11. Everything here is asserted against a
real PostgreSQL, because every claim under test is enforced by the database
rather than by application code - which is the point. A trigger that is
believed to reject something, and does not, is worse than no trigger: it is a
guarantee the rest of the design leans on.

Covers J.15 (the lifecycle table is enforced), J.18 (version collision
backstop), and the A.3 constraints.

**On transactions.** Every deliberately-failing statement runs inside
`session.begin_nested()`, so the SAVEPOINT it creates is what unwinds - not
the whole transaction. A bare `session.rollback()` after an expected failure
also discards the fixture row and whatever setup transition the test just
made, and the next statement then updates *zero* rows: no trigger fires,
nothing raises, and the test reports DID NOT RAISE while the trigger it was
checking is working perfectly. Savepoints keep the established state and undo
only the rejected statement.

`IntegrityError` is a subclass of `DBAPIError`, so CHECK violations and
trigger exceptions are both caught by the latter.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]


# --------------------------------------------------------------- helpers
async def _set(session, attempt_id, **columns) -> None:  # type: ignore[no-untyped-def]
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    await session.execute(
        text(f"UPDATE resume_revision_attempts SET {assignments} WHERE id = :id"),  # noqa: S608
        {"id": attempt_id, **columns},
    )


async def _set_raw(session, attempt_id, column, value) -> None:  # type: ignore[no-untyped-def]
    """Assign a SQL expression rather than a bound value (now(), NULL, ...)."""
    sql = f"UPDATE resume_revision_attempts SET {column} = {value} WHERE id = :id"  # noqa: S608
    await session.execute(text(sql), {"id": attempt_id})


async def _status(session, attempt_id) -> str:  # type: ignore[no-untyped-def]
    result = await session.execute(
        text("SELECT status FROM resume_revision_attempts WHERE id = :id"),
        {"id": attempt_id},
    )
    return str(result.scalar_one())


async def _to_running(session, attempt_id) -> None:  # type: ignore[no-untyped-def]
    """accepted -> running, the way D.2 acquisition does it.

    Written out rather than routed through `_set` because acquisition is the
    one place `lease_epoch` and `attempt_count` advance, and the test is
    clearer for showing that.
    """
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET status = 'running', "
            "lease_owner = 'worker-1', lease_epoch = lease_epoch + 1, "
            "attempt_count = attempt_count + 1, started_at = now(), "
            "lease_expires_at = now() + interval '5 minutes' WHERE id = :id"
        ),
        {"id": attempt_id},
    )


async def _abandon(session, attempt_id, user_id) -> None:  # type: ignore[no-untyped-def]
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET status = 'abandoned', "
            "abandoned_at = now(), abandoned_by = :user WHERE id = :id"
        ),
        {"id": attempt_id, "user": user_id},
    )


async def _insert_attempt(  # type: ignore[no-untyped-def]
    session, base, approval_id, **overrides
) -> object:
    attempt_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO resume_revision_attempts ("
            "  id, application_id, source_resume_id, source_resume_version,"
            "  source_approval_id, instructions_snapshot, instructions_digest,"
            "  requested_by, replaces_attempt_id, replacement_ordinal"
            ") VALUES (:id, :app, :resume, 1, :approval, 'again', "
            "repeat('b', 64), :user, :replaces, :ordinal)"
        ),
        {
            "id": attempt_id,
            "app": base["application_id"],
            "resume": base["source_resume_id"],
            "approval": approval_id,
            "user": base["user_id"],
            "replaces": overrides.get("replaces"),
            "ordinal": overrides.get("ordinal", 0),
        },
    )
    return attempt_id


async def _second_approval(session, application_id) -> object:  # type: ignore[no-untyped-def]
    """A second approval of a different kind, so two attempts can coexist.

    `uq_one_pending_approval` is scoped to (application_id, kind), and
    `uq_revision_attempt_per_approval` to the approval - so a second live
    attempt needs a second approval, not a second application.
    """
    approval_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO approvals (id, application_id, kind, status, "
            "subject_snapshot, requested_at, created_at, updated_at) "
            "VALUES (:id, :app, 'resume', 'pending', '{}'::jsonb, "
            "now(), now(), now())"
        ),
        {"id": approval_id, "app": application_id},
    )
    return approval_id


# =====================================================================
# 1. The lifecycle table is the authority (J.15, C.1)
# =====================================================================
async def test_accepted_to_running_is_permitted(session, revision_attempt) -> None:
    await _to_running(session, revision_attempt["id"])
    assert await _status(session, revision_attempt["id"]) == "running"


async def test_accepted_cannot_jump_straight_to_succeeded(session, revision_attempt) -> None:
    """`accepted` may become running, failed or abandoned - nothing else.

    A jump to `succeeded` would mean an attempt that produced a result without
    ever holding a lease, which is exactly the shape a fenced-out worker would
    write if the fence were ever bypassed.
    """
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], status="succeeded")
    assert "not permitted" in str(exc.value).lower()
    assert await _status(session, revision_attempt["id"]) == "accepted"


async def test_running_to_abandoned_is_refused(session, revision_attempt) -> None:
    """Running work is never casually released; it must fail first.

    This is the transition whose absence keeps a live worker's output from
    being orphaned by a human clicking release at the wrong moment.
    """
    await _to_running(session, revision_attempt["id"])
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _abandon(session, revision_attempt["id"], revision_attempt["user_id"])
    assert "not permitted" in str(exc.value).lower()
    assert await _status(session, revision_attempt["id"]) == "running"


@pytest.mark.parametrize("target", ["accepted", "running"])
async def test_failed_is_never_resurrected(session, revision_attempt, target) -> None:
    """A failed attempt is never re-driven; replacement is a new row (C.4).

    Parametrised rather than looped so each case gets its own fixture and its
    own transaction - a loop would have to re-establish `failed` after every
    rejection, which is precisely the re-setup that savepoints make
    unnecessary and that a bare rollback made wrong.
    """
    await _to_running(session, revision_attempt["id"])
    await _set(session, revision_attempt["id"], status="failed", failure_class="lease_lost")
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], status=target)
    assert "not permitted" in str(exc.value).lower()
    assert await _status(session, revision_attempt["id"]) == "failed"


@pytest.mark.parametrize(
    ("column", "value"),
    [("failure_detail", "'anything'"), ("recovered_at", "now()"), ("status", "'failed'")],
)
async def test_a_succeeded_attempt_rejects_every_update(
    session, revision_attempt, demo_resume, column, value
) -> None:
    """Terminal and immutable, in every column.

    Asserted as "any update at all is refused" rather than "these columns are
    refused": an enumeration stops protecting whatever is added next.
    """
    await _to_running(session, revision_attempt["id"])
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET status = 'succeeded', "
            "result_resume_id = :resume, result_version = 2, "
            "outcome = 'passed', finished_at = now() WHERE id = :id"
        ),
        {"id": revision_attempt["id"], "resume": demo_resume},
    )
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set_raw(session, revision_attempt["id"], column, value)
    assert "immutable" in str(exc.value).lower()
    assert await _status(session, revision_attempt["id"]) == "succeeded"


# =====================================================================
# 2. Source bindings and retry history (A.1)
# =====================================================================
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_resume_version", "99"),
        ("instructions_snapshot", "'say something else'"),
        ("replacement_ordinal", "3"),
    ],
)
async def test_source_bindings_are_immutable(session, revision_attempt, column, value) -> None:
    """An attempt that could be re-pointed makes "one live attempt per
    approval" unenforceable, so the binding is frozen by trigger."""
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set_raw(session, revision_attempt["id"], column, value)
    assert "immutable" in str(exc.value).lower()


@pytest.mark.parametrize(
    ("column", "value"),
    [("evidence_stale_retry_count", "0"), ("evidence_stale_retried_at", "NULL")],
)
async def test_retry_counters_only_move_forward(session, revision_attempt, column, value) -> None:
    """Gate 1, as a database fact.

    `failure_class` may be rewritten - that is how a retryable class becomes
    terminal - but the columns recording how much budget has been spent cannot
    go backwards. Without this, an attempt that consumed its evidence_stale
    retry and later failed with model_timeout could be re-reported as
    evidence_stale and silently earn a second one.
    """
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET evidence_stale_retry_count = 1, "
            "evidence_stale_retried_at = now() WHERE id = :id"
        ),
        {"id": revision_attempt["id"]},
    )
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set_raw(session, revision_attempt["id"], column, value)
    message = str(exc.value).lower()
    assert "monotonic" in message or "never cleared" in message

    # The established state survived the rejection, which is the whole point
    # of the savepoint: a bare rollback would have discarded it.
    still = await session.execute(
        text("SELECT evidence_stale_retry_count FROM resume_revision_attempts " "WHERE id = :id"),
        {"id": revision_attempt["id"]},
    )
    assert still.scalar_one() == 1


async def test_attempt_count_never_decreases(session, revision_attempt) -> None:
    await _to_running(session, revision_attempt["id"])
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], attempt_count=0)
    assert "monotonic" in str(exc.value).lower()


# =====================================================================
# 3. CHECK constraints (A.3)
# =====================================================================
async def test_a_semantic_negative_can_never_be_a_failed_status(session, revision_attempt) -> None:
    """The single most important constraint in the table.

    `outcome` is about the content and only ever exists on a succeeded
    attempt, so "the verifier said no" can never be recorded as "the job
    broke". The two axes stay apart at the database level.
    """
    await _to_running(session, revision_attempt["id"])
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await session.execute(
                text(
                    "UPDATE resume_revision_attempts SET status = 'failed', "
                    "failure_class = 'model_timeout', "
                    "outcome = 'failed_verification' WHERE id = :id"
                ),
                {"id": revision_attempt["id"]},
            )
    assert "outcome_only_on_success" in str(exc.value)


async def test_abandonment_must_name_a_human(session, revision_attempt) -> None:
    """PO-3: a release is always attributable to a person."""
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], status="abandoned")
    assert "abandoned_named_a_human" in str(exc.value)


async def test_accepted_to_abandoned_requires_a_human(session, revision_attempt) -> None:
    """The "requested by mistake" path (C.1 row 10), done properly."""
    await session.execute(
        text(
            "UPDATE resume_revision_attempts SET status = 'abandoned', "
            "abandoned_at = now(), abandoned_by = :user, "
            "abandon_reason = 'requested by mistake' WHERE id = :id"
        ),
        {"id": revision_attempt["id"], "user": revision_attempt["user_id"]},
    )
    assert await _status(session, revision_attempt["id"]) == "abandoned"


async def test_running_requires_a_lease(session, revision_attempt) -> None:
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], status="running")
    assert "running_has_lease" in str(exc.value)


async def test_retry_counter_and_timestamp_must_agree(session, revision_attempt) -> None:
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], evidence_stale_retry_count=1)
    assert "evidence_stale_retry_coherent" in str(exc.value)


# =====================================================================
# 4. One live attempt per approval, and the version backstop
# =====================================================================
async def test_one_live_attempt_per_approval(session, revision_attempt) -> None:
    """THE rule the table exists to enforce."""
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _insert_attempt(session, revision_attempt, revision_attempt["source_approval_id"])
    assert "uq_revision_attempt_per_approval" in str(exc.value)


async def test_an_abandoned_attempt_frees_the_approval(session, revision_attempt) -> None:
    """The partial index is what makes replacement possible at all."""
    await _abandon(session, revision_attempt["id"], revision_attempt["user_id"])
    replacement = await _insert_attempt(
        session,
        revision_attempt,
        revision_attempt["source_approval_id"],
        replaces=revision_attempt["id"],
        ordinal=1,
    )
    assert await _status(session, replacement) == "accepted"


async def test_replaced_by_is_set_once_on_an_abandoned_row(session, revision_attempt) -> None:
    """The one exception to terminal immutability, and its limit.

    NULL -> A is the permitted write. A -> A changes nothing and is allowed
    through by `IS DISTINCT FROM`. A -> B is the case the trigger refuses, so
    proving "set once" needs two distinct replacements, and the second needs
    its own approval because the first still holds this one.
    """
    await _abandon(session, revision_attempt["id"], revision_attempt["user_id"])
    first = await _insert_attempt(
        session,
        revision_attempt,
        revision_attempt["source_approval_id"],
        replaces=revision_attempt["id"],
        ordinal=1,
    )
    other_approval = await _second_approval(session, revision_attempt["application_id"])
    second = await _insert_attempt(session, revision_attempt, other_approval)

    # NULL -> first: the one permitted write.
    await _set(session, revision_attempt["id"], replaced_by_attempt_id=first)

    # first -> second: refused. This is what "set once" means.
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await _set(session, revision_attempt["id"], replaced_by_attempt_id=second)
    assert "set once" in str(exc.value).lower()

    current = await session.execute(
        text("SELECT replaced_by_attempt_id FROM resume_revision_attempts " "WHERE id = :id"),
        {"id": revision_attempt["id"]},
    )
    assert current.scalar_one() == first


async def test_version_collision_backstop(
    session, demo_profile, demo_application, demo_resume
) -> None:
    """J.18. Two v2 tailored resumes for one application is a data error no
    amount of application-level care should be trusted to prevent."""
    await session.execute(
        text("UPDATE resumes SET version = 2 WHERE id = :id"), {"id": demo_resume}
    )
    with pytest.raises(DBAPIError) as exc:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO resumes (id, profile_id, application_id, kind, "
                    "version, content, created_at, updated_at) VALUES "
                    "(:id, :profile, :app, 'tailored', 2, "
                    "'{\"sections\": []}'::jsonb, now(), now())"
                ),
                {
                    "id": uuid7(),
                    "profile": demo_profile,
                    "app": demo_application["id"],
                },
            )
    assert "uq_resume_version_per_application" in str(exc.value)
