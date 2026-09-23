"""Application lifecycle state machine (ARCHITECTURE.md §7.2).

**Deliberately partial.** The full lifecycle diagram in ARCHITECTURE.md covers
rework, approval, submission and error paths, but only the resume-generation
and question-extraction path is unambiguous today. Guessing the rest would
bake a wrong graph into the one place the system trusts to be right, so every
edge that is not yet settled is absent - and absent means rejected, not
allowed.

    created            -> resume_generating
    resume_generating  -> resume_ready
    resume_generating  -> verification_failed
    resume_generating  -> changes_requested      (C3 recovery, see below)
    resume_ready       -> questions_pending       (M9, see below)
    questions_pending  -> awaiting_approval       (M9, see below)
    awaiting_approval  -> approved
    awaiting_approval  -> changes_requested
    changes_requested  -> resume_generating       (rework, see below)

Every other pair fails closed, including self transitions. Adding an edge is a
deliberate act: write it here, with the documentation that justifies it.

**The `resume_ready -> questions_pending -> awaiting_approval` path is M9's.**
It replaces the `resume_ready -> awaiting_approval` bridge C2 used while
question extraction (A2) did not exist. The first edge is
`packages.core.services.applications.prepare_application`'s: it extracts,
classifies, grounds and drafts, persists the `application_questions` /
`application_answers` rows, and moves the application into
`questions_pending` - always, even when extraction produced zero questions,
because the state records that extraction happened, not how many results it
found. The second edge remains `resume_review.enter_review`'s, unchanged
except its precondition (`questions_pending` rather than `resume_ready`): a
human still explicitly opens review through the one existing endpoint
(`POST /applications/{id}/resume-review`), which still only ever creates the
`resume` approval. The `answers` approval this package may also need is a
separate, independently created-and-decided `ApprovalKind.ANSWERS` record
(`packages.core.services.applications.approve_answers` /
`request_changes_answers`) - it needs no "open" step of its own, because
unlike a resume decision it triggers no regeneration pipeline that would
need a pending row to wait against.

**The `changes_requested -> resume_generating` edge is C3's rework path.**
It is the single edge regeneration needs, and it deliberately lands in
`resume_generating` rather than anywhere later: a revised resume is a new
generation attempt and must earn `resume_ready` by passing the same
verification the first attempt did. Routing it into `resume_generating` means
B6 - which already refuses to run outside that status - is the only way back
out, through `resume_ready` or `verification_failed` exactly as before.

**The `resume_generating -> changes_requested` edge is C3's recovery path
(PO-1).** It exists because a durable revision attempt can fail *after* the
application has entered generation, and an application with no worker on it
must not be left in `resume_generating`. Before durable attempts, a failed
pipeline rolled the whole request back, so the application had never left
`changes_requested` and no edge was needed; that rollback was what PO-4
removed, because it also erased the evidence that an attempt had happened at
all. The transition is therefore the *explicit* form of what used to happen by
the absence of a commit, and it is recorded in `application_status_history`
like every other move.

Only a technical failure takes this edge. A semantic negative - B5 finding a
claim unsupported - is not a failure of the attempt: it produces a real v2 and
ends at `verification_failed` by B6's own route, which is why that edge is
untouched. And the edge does not let a rework skip verification: the only way
back out of `changes_requested` remains `-> resume_generating`, so a recovered
application must re-acquire its attempt and generate again.

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

**The `approved -> submission_ready -> {manual_handoff | policy_blocked}`
edges are M10 Phase 2's.** Both are driven by one deterministic worker step,
`packages.core.services.submission_readiness.check_readiness_on_approval`,
reacting to `approval.granted` on the existing `cg.application` consumer
(no new worker kind). `approved -> submission_ready` fires only once the
application's *current* resume, cover letter, and - if any questions were
prepared - answers package each carry a human `APPROVED` decision bound to
that exact current version (never a stale one; see that module's snapshot
checks). The application always reaches `submission_ready` first, regardless
of what `evaluate_submission` decides, because "ready" describes packet
completeness, not policy permission. The same step then immediately
evaluates policy and takes the second edge only for `BLOCKED`
(`-> policy_blocked`) or `REQUIRES_HUMAN` (`-> manual_handoff`); an `ALLOWED`
verdict leaves the application at `submission_ready` - M10 implements no
submission dispatcher for any channel, so "policy permits it" is not treated
as "a human should act now."

Note what was deliberately not added. `submission_ready -> submitting`,
`submitting -> submitted`, `submitting -> submission_failed`, and any
retry/abandon path stay absent - M11's, not guessed at here, exactly as this
module's opening paragraph says every unsettled edge should.

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
from packages.shared.clock import utc_now
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
        {
            ApplicationStatus.RESUME_READY,
            ApplicationStatus.VERIFICATION_FAILED,
            # C3 PO-1 recovery. See the module docstring.
            ApplicationStatus.CHANGES_REQUESTED,
        }
    ),
    # M9. Extraction/classification/drafting only; no human input yet.
    ApplicationStatus.RESUME_READY: frozenset({ApplicationStatus.QUESTIONS_PENDING}),
    ApplicationStatus.VERIFICATION_FAILED: frozenset(),
    # M9. Left by the same worker execution that entered it, once questions
    # (zero or more) have been extracted and drafted.
    ApplicationStatus.QUESTIONS_PENDING: frozenset({ApplicationStatus.AWAITING_APPROVAL}),
    ApplicationStatus.AWAITING_APPROVAL: frozenset(
        {ApplicationStatus.APPROVED, ApplicationStatus.CHANGES_REQUESTED}
    ),
    # Rework (C3). The only exit, and it goes back to the start of
    # generation so the revision is verified like any other attempt.
    ApplicationStatus.CHANGES_REQUESTED: frozenset(
        {ApplicationStatus.RESUME_GENERATING}
    ),
    # M10 Phase 2. See the module docstring's readiness section.
    ApplicationStatus.APPROVED: frozenset({ApplicationStatus.SUBMISSION_READY}),
    # M11 Phase 2: human Submit action opens SUBMITTING.
    ApplicationStatus.SUBMISSION_READY: frozenset(
        {
            ApplicationStatus.MANUAL_HANDOFF,
            ApplicationStatus.POLICY_BLOCKED,
            ApplicationStatus.SUBMITTING,
        }
    ),
    # M11 Phase 2: submission execution transitions to SUBMITTED or SUBMISSION_FAILED.
    ApplicationStatus.SUBMITTING: frozenset(
        {ApplicationStatus.SUBMITTED, ApplicationStatus.SUBMISSION_FAILED}
    ),
    ApplicationStatus.SUBMITTED: frozenset(),
    # M11 Phase 3: bounded retry recovery transition.
    ApplicationStatus.SUBMISSION_FAILED: frozenset({ApplicationStatus.SUBMITTING}),
    ApplicationStatus.MANUAL_HANDOFF: frozenset(),
    ApplicationStatus.POLICY_BLOCKED: frozenset(),
    ApplicationStatus.ABANDONED: frozenset(),
    ApplicationStatus.ERROR: frozenset(),
}


ALLOWED_TRANSITION_ACTORS: dict[
    tuple[ApplicationStatus, ApplicationStatus], frozenset[ActorType]
] = {
    # M11 Phase 2: SUBMISSION_READY -> SUBMITTING is strictly human-authorized.
    (ApplicationStatus.SUBMISSION_READY, ApplicationStatus.SUBMITTING): frozenset({"human"}),
    # Completion transitions from SUBMITTING are strictly system-driven.
    (ApplicationStatus.SUBMITTING, ApplicationStatus.SUBMITTED): frozenset({"system"}),
    (ApplicationStatus.SUBMITTING, ApplicationStatus.SUBMISSION_FAILED): frozenset({"system"}),
    # M11 Phase 3: retry recovery transition is strictly system-driven.
    (ApplicationStatus.SUBMISSION_FAILED, ApplicationStatus.SUBMITTING): frozenset({"system"}),
}


def can_transition(
    current: ApplicationStatus,
    target: ApplicationStatus,
    *,
    actor_type: ActorType | None = None,
) -> bool:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        return False
    if actor_type is not None:
        allowed = ALLOWED_TRANSITION_ACTORS.get((current, target))
        if allowed is not None and actor_type not in allowed:
            return False
    return True


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

    allowed_actors = ALLOWED_TRANSITION_ACTORS.get((current, target))
    if allowed_actors is not None and actor_type not in allowed_actors:
        raise IllegalTransitionError(
            f"application {application.id}: transition {current} -> {target} "
            f"requires actor in {sorted(allowed_actors)}, got {actor_type!r}"
        )

    application.status = target
    if (
        target is ApplicationStatus.SUBMITTED
        and hasattr(application, "submitted_at")
        and application.submitted_at is None
    ):
        application.submitted_at = utc_now()
    if (
        target is ApplicationStatus.SUBMISSION_FAILED
        and reason
        and hasattr(application, "error_reason")
        and not application.error_reason
    ):
        application.error_reason = reason
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
