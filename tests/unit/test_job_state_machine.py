"""Job lifecycle transitions (ARCHITECTURE.md §7.1)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from packages.core.models import JobStatus
from packages.core.state_machines.job import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    IllegalTransitionError,
    can_transition,
    transition,
)

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@dataclass
class FakeJob:
    id: object
    status: JobStatus
    discard_reason: str | None = None


def job(status: JobStatus) -> FakeJob:
    return FakeJob(id=uuid4(), status=status)


def test_every_status_has_an_entry() -> None:
    """A status missing from the map is unreachable and un-exitable."""
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_terminal_statuses_have_no_exits() -> None:
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset(), status


def test_documented_happy_path() -> None:
    current = JobStatus.DISCOVERED
    for nxt in (
        JobStatus.NORMALIZED, JobStatus.EVALUATED, JobStatus.SHORTLISTED,
        JobStatus.QUEUED, JobStatus.CLAIMED, JobStatus.APPLIED,
    ):
        assert can_transition(current, nxt), f"{current} -> {nxt}"
        current = nxt


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.DISCOVERED, JobStatus.SHORTLISTED),   # skips evaluation
        (JobStatus.NORMALIZED, JobStatus.APPLIED),       # skips everything
        (JobStatus.APPLIED, JobStatus.EVALUATED),        # terminal
        (JobStatus.DUPLICATE, JobStatus.NORMALIZED),     # terminal
        (JobStatus.EXPIRED, JobStatus.SHORTLISTED),      # terminal
        (JobStatus.EVALUATED, JobStatus.CLAIMED),        # skips the queue
    ],
)
def test_illegal_transitions_are_rejected(current: JobStatus, target: JobStatus) -> None:
    assert can_transition(current, target) is False
    with pytest.raises(IllegalTransitionError):
        transition(job(current), target)


def test_transition_returns_the_previous_status() -> None:
    entity = job(JobStatus.NORMALIZED)
    previous = transition(entity, JobStatus.EVALUATED)
    assert previous is JobStatus.NORMALIZED
    assert entity.status is JobStatus.EVALUATED


def test_discard_records_a_reason() -> None:
    entity = job(JobStatus.EVALUATED)
    transition(entity, JobStatus.DISCARDED, reason="below the review floor")
    assert entity.discard_reason == "below the review floor"


def test_reason_is_only_stored_on_discard() -> None:
    entity = job(JobStatus.EVALUATED)
    transition(entity, JobStatus.SHORTLISTED, reason="ignored")
    assert entity.discard_reason is None


def test_discarded_can_be_re_shortlisted_by_a_human() -> None:
    """ARCHITECTURE.md §7.1 notes discarded allows a manual re-shortlist."""
    assert can_transition(JobStatus.DISCARDED, JobStatus.SHORTLISTED)


def test_re_evaluation_is_allowed() -> None:
    """A newer profile version legitimately re-evaluates an evaluated job."""
    assert can_transition(JobStatus.EVALUATED, JobStatus.EVALUATED)
