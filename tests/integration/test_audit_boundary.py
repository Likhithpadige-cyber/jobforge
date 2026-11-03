"""The audit boundary against real PostgreSQL: does the evidence outlive T3?

One property justifies this whole slice, and it is not "we write some events".
It is that an audit row survives the rollback of the transaction it describes.
Batch 2's diagnostics were `log.*` calls precisely because `emit()` could not
do this - an outbox row written into the caller's session dies with the
caller's session, which is correct for a domain event and exactly wrong for a
diagnostic that is usually *about* a rollback.

So the tests below are mostly one test asked in different ways: write, roll the
domain back, and look again from somewhere else.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.core.db.audit_boundary import (
    ORCHESTRATION_EVENTS,
    AuditBoundary,
    AuditContext,
    AuditUnavailableError,
)
from packages.core.db.audit_session import (
    audit_engine_ready,
    dispose_audit_engine,
    init_audit_engine,
)
from packages.shared.config import Settings
from tests.integration.conftest import _url

pytestmark = [pytest.mark.integration, pytest.mark.safety]


def _settings(role: str | None = None) -> Settings:
    """Settings whose audit DSN points at the test database.

    `role=None` exercises the documented fallback (own engine, own
    transactions, domain DSN); `role="app_audit"` exercises the provisioned
    shape. Both must satisfy the durability property - that is the point of
    allowing the fallback at all.
    """
    return Settings(audit_database_url=_url(role))


def _context() -> AuditContext:
    return AuditContext(
        attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4(), lease_epoch=3
    )


async def _read_event(event_id):  # type: ignore[no-untyped-def]
    """Read the row back on a connection of its own.

    Deliberately not the domain session and not the audit session: a row that
    is only visible from the connection that wrote it has not been committed,
    and checking from either participant could not tell the difference.
    """
    eng = create_async_engine(_url(), poolclass=None, future=True)
    try:
        async with eng.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT type, aggregate_type, aggregate_id, correlation_id, "
                        "       actor_type, actor_id, payload, published "
                        "  FROM events WHERE id = :id"
                    ),
                    {"id": event_id},
                )
            ).one_or_none()
    finally:
        await eng.dispose()


# =====================================================================
# The property the slice exists for
# =====================================================================
async def test_an_audit_write_survives_a_domain_rollback(
    audit_boundary_engine, session, demo_application
) -> None:
    """The one that matters, and it needs BOTH halves.

    Rolling back a session that wrote nothing proves nothing: the audit row
    would survive a no-op just as happily, and this test would pass against a
    boundary that shared the domain connection. So the domain writes a row
    first, and the assertions come as a pair - the domain row must be GONE and
    the audit row must remain. Only the pair separates "independent
    transaction" from "nothing happened".
    """
    ctx = _context()
    doomed_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO events (id, type, aggregate_type, aggregate_id, "
            "                    correlation_id, actor_type, payload) "
            "VALUES (:id, 'application.status.changed', 'application', :agg, "
            "        :agg, 'system', '{}'::jsonb)"
        ),
        {"id": doomed_id, "agg": demo_application["id"]},
    )
    await session.flush()

    event_id = await AuditBoundary().record(
        "revision.attempt.recovery_failed", context=ctx, detail={"cause": "Simulated"}
    )

    await session.rollback()

    assert await _read_event(doomed_id) is None, (
        "the domain write survived the rollback, so this test cannot tell an "
        "independent audit transaction from a shared one"
    )
    row = await _read_event(event_id)
    assert row is not None, "the audit row did not survive the domain rollback"
    assert row.type == "revision.attempt.recovery_failed"
    assert row.aggregate_type == "application"
    assert row.aggregate_id == ctx.application_id
    assert row.correlation_id == ctx.application_id
    assert row.actor_type == "system"
    assert row.payload["attempt_id"] == str(ctx.attempt_id)
    assert row.payload["lease_epoch"] == 3
    assert row.payload["detail"] == {"cause": "Simulated"}
    # app_audit holds no UPDATE on events: publishing stays the relay's job.
    assert row.published is False


async def test_the_boundary_holds_no_domain_session(audit_boundary_engine, session) -> None:
    """Structural, not behavioural: no method takes a session at all.

    A boundary that accepted one could be handed the domain session by a
    caller trying to be helpful, and the durability property would evaporate
    with no test failing.
    """
    for name in ("record", "try_record"):
        params = set(inspect.signature(getattr(AuditBoundary, name)).parameters)
        assert "session" not in params, f"{name} accepts a session"
        assert params == {"self", "event_type", "context", "detail"}


async def test_every_allowlisted_type_actually_inserts(audit_boundary_engine) -> None:
    """The allowlist is checked in a unit test; this proves each entry is a
    row PostgreSQL will accept - `events` has CHECK constraints on
    `actor_type` and `aggregate_type` that a wrong constant would trip."""
    for event_type in sorted(ORCHESTRATION_EVENTS):
        event_id = await AuditBoundary().record(event_type, context=_context())
        row = await _read_event(event_id)
        assert row is not None and row.type == event_type


async def test_two_diagnostics_about_one_attempt_are_both_kept(audit_boundary_engine) -> None:
    """Insert-only, never upsert. A fenced-out failure followed by a recovery
    failure is two facts about one attempt, and collapsing them would lose the
    order events happened in."""
    ctx = _context()
    first = await AuditBoundary().record("revision.attempt.fenced_out_before_failure", context=ctx)
    second = await AuditBoundary().record("revision.attempt.recovery_failed", context=ctx)
    assert first != second
    assert await _read_event(first) is not None
    assert await _read_event(second) is not None


# =====================================================================
# Audit outage: the secondary failure must never become the primary one
# =====================================================================
async def test_try_record_survives_the_audit_database_being_gone() -> None:
    """No engine at all - the state of a process whose audit init failed."""
    await dispose_audit_engine()
    assert not audit_engine_ready()
    assert (
        await AuditBoundary().try_record(
            "revision.attempt.failure_record_failed", context=_context()
        )
        is None
    )


async def test_a_dead_audit_engine_does_not_replace_the_original_error() -> None:
    """The audit-outage rule, exercised in the shape the service uses it.

    The original exception must be what propagates. If `try_record` raised,
    the caller's `except` block would finish with a bookkeeping error and the
    real cause would be gone - which is the failure this project already hit
    once, when an expired ORM read inside a handler replaced a
    `DependencyUnavailableError` with a `MissingGreenlet`.
    """
    await dispose_audit_engine()
    original = RuntimeError("the real failure")

    async def failing_path() -> None:
        try:
            raise original
        except RuntimeError:
            await AuditBoundary().try_record("revision.attempt.recovery_failed", context=_context())
            raise

    with pytest.raises(RuntimeError) as caught:
        await failing_path()
    assert caught.value is original


async def test_record_raises_a_distinguishable_outage_type() -> None:
    """`record()` is for callers that must not proceed without evidence - the
    audit-outage policy's "do not start new model work". They need an
    exception type they can catch without also catching a programming error,
    which is why the allowlist raises `ValueError` and this raises
    `AuditUnavailableError`."""
    await dispose_audit_engine()
    with pytest.raises(AuditUnavailableError):
        await AuditBoundary().record("revision.attempt.wait_timed_out", context=_context())


# =====================================================================
# The provisioned shape, when the role exists
# =====================================================================
async def test_the_boundary_works_as_the_least_privileged_role(
    audit_engine,
) -> None:
    """The real write, on the real role, with nothing catching it.

    Role availability is `audit_engine`'s job - Batch 1's fixture, which
    already refuses to fall back to a privileged connection and honours
    `REQUIRE_SECURITY_TESTS` through `_skip_or_fail`. Everything below is the
    implementation under test:
    an insert the grants refuse, an allowlist mistake or a payload the CHECK
    constraints reject must FAIL here. An earlier version wrapped this call in
    the same `except` that established availability, which would have filed a
    permission regression as "role not provisioned" - a green run proving
    nothing, which is the exact failure `_skip_or_fail` exists to prevent.
    """
    settings = _settings("app_audit")
    await dispose_audit_engine()
    try:
        await init_audit_engine(settings)
        event_id = await AuditBoundary().record("revision.attempt.reconciled", context=_context())
        assert settings.audit_dsn_is_separate
        assert await _read_event(event_id) is not None
    finally:
        await dispose_audit_engine()


async def test_the_fallback_is_reported_rather_than_hidden() -> None:
    """With no AUDIT_DATABASE_URL the boundary still runs, but nothing
    pretends privilege separation is in force."""
    fallback = Settings()
    assert fallback.audit_database_url is None
    assert fallback.audit_dsn_is_separate is False
    assert fallback.audit_dsn == str(fallback.database_url)
    assert "audit_database_url" in fallback.safe_dump()
    assert fallback.safe_dump()["audit_database_url"] == "***unset***"


def test_the_audit_dsn_is_never_none() -> None:
    """The boundary has no "not configured" branch to get wrong: there is
    always a DSN, and `audit_dsn_is_separate` carries the distinction."""
    assert Settings().audit_dsn
    assert Settings(audit_database_url=_url("app_audit")).audit_dsn.startswith(
        "postgresql+asyncpg://app_audit"
    )
