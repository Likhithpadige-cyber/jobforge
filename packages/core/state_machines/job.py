"""Job lifecycle state machine (ARCHITECTURE.md §7.1).

Transitions happen here and nowhere else. A status assignment scattered
through service code is how a state machine stops being one: the diagram in
the architecture stays accurate only if every move is validated against it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.models import JobStatus

if TYPE_CHECKING:  # `Job` is needed only for the annotation, and the
    # state machine should not drag the ORM in at runtime.
    from packages.core.models import Job


class IllegalTransitionError(ValueError):
    """Raised when a move is not in the documented lifecycle."""


#: The graph from ARCHITECTURE.md §7.1, transcribed exactly.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.DISCOVERED: frozenset(
        {JobStatus.NORMALIZED, JobStatus.DUPLICATE, JobStatus.DISCARDED, JobStatus.EXPIRED}
    ),
    JobStatus.NORMALIZED: frozenset(
        {JobStatus.EVALUATED, JobStatus.DISCARDED, JobStatus.DUPLICATE, JobStatus.EXPIRED}
    ),
    JobStatus.EVALUATED: frozenset(
        {
            JobStatus.SHORTLISTED,
            JobStatus.DISCARDED,
            JobStatus.EXPIRED,
            # Re-evaluation against a newer profile version is legitimate.
            JobStatus.EVALUATED,
        }
    ),
    JobStatus.SHORTLISTED: frozenset(
        {JobStatus.QUEUED, JobStatus.DISCARDED, JobStatus.EXPIRED, JobStatus.EVALUATED}
    ),
    JobStatus.QUEUED: frozenset({JobStatus.CLAIMED, JobStatus.DISCARDED, JobStatus.EXPIRED}),
    JobStatus.CLAIMED: frozenset({JobStatus.APPLIED, JobStatus.DISCARDED}),
    # `discarded` is terminal but ARCHITECTURE.md §7.1 notes "manual
    # re-shortlist allowed", so a human may bring it back.
    JobStatus.DISCARDED: frozenset({JobStatus.SHORTLISTED, JobStatus.EVALUATED}),
    JobStatus.APPLIED: frozenset(),
    JobStatus.DUPLICATE: frozenset(),
    JobStatus.EXPIRED: frozenset(),
}

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.APPLIED, JobStatus.DUPLICATE, JobStatus.EXPIRED}
)


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(job: Job, target: JobStatus, *, reason: str | None = None) -> JobStatus:
    """Move `job` to `target`, or raise.

    Returns the previous status so the caller can record the move. Does not
    flush or commit - the caller owns the transaction, so the status change
    and its outbox event land together.
    """
    current = job.status
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"job {job.id}: {current} -> {target} is not a documented transition "
            f"(allowed from {current}: "
            f"{sorted(str(s) for s in ALLOWED_TRANSITIONS.get(current, frozenset()))})"
        )
    job.status = target
    if reason is not None and target is JobStatus.DISCARDED:
        job.discard_reason = reason
    return current
