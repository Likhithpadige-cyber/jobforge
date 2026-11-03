"""The audit event allowlist, and the two ways past it.

The allowlist exists because of a failure mode this project has now met six
times: a check that was correct when written and silently stopped covering
what it claimed. A mistyped event type would produce a row no consumer queries
and no test notices - an audit record that is worse than none, because it
looks like coverage.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.core.db.audit_boundary import (
    ORCHESTRATION_EVENTS,
    AuditBoundary,
    AuditContext,
    AuditUnavailableError,
)

pytestmark = [pytest.mark.unit, pytest.mark.safety]


def context() -> AuditContext:
    return AuditContext(
        attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4(), lease_epoch=1
    )


#: Every outcome Batch 3A was asked to make durable, named here independently
#: of the implementation. If the two lists disagree, one of them is wrong and
#: this test says which.
REQUIRED = {
    "revision.attempt.failure_record_failed",
    "revision.attempt.fenced_out_before_failure",
    "revision.attempt.failure_not_recorded",
    "revision.attempt.success_not_recorded",
    "revision.attempt.recovery_failed",
    "revision.attempt.wait_timed_out",
    "revision.attempt.reconciled",
    "revision.attempt.reconcile_skipped",
}


def test_the_allowlist_is_exactly_the_required_set() -> None:
    """Both directions. Missing means a diagnostic silently went nowhere;
    extra means a type was added without being asked for, which is how an
    allowlist stops being one."""
    assert ORCHESTRATION_EVENTS == REQUIRED


def test_the_allowlist_is_a_subset_of_the_central_catalogue() -> None:
    """Two allowlists, one of which must contain the other.

    `KNOWN_EVENT_TYPES` is the contract for what may exist in `events` at all -
    a consumer reads it to know what it might be handed, and `EventEnvelope`
    refuses anything outside it. The boundary writes its rows directly rather
    than through the envelope (it has no domain session to emit into), so
    nothing forces the two into agreement at runtime; without this assertion
    the boundary could persist types the standard producer path would reject,
    and the drift would show up as an event no consumer subscribes to.

    Subset, not equality: the boundary is deliberately narrower than the
    catalogue, since it must never be able to write a domain event.
    """
    from packages.core.events import KNOWN_EVENT_TYPES

    assert ORCHESTRATION_EVENTS <= KNOWN_EVENT_TYPES
    assert "application.status.changed" not in ORCHESTRATION_EVENTS


def test_every_allowlisted_type_is_namespaced() -> None:
    """A consumer filters on this prefix to get "everything that went wrong
    with revision attempts". An unprefixed type would be invisible to it."""
    assert all(t.startswith("revision.attempt.") for t in ORCHESTRATION_EVENTS)


@pytest.mark.parametrize(
    "bogus",
    [
        "revision.attempt.typo",
        "revision.attempt.recovery_faild",  # one transposed letter
        "application.status.changed",  # a real type, but not ours
        "",
    ],
)
async def test_record_refuses_an_unallowlisted_type(bogus) -> None:
    with pytest.raises(ValueError, match="not an allowlisted audit event"):
        await AuditBoundary().record(bogus, context=context())


@pytest.mark.parametrize("bogus", ["revision.attempt.typo", "application.status.changed"])
async def test_try_record_also_refuses_an_unallowlisted_type(bogus) -> None:
    """`try_record` swallows *operational* failures, never programming errors.

    If it absorbed a bad type too, the allowlist would be decorative: a typo
    would return None and look exactly like an audit outage.
    """
    with pytest.raises(ValueError, match="not an allowlisted audit event"):
        await AuditBoundary().try_record(bogus, context=context())


async def test_record_raises_when_the_audit_engine_is_absent() -> None:
    """No engine is an audit outage, not a programming error, so it raises the
    outage type - which is what a caller gating model work catches."""
    for event_type in sorted(ORCHESTRATION_EVENTS):
        with pytest.raises(AuditUnavailableError):
            await AuditBoundary().record(event_type, context=context())


async def test_try_record_returns_none_when_the_audit_engine_is_absent() -> None:
    """The whole audit-outage policy in one assertion: a secondary audit
    failure must never replace the primary exception, so the failure paths
    that call this get None rather than a raise."""
    assert (
        await AuditBoundary().try_record("revision.attempt.recovery_failed", context=context())
        is None
    )


def test_context_carries_only_primitives() -> None:
    """Every call site is downstream of a rollback, which expires every ORM
    instance in the session. A non-primitive here would be a lazy refresh -
    `MissingGreenlet` in async - raised from inside the handler for the real
    error, destroying the diagnosis it was meant to record."""
    payload = context().as_payload()
    assert set(payload) == {"attempt_id", "application_id", "approval_id", "lease_epoch"}
    assert all(isinstance(v, str | int) for v in payload.values())


def test_a_caller_that_never_held_a_lease_records_no_epoch() -> None:
    """The wait timeout is reported by a caller that never acquired. `0` would
    be a plausible-looking lie in a column operators reason about."""
    bare = AuditContext(attempt_id=uuid4(), application_id=uuid4(), approval_id=uuid4())
    assert bare.lease_epoch is None
    assert bare.as_payload()["lease_epoch"] is None


def test_detail_cannot_overwrite_which_attempt_a_diagnostic_is_about() -> None:
    ctx = context()
    payload = ctx.as_payload()
    assert payload["attempt_id"] == str(ctx.attempt_id)
    # `detail` is nested under its own key by `record`, so a caller passing
    # {"attempt_id": ...} cannot re-point the row at a different attempt.
    assert "detail" not in payload
