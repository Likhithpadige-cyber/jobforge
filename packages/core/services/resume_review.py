"""C2 - human review and approval of a tailored resume.

Three actions, and nothing else: enter review, approve, request changes. Each
one is a single transactional unit that the caller commits.

**Why a service and not router code.** The rules here are not HTTP rules. They
are invariants about what may be approved, and they are worth testing without
spinning up a request. The router is left with exactly two jobs: establish who
the human is, and translate a refusal into a status code.

What this module deliberately does *not* do:

* It never assigns ``application.status`` directly. Every move goes through
  `transition_application`, so the graph, the history row and the outbox event
  are impossible to skip.
* It never accepts an actor id as data. The only way to name the deciding human
  is to pass a loaded `User`, which the API obtains from the server-side
  session. There is no code path from a request body to ``decided_by``.
* It never regenerates, re-verifies, renders or repairs anything. A resume that
  fails its preconditions is refused, not fixed. Silently repairing the thing a
  human is about to vouch for is the one failure mode this whole phase exists to
  prevent.

**The database has the final say on approval.** `trg_applications_require_real_approval`
refuses to let an application reach ``approved`` unless an ``approvals`` row for
it is already ``approved`` with a non-null ``decided_by``, and unless
``applications.approved_by`` names that same human. This module satisfies that
trigger in the honest order - decide the approval first, flush it, then move the
application - rather than working around it. If the ordering here ever regressed,
the database would reject the write, which is exactly the intended safety net.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from packages.core.events import emit
from packages.core.models import (
    Application,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    ClaimVerdict,
    Resume,
    ResumeClaim,
    User,
)
from packages.core.state_machines.application import transition_application
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

#: The only resume kind a human reviews. A base resume is a profile artefact.
TAILORED = "tailored"

#: `resumes.verification_status` value that C2 will consider.
PASSED = "passed"

#: EVENTS.md §3.4 - `approval.changes_requested` carries which artefact the
#: instructions are about. C2 only ever reviews a resume.
CHANGES_TARGET = "resume"


class ReviewStateError(ConflictError):
    """The application or its approval is not in a state this action allows.

    A 409 rather than a 422: the request was well-formed and the caller is not
    at fault. Something moved - or never got where the caller assumed it was.
    """


class StaleResumeError(ConflictError):
    """The approval under decision does not describe the current resume.

    This is the check that makes an approval mean something. An approval is
    bound to one resume id at one version; if a newer version has since been
    generated, the human's decision was made about text that is no longer what
    would be submitted, so it is refused rather than carried forward.
    """


class MissingNoteError(ValidationError):
    """Request-changes was called without instructions for the author.

    "Changes requested" with no note is an instruction to guess. The whole
    value of the state is the sentence that says what to change.
    """


# ---------------------------------------------------------------------------
# Readiness - pure, so the rules can be tested without a database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    """Whether a resume may be put in front of a human, and why not."""

    ready: bool
    reason: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ready


READY = Readiness(ready=True)


def content_paths(content: dict[str, Any]) -> set[str]:
    """Every `/section/index` pointer the persisted content contains.

    `build_content` (B6) stores each section as an index-keyed object whose
    keys are the tail of the claim path, precisely so the two can be compared.
    This is that comparison.
    """
    paths: set[str] = set()
    for section, entries in content.items():
        if not isinstance(entries, dict):
            continue
        for index in entries:
            paths.add(f"/{section}/{index}")
    return paths


def evaluate_readiness(
    *,
    verification_status: str,
    claims: list[ResumeClaim],
    content: dict[str, Any],
) -> Readiness:
    """Is this resume safe to put in front of a human for approval?

    Every condition is a refusal, never a repair. The order is deliberate:
    the broadest fact first (did verification pass at all), then the per-claim
    verdicts, then coverage - so the reported reason is the most useful one
    rather than whichever check happened to run first.

    Coverage is checked in both directions. A rendered line with no claim is an
    unverified sentence hiding in plain sight; a claim with no line is a stale
    verdict. Either means the claim table and the resume text have drifted, and
    a human approving one would be told about the other.
    """
    if verification_status != PASSED:
        return Readiness(
            False, f"resume verification status is {verification_status!r}, not 'passed'"
        )
    if not claims:
        return Readiness(False, "resume has no recorded claims to review")

    tally = dict.fromkeys(ClaimVerdict, 0)
    for claim in claims:
        tally[claim.verdict] += 1
    for verdict in (
        ClaimVerdict.UNSUPPORTED,
        ClaimVerdict.OVERREACH,
        ClaimVerdict.NOT_CHECKED,
    ):
        if tally[verdict]:
            return Readiness(
                False,
                f"{tally[verdict]} claim(s) are {verdict}; every claim must be supported",
            )

    unverified = [claim.path for claim in claims if claim.verified_at is None]
    if unverified:
        return Readiness(
            False, f"{len(unverified)} claim(s) carry no verification timestamp"
        )

    claimed = {claim.path for claim in claims}
    rendered = content_paths(content)
    uncovered = rendered - claimed
    if uncovered:
        return Readiness(
            False,
            f"{len(uncovered)} rendered line(s) have no verified claim: "
            f"{', '.join(sorted(uncovered)[:3])}",
        )
    orphaned = claimed - rendered
    if orphaned:
        return Readiness(
            False,
            f"{len(orphaned)} claim(s) point at content that is not present: "
            f"{', '.join(sorted(orphaned)[:3])}",
        )
    return READY


def snapshot_matches(snapshot: dict[str, Any], resume: Resume) -> bool:
    """Does this approval describe exactly this resume, at this version?

    Both halves are required. Matching on id alone would let a regenerated
    version inherit a decision made about its predecessor, which is the precise
    thing `subject_snapshot` exists to prevent.
    """
    return (
        str(snapshot.get("resume_id")) == str(resume.id)
        and snapshot.get("resume_version") == resume.version
    )


def build_subject_snapshot(
    *, application: Application, resume: Resume, claims: list[ResumeClaim]
) -> dict[str, Any]:
    """Exactly what the reviewer was shown, recorded at request time.

    Identifiers, counts and verdict tallies only. No claim prose, no job
    description, no profile payload: the audit trail needs to prove *which*
    artefact was approved, and it can point at the rows that still hold the
    text. Copying the text here would duplicate it into a second place that
    nothing keeps in sync.
    """
    return {
        "resume_id": str(resume.id),
        "resume_version": resume.version,
        "resume_kind": resume.kind,
        "verification_status": resume.verification_status,
        "claim_count": len(claims),
        "application_id": str(application.id),
        "application_status": str(application.status),
        "job_id": str(application.job_id),
        "model": resume.model,
        "prompt_version": resume.prompt_version,
    }


def build_guardrail_flags(claims: list[ResumeClaim]) -> dict[str, Any]:
    """The verdict tally that justified opening review, kept with the approval.

    `approvals.guardrail_flags` is the column for "what did the system know
    when it asked". Storing the tally means a later reader can see that the
    resume was clean when it was submitted for review without re-deriving it
    from rows that may since have changed.
    """
    tally = dict.fromkeys((str(verdict) for verdict in ClaimVerdict), 0)
    for claim in claims:
        tally[str(claim.verdict)] += 1
    return {"verdict_counts": tally, "total_claims": len(claims)}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """The three rows every review action needs, loaded once and consistently."""

    application: Application
    resume: Resume
    claims: list[ResumeClaim]
    approval: Approval | None


async def _load_application(
    session: AsyncSession, application_id: UUID, *, lock: bool
) -> Application:
    stmt = select(Application).where(Application.id == application_id)
    if lock:
        # Serialises concurrent review actions on one application. Two browser
        # tabs both clicking Approve must not both pass their preconditions.
        stmt = stmt.with_for_update()
    application = (await session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"applications {application_id} not found")
    return application


async def latest_tailored_resume(
    session: AsyncSession, application_id: UUID
) -> Resume | None:
    """The highest tailored version - the same rule the read endpoint uses."""
    return (
        await session.execute(
            select(Resume)
            .options(selectinload(Resume.claims))
            .where(Resume.application_id == application_id, Resume.kind == TAILORED)
            .order_by(Resume.version.desc(), Resume.created_at.desc(), Resume.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resume_approval(
    session: AsyncSession, application_id: UUID
) -> Approval | None:
    """The most recent RESUME approval for this application, if any.

    Ordered newest-first so a superseded predecessor never shadows the row the
    UI should be showing.
    """
    return (
        await session.execute(
            select(Approval)
            .where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.RESUME,
            )
            .order_by(Approval.requested_at.desc(), Approval.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def pending_resume_approval(
    session: AsyncSession, application_id: UUID
) -> Approval | None:
    """The single pending RESUME approval, if one is open.

    ``uq_one_pending_approval`` guarantees at most one per (application, kind),
    so this is a lookup rather than a choice.
    """
    return (
        await session.execute(
            select(Approval).where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.RESUME,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).scalar_one_or_none()


async def _context(
    session: AsyncSession, application_id: UUID, *, lock: bool
) -> ReviewContext:
    application = await _load_application(session, application_id, lock=lock)
    resume = await latest_tailored_resume(session, application_id)
    if resume is None:
        raise NotFoundError(f"no resume has been generated for {application_id}")
    claims = list(resume.claims)
    approval = await resume_approval(session, application_id)
    return ReviewContext(
        application=application, resume=resume, claims=claims, approval=approval
    )


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewState:
    """Everything the studio needs to render the review panel.

    The three `can_*` booleans are computed here rather than in the browser so
    that what the UI enables and what the API accepts come from one rule set.
    A button the server would refuse is a bug, not a styling question.
    """

    application_id: UUID
    application_status: ApplicationStatus
    resume_id: UUID
    resume_version: int
    review_status: str
    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    approval_is_current: bool = False
    decision_note: str | None = None
    changes_requested: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    decided_by_email: str | None = None
    can_start_review: bool = False
    can_approve: bool = False
    can_request_changes: bool = False
    blocked_reason: str | None = None


#: `review_status` values. Derived from the application status and the
#: approval row together, because either alone can mislead: an application in
#: `resume_ready` with no approval has simply not been reviewed, while one in
#: `resume_ready` whose previous approval was `changes_requested` has.
NOT_STARTED = "not_started"
IN_REVIEW = "in_review"
APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"


def _review_status(
    application: Application, approval: Approval | None
) -> str:
    if application.status is ApplicationStatus.AWAITING_APPROVAL:
        return IN_REVIEW
    if application.status is ApplicationStatus.APPROVED:
        return APPROVED
    if application.status is ApplicationStatus.CHANGES_REQUESTED:
        return CHANGES_REQUESTED
    if approval is not None and approval.status is ApprovalStatus.CHANGES_REQUESTED:
        return CHANGES_REQUESTED
    return NOT_STARTED


async def _decider_email(session: AsyncSession, user_id: UUID | None) -> str | None:
    """The deciding human's email, for display beside their decision.

    Email is what `GET /auth/me` already returns for the signed-in user, so it
    introduces no new class of exposure. Nothing else about the user is read:
    no password hash, no TOTP secret, no session data.
    """
    if user_id is None:
        return None
    return (
        await session.execute(select(User.email).where(User.id == user_id))
    ).scalar_one_or_none()


async def review_state(session: AsyncSession, application_id: UUID) -> ReviewState:
    """The review read model for one application. Reads only; mutates nothing."""
    context = await _context(session, application_id, lock=False)
    application, resume, approval = (
        context.application,
        context.resume,
        context.approval,
    )
    current = approval is not None and snapshot_matches(
        approval.subject_snapshot, resume
    )
    readiness = evaluate_readiness(
        verification_status=resume.verification_status,
        claims=context.claims,
        content=resume.content,
    )

    can_start = (
        application.status is ApplicationStatus.RESUME_READY and readiness.ready
    )
    decidable = (
        application.status is ApplicationStatus.AWAITING_APPROVAL
        and approval is not None
        and approval.status is ApprovalStatus.PENDING
        and current
        and readiness.ready
    )

    blocked: str | None = None
    if not readiness.ready:
        blocked = readiness.reason
    elif application.status is ApplicationStatus.AWAITING_APPROVAL and not current:
        blocked = "a newer resume version exists; this review is stale"
    elif not can_start and not decidable:
        blocked = f"no review action is available from {application.status}"

    return ReviewState(
        application_id=application.id,
        application_status=application.status,
        resume_id=resume.id,
        resume_version=resume.version,
        review_status=_review_status(application, approval),
        approval_id=approval.id if approval else None,
        approval_status=approval.status if approval else None,
        approval_is_current=current,
        decision_note=approval.decision_note if approval else None,
        changes_requested=approval.changes_requested if approval else None,
        requested_at=approval.requested_at if approval else None,
        decided_at=approval.decided_at if approval else None,
        decided_by=approval.decided_by if approval else None,
        decided_by_email=await _decider_email(
            session, approval.decided_by if approval else None
        ),
        can_start_review=can_start,
        can_approve=decidable,
        can_request_changes=decidable,
        blocked_reason=blocked,
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def enter_review(
    session: AsyncSession, *, application_id: UUID, actor: User
) -> ReviewState:
    """Open human review: `resume_ready -> awaiting_approval`.

    Idempotent by state rather than by token. A retry finds the application
    already in `awaiting_approval` with a pending approval bound to this exact
    resume, and returns it untouched - no second approval row, no second
    history row, no second event. Anything else in `awaiting_approval` (a
    pending approval for an older version, or none at all) is a genuine
    conflict and is reported as one.
    """
    context = await _context(session, application_id, lock=True)
    application, resume, claims = context.application, context.resume, context.claims

    if application.status is ApplicationStatus.AWAITING_APPROVAL:
        existing = await pending_resume_approval(session, application_id)
        if existing is not None and snapshot_matches(
            existing.subject_snapshot, resume
        ):
            log.info(
                "resume_review.enter.idempotent",
                application_id=str(application_id),
                approval_id=str(existing.id),
            )
            return await review_state(session, application_id)
        raise ReviewStateError(
            f"application {application_id} is already awaiting approval, but not "
            "for the current resume version"
        )

    if application.status is not ApplicationStatus.RESUME_READY:
        raise ReviewStateError(
            f"review can only be opened from resume_ready, not {application.status}"
        )

    readiness = evaluate_readiness(
        verification_status=resume.verification_status,
        claims=claims,
        content=resume.content,
    )
    if not readiness.ready:
        raise ReviewStateError(f"resume is not reviewable: {readiness.reason}")

    approval = Approval(
        id=uuid7(),
        application_id=application.id,
        kind=ApprovalKind.RESUME,
        status=ApprovalStatus.PENDING,
        subject_snapshot=build_subject_snapshot(
            application=application, resume=resume, claims=claims
        ),
        guardrail_flags=build_guardrail_flags(claims),
        requested_at=utc_now(),
    )
    session.add(approval)
    await session.flush()

    await transition_application(
        session,
        application,
        ApplicationStatus.AWAITING_APPROVAL,
        actor_type="human",
        actor_id=str(actor.id),
        reason="resume submitted for human review",
        correlation_id=application.id,
    )
    await emit(
        session,
        type="approval.requested",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "approval_id": str(approval.id),
            "application_id": str(application.id),
            "kind": str(ApprovalKind.RESUME),
            "guardrail_flags": approval.guardrail_flags,
            # No question-extraction phase exists, so nothing is waiting on a
            # human answer. Reported as 0 rather than omitted: EVENTS.md §3.4
            # lists the field, and a consumer should not have to guess.
            "human_required_answers": 0,
            "requested_at": approval.requested_at.isoformat(),
            "resume_id": str(resume.id),
            "resume_version": resume.version,
        },
    )
    log.info(
        "resume_review.entered",
        application_id=str(application_id),
        approval_id=str(approval.id),
        resume_version=resume.version,
    )
    return await review_state(session, application_id)


async def _decidable_approval(
    context: ReviewContext, session: AsyncSession, application_id: UUID
) -> Approval:
    """The pending approval this decision may be applied to, or a refusal.

    Every refusal below is a 409 with a distinct message, because "you cannot
    approve this" has several very different causes and the reviewer deserves
    to know which one they hit.
    """
    application, resume = context.application, context.resume
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        raise ReviewStateError(
            f"a decision requires the application to be awaiting_approval, "
            f"not {application.status}"
        )
    approval = await pending_resume_approval(session, application_id)
    if approval is None:
        raise ReviewStateError(
            f"application {application_id} has no pending resume approval to decide"
        )
    if not snapshot_matches(approval.subject_snapshot, resume):
        raise StaleResumeError(
            "this approval was raised for resume "
            f"{approval.subject_snapshot.get('resume_id')} "
            f"v{approval.subject_snapshot.get('resume_version')}, but the current "
            f"resume is {resume.id} v{resume.version}"
        )
    readiness = evaluate_readiness(
        verification_status=resume.verification_status,
        claims=context.claims,
        content=resume.content,
    )
    if not readiness.ready:
        raise ReviewStateError(f"resume is no longer reviewable: {readiness.reason}")
    return approval


def _already_decided(
    context: ReviewContext,
    *,
    status: ApprovalStatus,
    application_status: ApplicationStatus,
) -> bool:
    """Has this exact decision already been recorded for this exact resume?

    That is what makes a retry safe: the second call finds the terminal state
    it was trying to produce, already bound to the same resume id and version,
    and does nothing rather than writing a duplicate decision.
    """
    approval = context.approval
    return (
        context.application.status is application_status
        and approval is not None
        and approval.status is status
        and snapshot_matches(approval.subject_snapshot, context.resume)
    )


async def approve(
    session: AsyncSession,
    *,
    application_id: UUID,
    actor: User,
    decision_note: str | None = None,
) -> ReviewState:
    """Record a human approval: `awaiting_approval -> approved`.

    The write order is load-bearing. `trg_applications_require_real_approval`
    runs BEFORE UPDATE on `applications` and looks for an already-approved
    `approvals` row naming the same human, so the approval is decided and
    flushed first. Doing it the other way round does not produce a subtly wrong
    audit trail - it produces a rejected transaction, which is the behaviour
    that keeps the trail honest.
    """
    context = await _context(session, application_id, lock=True)
    if _already_decided(
        context,
        status=ApprovalStatus.APPROVED,
        application_status=ApplicationStatus.APPROVED,
    ):
        log.info(
            "resume_review.approve.idempotent", application_id=str(application_id)
        )
        return await review_state(session, application_id)

    approval = await _decidable_approval(context, session, application_id)
    application, resume = context.application, context.resume
    decided_at = utc_now()

    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = actor.id
    approval.decided_at = decided_at
    if decision_note is not None:
        approval.decision_note = decision_note.strip() or None
    # Flushed on its own, before the application moves: the trigger reads this
    # row during the UPDATE that follows.
    await session.flush()

    application.approved_by = actor.id
    application.approved_at = decided_at
    await transition_application(
        session,
        application,
        ApplicationStatus.APPROVED,
        actor_type="human",
        actor_id=str(actor.id),
        reason="resume approved by reviewer",
        correlation_id=application.id,
    )
    payload: dict[str, Any] = {
        "approval_id": str(approval.id),
        "application_id": str(application.id),
        "kind": str(ApprovalKind.RESUME),
        "decided_by": str(actor.id),
        "resume_id": str(resume.id),
        "resume_version": resume.version,
    }
    if approval.decision_note:
        payload["decision_note"] = approval.decision_note
    await emit(
        session,
        type="approval.granted",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        # HUMAN_ONLY_EVENT_TYPES rejects any other actor_type outright. The
        # envelope refuses to be built, so a forged grant cannot be recorded.
        actor_type="human",
        actor_id=str(actor.id),
        payload=payload,
    )
    log.info(
        "resume_review.approved",
        application_id=str(application_id),
        approval_id=str(approval.id),
        resume_version=resume.version,
    )
    return await review_state(session, application_id)


async def request_changes(
    session: AsyncSession, *, application_id: UUID, actor: User, note: str
) -> ReviewState:
    """Record a rework request: `awaiting_approval -> changes_requested`.

    The note is required and checked first, before any state is read. A retry
    with an empty note is still refused: "the previous call supplied one" is
    not a reason to accept a request that says nothing.

    Nothing about the resume changes. No new version, no claim edits, no
    regeneration - C2 records the request and stops. The API orchestration in
    resume_rework invokes C3 in the same transaction after this decision.
    """
    instructions = (note or "").strip()
    if not instructions:
        raise MissingNoteError(
            "requesting changes needs a note saying what should change"
        )

    context = await _context(session, application_id, lock=True)
    if _already_decided(
        context,
        status=ApprovalStatus.CHANGES_REQUESTED,
        application_status=ApplicationStatus.CHANGES_REQUESTED,
    ):
        log.info(
            "resume_review.request_changes.idempotent",
            application_id=str(application_id),
        )
        return await review_state(session, application_id)

    approval = await _decidable_approval(context, session, application_id)
    application, resume = context.application, context.resume
    decided_at = utc_now()

    approval.status = ApprovalStatus.CHANGES_REQUESTED
    # `changes_requested` holds the instructions; `decision_note` mirrors them
    # so a reader scanning decisions of every kind sees the reasoning without
    # having to know which column this kind happens to use.
    approval.changes_requested = instructions
    approval.decision_note = instructions
    approval.decided_by = actor.id
    approval.decided_at = decided_at
    await session.flush()

    await transition_application(
        session,
        application,
        ApplicationStatus.CHANGES_REQUESTED,
        actor_type="human",
        actor_id=str(actor.id),
        reason="reviewer requested changes",
        correlation_id=application.id,
    )
    await emit(
        session,
        type="approval.changes_requested",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "approval_id": str(approval.id),
            "application_id": str(application.id),
            "kind": str(ApprovalKind.RESUME),
            "decided_by": str(actor.id),
            "instructions": instructions,
            "target": CHANGES_TARGET,
            "resume_id": str(resume.id),
            "resume_version": resume.version,
        },
    )
    log.info(
        "resume_review.changes_requested",
        application_id=str(application_id),
        approval_id=str(approval.id),
        resume_version=resume.version,
    )
    return await review_state(session, application_id)
