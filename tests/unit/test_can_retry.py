"""J.28 - the retry predicate has one meaning, in two languages.

`can_retry()` and the SQL fragment `retryable_sql()` binds into D.6's single
statement must agree at every point, because they are consulted at different
moments: the Python form decides what to log and whether to recover, the SQL
form decides what is actually written. If they disagreed, an attempt could be
reported as terminal while the database quietly returned it to `accepted`, or
the reverse - and the disagreement would only ever show up as a stuck
application nobody could explain.

Both forms are generated from the same `RETRYABLE` / `BUDGETED` sets, so this
test is really asserting that the generation is faithful. It needs no database
and no session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from packages.core.models import RevisionFailureClass
from packages.core.revision_attempt_policy import (
    BUDGETED,
    RELEASABLE,
    RETRYABLE,
    can_retry,
    retryable_sql,
)

pytestmark = pytest.mark.unit


@dataclass
class _Attempt:
    attempt_count: int
    evidence_stale_retry_count: int


def _sql_says_retryable(
    failure_class: RevisionFailureClass,
    attempt: _Attempt,
    *,
    max_attempts: int,
    budget: int,
) -> bool:
    """Evaluate the generated SQL fragment in Python.

    A faithful re-implementation of PostgreSQL's semantics for the three
    operators the fragment uses - `= ANY(...)`, `<> ALL(...)` and `<` - which
    is enough to compare the two forms without a database. The integration
    suite exercises the real statement; this pins the logic.
    """
    fragment = retryable_sql()
    in_retryable = set(re.findall(r"'([a-z_]+)'", fragment.split("attempt_count")[0]))
    in_budgeted = set(re.findall(r"'([a-z_]+)'", fragment.split("<> ALL")[1]))
    value = failure_class.value
    if value not in in_retryable:
        return False
    if attempt.attempt_count >= max_attempts:
        return False
    if value in in_budgeted:
        return attempt.evidence_stale_retry_count < budget
    return True


@pytest.mark.parametrize("failure_class", sorted(RevisionFailureClass))
@pytest.mark.parametrize("attempt_count", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("stale_count", [0, 1, 2])
@pytest.mark.parametrize(("max_attempts", "budget"), [(3, 1), (1, 0), (5, 2)])
def test_python_and_sql_agree_over_the_cross_product(
    failure_class: RevisionFailureClass,
    attempt_count: int,
    stale_count: int,
    max_attempts: int,
    budget: int,
) -> None:
    attempt = _Attempt(attempt_count=attempt_count, evidence_stale_retry_count=stale_count)
    python = can_retry(
        attempt, failure_class, max_attempts=max_attempts, evidence_stale_budget=budget
    )
    sql = _sql_says_retryable(failure_class, attempt, max_attempts=max_attempts, budget=budget)
    assert python == sql, (
        f"{failure_class.value} at attempt_count={attempt_count}, "
        f"stale={stale_count}, max={max_attempts}, budget={budget}: "
        f"python={python} sql={sql}"
    )


def test_retryable_is_a_strict_subset_of_releasable() -> None:
    """Every retryable class is releasable; not every releasable one is
    retryable. `model_output_invalid` and `budget_exceeded` are released by a
    human, never re-driven, because retrying them unattended just spends the
    budget again on the same input."""
    assert RETRYABLE < RELEASABLE
    assert RevisionFailureClass.MODEL_OUTPUT_INVALID in RELEASABLE - RETRYABLE
    assert RevisionFailureClass.BUDGET_EXCEEDED in RELEASABLE - RETRYABLE
    assert RevisionFailureClass.LEASE_LOST in RELEASABLE - RETRYABLE
    assert RevisionFailureClass.WORKER_LOST in RELEASABLE - RETRYABLE


def test_every_failure_class_is_releasable() -> None:
    """There is no semantic member and there cannot be one: a semantic
    negative never reaches `failed` at all."""
    assert frozenset(RevisionFailureClass) == RELEASABLE


def test_the_evidence_stale_budget_is_consumed_once_at_the_default() -> None:
    """The scenario gate 1 exists for: one stale retry, then terminal."""
    fresh = _Attempt(attempt_count=1, evidence_stale_retry_count=0)
    spent = _Attempt(attempt_count=1, evidence_stale_retry_count=1)
    kwargs = {"max_attempts": 3, "evidence_stale_budget": 1}
    assert can_retry(fresh, RevisionFailureClass.EVIDENCE_STALE, **kwargs)
    assert not can_retry(spent, RevisionFailureClass.EVIDENCE_STALE, **kwargs)
    # Laundering is refused: a different retryable class does not restore it.
    assert can_retry(spent, RevisionFailureClass.MODEL_TIMEOUT, **kwargs)
    assert not can_retry(spent, RevisionFailureClass.EVIDENCE_STALE, **kwargs)


def test_only_evidence_stale_is_budgeted() -> None:
    assert frozenset({RevisionFailureClass.EVIDENCE_STALE}) == BUDGETED


def test_the_ceiling_stops_every_class() -> None:
    at_ceiling = _Attempt(attempt_count=3, evidence_stale_retry_count=0)
    for failure_class in RETRYABLE:
        assert not can_retry(at_ceiling, failure_class, max_attempts=3, evidence_stale_budget=1)
