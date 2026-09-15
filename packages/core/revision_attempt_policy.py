"""Retry policy for durable revision attempts (C3_HARDENING_DESIGN.md C.1.1).

This module is deliberately free of SQLAlchemy, HTTP and I/O. It holds the
answer to one question - *may this attempt be retried?* - in one place, so the
Python guard and the SQL `WHERE` clause that enforces it cannot drift into
disagreeing. `retryable_sql()` below is generated from the same constants the
Python predicate reads; a test compares the two over the full cross product.

**Why it lives at `packages.core`, not under `services` or `repositories`.**
Both layers need it: the repository binds `retryable_sql()` into D.6's single
statement, and the service reads `can_retry()` to decide what to log and
whether to recover. A repository importing a service inverts the intended
dependency direction, and in this package it is not merely untidy - it closes
a genuine import cycle, because `services/__init__` pulls in `profile_service`
which pulls in `repositories`. Policy is neither layer, so it sits below both
and is imported downward by each. That also keeps the J.28 guarantee intact:
one source, two renderings, no duplicated sets.

Two sets, named here and nowhere else:

``RELEASABLE``
    every technical failure class. A human may release one of these and
    replace the attempt. There is no semantic member and there cannot be one:
    a semantic negative never reaches `failed` at all (see `outcome` below).
``RETRYABLE``
    the subset a worker may re-drive by itself. ``RETRYABLE < RELEASABLE``:
    `model_output_invalid`, `budget_exceeded`, `state_conflict`, `lease_lost`
    and `worker_lost` are releasable-but-terminal - released by a human, never
    re-driven automatically, because retrying them without a human looking
    would just burn the budget again on the same input.

**The distinction this module exists to protect.** `status` is about the JOB;
`outcome` is about the CONTENT. A B5 verdict of "unsupported" means the
pipeline did exactly what it should: the attempt SUCCEEDED and carries
`outcome='failed_verification'`. It is never a `failed` attempt, never
retryable, and never recoverable - there is nothing to retry, because nothing
went wrong. `ck_resume_revision_attempts_outcome_only_on_success` makes that a
database fact; this module makes it a code fact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from packages.core.models import RevisionFailureClass

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

#: Every technical class. Releasable by an authenticated human (PO-3).
RELEASABLE: frozenset[RevisionFailureClass] = frozenset(RevisionFailureClass)

#: The subset a worker may re-drive without a human.
RETRYABLE: frozenset[RevisionFailureClass] = frozenset(
    {
        RevisionFailureClass.MODEL_TIMEOUT,
        RevisionFailureClass.PERSISTENCE_FAILED,
        RevisionFailureClass.EVIDENCE_LOCKED,
        RevisionFailureClass.EVIDENCE_STALE,
    }
)

#: `evidence_stale` alone carries a per-attempt budget. Evidence that moved
#: once may well have moved for a reason; retrying for ever would mean a
#: revision racing a human editor indefinitely.
BUDGETED: frozenset[RevisionFailureClass] = frozenset(
    {RevisionFailureClass.EVIDENCE_STALE}
)


class AttemptView(Protocol):
    """The attempt fields the predicate reads, and nothing else.

    A Protocol rather than the ORM class so the truth table can be exercised
    over plain objects, with no database and no session.
    """

    attempt_count: int
    evidence_stale_retry_count: int


def can_retry(
    attempt: AttemptView,
    failure_class: RevisionFailureClass,
    *,
    max_attempts: int,
    evidence_stale_budget: int,
) -> bool:
    """The one retry predicate. Referenced, never restated.

    Three conjuncts, in the order they become false most often:

    1. the class is one a worker may re-drive at all;
    2. the attempt has acquisitions left;
    3. for a budgeted class, that budget is not yet spent.

    `max_attempts` and `evidence_stale_budget` are passed in rather than read
    from settings here, because a predicate that reaches for configuration is
    a predicate that cannot be tested at every value of it.
    """
    if failure_class not in RETRYABLE:
        return False
    if attempt.attempt_count >= max_attempts:
        return False
    if failure_class in BUDGETED:
        return attempt.evidence_stale_retry_count < evidence_stale_budget
    return True


def retryable_sql(
    *, failure_class_param: str = "failure_class",
    max_attempts_param: str = "max_attempts",
    budget_param: str = "evidence_stale_budget",
) -> str:
    """The same predicate as a SQL `WHERE` fragment, generated from the sets.

    The repository binds this into D.6's single statement, so the decision and
    the transition it authorises are atomic - there is no window in which a
    second worker can read "retryable" and act on it after the budget was
    spent. Because both forms come from `RETRYABLE` and `BUDGETED`, adding a
    class to either set updates both at once.
    """
    retryable = _sql_list(RETRYABLE)
    budgeted = _sql_list(BUDGETED)
    return (
        f"(:{failure_class_param} = ANY(ARRAY[{retryable}]::revision_failure_class[])"
        f" AND attempt_count < :{max_attempts_param}"
        f" AND (:{failure_class_param} <> ALL(ARRAY[{budgeted}]::revision_failure_class[])"
        f"      OR evidence_stale_retry_count < :{budget_param}))"
    )


def _sql_list(classes: Iterable[RevisionFailureClass]) -> str:
    return ", ".join(f"'{member.value}'" for member in sorted(classes))
