"""Application lifecycle state machine (ARCHITECTURE.md §7.2).

**Deliberately partial.** The full lifecycle diagram in ARCHITECTURE.md covers
rework, approval, submission and error paths, but only the initial
resume-generation path is unambiguous today. Guessing the rest would bake a
wrong graph into the one place the system trusts to be right, so every edge
that is not yet settled is absent - and absent means rejected, not allowed.

    created            -> resume_generating
    resume_generating  -> resume_ready
    resume_generating  -> verification_failed
    resume_ready       -> awaiting_approval      (temporary bridge, see below)
    awaiting_approval  -> approved
    awaiting_approval  -> changes_requested
    changes_requested  -> resume_generating       (rework, see below)

Every other pair fails closed, including self transitions. Adding an edge is a
deliberate act: write it here, with the documentation that justifies it.

**The `resume_ready -> awaiting_approval` edge is a documented temporary
bridge.** ARCHITECTURE.md §7.2 routes review through `questions_pending`
(`resume_ready -> questions_pending -> awaiting_approval`), and that remains
the intended long-term path. `questions_pending` is owned by the
question-extraction phase (A2), which is not implemented, so today there is no
way to reach human review at all. Rather than build an unrelated phase to
unblock review, C2 bridges directly and this comment records the debt: when
question extraction lands, route through `questions_pending` and revisit this
edge. `questions_pending` deliberately keeps its place in the graph and its
empty exit set, so nothing here pretends the intended path has gone away.

**The `changes_requested -> resume_generating` edge is C3's rework path.**
It is the single edge regeneration needs, and it deliberately lands in
`resume_generating` rather than anywhere later: a revised resume is a new
generation attempt and must earn `resume_ready` by passing the same
verification the first attempt did. Routing it into `resume_generating` means
B6 - which already refuses to run outside that status - is the only way back
out, through `resume_ready` or `verification_failed` exactly as before.

Note what was *not* added, in either phase. There is no `resume_ready ->
approved` and no `resume_ready -> changes_requested`: a decision is only
reachable from `awaiting_approval`, so "entered review" stays a distinct,
recorded step. And there is no `changes_requested -> resume_ready`,
`-> awaiting_approval` or `-> approved`: a rework that could reach review or
approval without re-entering generation would be a resume whose text changed
after the verdicts that vouched for it.

Reaching `approved` in the graph is necessary but not sufficient: the database
independently refuses the write unless a real `approvals` row decided by a
human exists (trigger `jobforge_require_real_approval`, plus
`ck_applications_approved_requires_human`). This module never tries to satisfy
those - a caller without a genuine human decision fails at flush, which is the
intended behaviour.

Unlike the job machine, a move here is not just a field assignment. Three
things happen together or not at all: the status changes, an append-only
`application_status_history` row records who moved it and why, and
`application.status.changed` goes to the outbox (EVENTS.md §3.3 - emitted for
*every* transition without exception, which is what makes the timeline view
complete). All three land in the caller's transaction; this module never
commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events import emit
from packages.core.models import ApplicationStatus, ApplicationStatusHistory
from packages.core.state_machines.job import IllegalTransitionError
from packages.shared.ids import uuid7

if TYPE_CHECKING:  # `Application` is needed only for the annotation; the
    # state machine should not drag the ORM in at runtime.
    from packages.core.events.envelope import ActorType
    from packages.core.models import Application


#: Only the transitions the review settled. Statuses are listed exhaustively
#: with an empty frozenset rather than left out, so a missing key is a bug in
#: this table rather than a silently unreachable state.
ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.CREATED: frozenset({ApplicationStatus.RESUME_GENERATING}),
    ApplicationStatus.RESUME_GENERATING: frozenset(
        {ApplicationStatus.RESUME_READY, ApplicationStatus.VERIFICATION_FAILED}
    ),
    # Temporary bridge while question extraction (A2) is unimplemented; the
    # documented route is resume_ready -> questions_pending -> awaiting_approval.
    ApplicationStatus.RESUME_READY: frozenset({ApplicationStatus.AWAITING_APPROVAL}),
    ApplicationStatus.VERIFICATION_FAILED: frozenset(),
    # Intentionally still closed: the long-term entry point to review, kept in
    # the graph so the intended path is visible rather than forgotten.
    ApplicationStatus.QUESTIONS_PENDING: frozenset(),
    ApplicationStatus.AWAITING_APPROVAL: frozenset(
        {ApplicationStatus.APPROVED, ApplicationStatus.CHANGES_REQUESTED}
    ),
    # Rework (C3). The only exit, and it goes back to the start of
    # generation so the revision is verified like any other attempt.
    ApplicationStatus.CHANGES_REQUESTED: frozenset(
        {ApplicationStatus.RESUME_GENERATING}
    ),
    ApplicationStatus.APPROVED: frozenset(),
    ApplicationStatus.SUBMISSION_READY: frozenset(),
    ApplicationStatus.SUBMITTING: frozenset(),
    ApplicationStatus.SUBMITTED: frozenset(),
    ApplicationStatus.SUBMISSION_FAILED: frozenset(),
    ApplicationStatus.MANUAL_HANDOFF: frozenset(),
    ApplicationStatus.POLICY_BLOCKED: frozenset(),
    ApplicationStatus.ABANDONED: frozenset(),
    ApplicationStatus.ERROR: frozenset(),
}


def can_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


async def transition_application(
    session: AsyncSession,
    application: Application,
    target: ApplicationStatus,
    *,
    actor_type: ActorType = "agent",
    actor_id: str | None = None,
    reason: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    trace_id: str | None = None,
) -> ApplicationStatus:
    """Move `application` to `target`, recording history and the event.

    Returns the previous status. Raises `IllegalTransitionError` *before* any
    mutation, so a rejected move leaves no status change, no history row and
    no event behind - the caller's transaction is untouched.

    Does not commit. The status, the history row and the outbox event share
    the caller's transaction on purpose: a committed status change with a lost
    event is an application that silently stops moving and cannot explain why.
    """
    current = application.status
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"application {application.id}: {current} -> {target} is not a "
            f"documented transition (allowed from {current}: "
            f"{sorted(str(s) for s in ALLOWED_TRANSITIONS.get(current, frozenset()))})"
        )

    application.status = target
    session.add(
        ApplicationStatusHistory(
            id=uuid7(),
            application_id=application.id,
            from_status=current,
            to_status=target,
            actor_type=actor_type,
            actor_id=actor_id,
            # `event_id` is left unset: the schema allows it but no document
            # defines which event it should point at. Inventing a meaning for
            # an audit column is worse than leaving it null.
            reason=reason,
        )
    )
    await session.flush()

    payload: dict[str, object] = {
        "application_id": str(application.id),
        "from_status": str(current),
        "to_status": str(target),
        "actor_type": actor_type,
        "actor_id": actor_id,
    }
    if reason is not None:
        payload["reason"] = reason

    await emit(
        session,
        type="application.status.changed",
        aggregate_type="application",
        aggregate_id=application.id,
        correlation_id=correlation_id or application.id,
        causation_id=causation_id,
        trace_id=trace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload,
    )
    return current
