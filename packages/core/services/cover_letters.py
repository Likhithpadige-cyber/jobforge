"""M10 Phase 1 - grounded cover-letter generation and human review/rework.

Mirrors `packages.core.services.resume_review`'s shape for the review half
(open, approve, request-changes) and the append-only versioning pattern
`packages.agents.resume_optimizer.resume_persistence` uses for resumes - with
no schema change, because `cover_letters` already carries the identical
`application_id`/`version` shape `resumes` does (see the M10 cover-letter
design check this module implements).

**What is deliberately different from resume rework.** A cover letter's
review/rework cycle never touches `applications.status`: it lives entirely
inside `AWAITING_APPROVAL`, the same way `ApprovalKind.ANSWERS`'s
request-changes already does not move the application. There is no C3-style
durable-attempt/lease table here, and none is needed: generation is
worker/event-triggered (like `generate_first_draft`), not synchronous inside
one HTTP request, so at-least-once event redelivery plus the idempotency
check in `generate_cover_letter` gives the same "an attempt's evidence
survives a crash" guarantee C3 was built for, without new schema.

**Versioning.** Every generation - first draft or a regeneration after
`request_changes_cover_letter` - creates a new, append-only `CoverLetter`
row. Nothing UPDATEs an existing row's `content` after creation: an
already-reviewed or approved version can never be mutated out from under a
granted or pending decision. `Approval.subject_snapshot` binds a decision to
`{cover_letter_id, cover_letter_version}` exactly, so a decision made about
an older version can never authorize a newer one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from packages.agents.cover_letter.drafting import BODY_SECTION, draft_cover_letter
from packages.agents.llm.runtime import CostLimitError, Request, Response, StructuredClient
from packages.agents.resume_optimizer.context import load_tailoring_context
from packages.agents.resume_optimizer.handoff import (
    ClaimRef,
    assert_unique_identities,
    verify_claim_for_handoff,
)
from packages.agents.resume_optimizer.resume_persistence import (
    build_content,
    build_verification_report,
)
from packages.core.db.inference_audit import CoverLetterInferenceAudit
from packages.core.events import emit
from packages.core.models import (
    Application,
    ApplicationStatus,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    CoverLetter,
    CoverLetterClaim,
    User,
)
from packages.shared.clock import utc_now
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

PASSED = "passed"
FAILED = "failed"


class CoverLetterClient(StructuredClient):
    """The configured run budget applies across drafting and verification
    together - mirrors `packages.agents.resume_optimizer.first_draft.
    FirstDraftClient` exactly, for the same reason: one cover-letter
    generation is one pipeline (draft, then verify every paragraph), and its
    cost must be tracked as one run, not reset between stages."""

    total_spent: Decimal = Decimal("0")

    async def complete(self, request: Request, *, spent: Decimal = Decimal("0")) -> Response:
        response = await super().complete(request, spent=max(spent, self.total_spent))
        self.total_spent += response.cost_usd
        return response

#: Same cap resume rework uses (`resume_review.ReworkLimitReachedError`),
#: reused as a value rather than a shared constant: cover-letter rework has
#: its own rate limit and its own counted approval kind, so the two counters
#: are independent even though they share one number today.
MAX_REWORK_CYCLES = 3


class CoverLetterStateError(ConflictError):
    """The application, or its cover letter, is not in a state this action allows."""


class NoCoverLetterError(NotFoundError):
    """No cover letter has been generated for this application yet."""


class StaleCoverLetterApprovalError(ConflictError):
    """The approval under decision does not describe the current cover letter.

    An approval is bound to one cover-letter id at one version; if a newer
    version has since been generated, the human's decision was made about
    text that is no longer what would be reviewed, so it is refused rather
    than carried forward.
    """


class MissingNoteError(ValidationError):
    """Request-changes was called without instructions for the author."""


class CoverLetterReworkLimitReachedError(ConflictError):
    """A fourth distinct human cover-letter rework decision is forbidden."""

    error_type = "https://jobforge.local/errors/cover-letter-rework-limit-reached"

    def __init__(self) -> None:
        super().__init__(
            "This application has already used its three cover-letter "
            "request-changes cycles",
            code="cover_letter_rework_limit_reached",
        )


# ---------------------------------------------------------------------------
# Versioning - mirrors resume_persistence._next_version / resume_review's
# latest_tailored_resume, with no schema change (see module docstring).
# ---------------------------------------------------------------------------


async def latest_cover_letter(
    session: AsyncSession, application_id: UUID
) -> CoverLetter | None:
    """The highest cover-letter version for this application, if any.

    Ordering mirrors `resume_review.latest_tailored_resume` exactly:
    version, then created_at, then id, all descending - deterministic even
    when two rows share a version-adjacent timestamp, and correct even though
    (like `resumes`) `cover_letters` has no `UNIQUE(application_id, version)`
    constraint; concurrency is serialized by the `Application` row lock the
    caller holds while allocating the next version, not by this index.
    """
    return (
        await session.execute(
            select(CoverLetter)
            .options(selectinload(CoverLetter.claims))
            .where(CoverLetter.application_id == application_id)
            .order_by(
                CoverLetter.version.desc(),
                CoverLetter.created_at.desc(),
                CoverLetter.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _next_version(session: AsyncSession, application_id: UUID) -> int:
    """The next append-only version. Caller must hold the `Application` lock."""
    current = (
        await session.execute(
            select(func.max(CoverLetter.version)).where(
                CoverLetter.application_id == application_id
            )
        )
    ).scalar_one()
    return (current or 0) + 1


async def _cover_letter_for_event(
    session: AsyncSession, application_id: UUID, event_id: UUID
) -> CoverLetter | None:
    """Idempotency lookup: did this exact triggering event already produce a
    row? Mirrors `generate_first_draft`'s digest check - a redelivered
    `cover_letter.generation.requested` must not produce a second version."""
    rows = (
        await session.execute(
            select(CoverLetter)
            .options(selectinload(CoverLetter.claims))
            .where(CoverLetter.application_id == application_id)
        )
    ).scalars()
    target = str(event_id)
    for row in rows:
        if (row.verification_report or {}).get("triggering_event_id") == target:
            return row
    return None


def cover_letter_snapshot_matches(snapshot: dict[str, Any], cover_letter: CoverLetter) -> bool:
    """Does this approval describe exactly this cover letter, at this version?

    Same two-field requirement `resume_review.snapshot_matches` states for
    resumes: matching on id alone would let a regenerated version inherit a
    decision made about its predecessor.
    """
    return (
        str(snapshot.get("cover_letter_id")) == str(cover_letter.id)
        and snapshot.get("cover_letter_version") == cover_letter.version
    )


def build_cover_letter_subject_snapshot(
    *, application: Application, cover_letter: CoverLetter, claims: list[CoverLetterClaim]
) -> dict[str, Any]:
    """Exactly what the reviewer was shown, recorded at request time.

    Identifiers, counts and verdict tallies only - the same discipline
    `resume_review.build_subject_snapshot` documents: the audit trail needs
    to prove *which* artefact was approved, and it can point at the rows
    that still hold the text.
    """
    return {
        "cover_letter_id": str(cover_letter.id),
        "cover_letter_version": cover_letter.version,
        "verification_status": cover_letter.verification_status,
        "claim_count": len(claims),
        "application_id": str(application.id),
        "application_status": str(application.status),
        "job_id": str(application.job_id),
        "model": cover_letter.model,
        "prompt_version": cover_letter.prompt_version,
    }


# ---------------------------------------------------------------------------
# Generation - the worker entry point
# ---------------------------------------------------------------------------


async def _load_application(
    session: AsyncSession, application_id: UUID, *, lock: bool
) -> Application:
    stmt = select(Application).where(Application.id == application_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    application = (await session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"applications {application_id} not found")
    return application


async def generate_cover_letter(
    session: AsyncSession,
    *,
    application_id: UUID,
    event_id: UUID,
    client: CoverLetterClient,
    note: str | None = None,
    causation_id: UUID | None = None,
) -> CoverLetter:
    """Draft, verify, persist and (if grounded) open review for one version.

    Idempotent and safe to replay: a redelivered `cover_letter.generation.
    requested` that already produced a row for this exact event returns that
    row untouched rather than drafting again.

    Only a cover letter whose every paragraph is claim-verified (`verdict ==
    SUPPORTED` for every paragraph) opens a `COVER_LETTER` approval. A
    version that fails verification is still persisted, in full, as an
    honest record of the attempt - never silently discarded - but is not put
    in front of a human, mirroring the same "only a passing resume is
    reviewable" rule `resume_review.evaluate_readiness` enforces for resumes.
    """
    application = await _load_application(session, application_id, lock=True)
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        raise CoverLetterStateError(
            f"cover-letter generation requires the application to be "
            f"awaiting_approval, not {application.status}"
        )

    existing = await _cover_letter_for_event(session, application_id, event_id)
    if existing is not None:
        log.info(
            "cover_letter.generate.idempotent",
            application_id=str(application_id),
            cover_letter_id=str(existing.id),
            event_id=str(event_id),
        )
        return existing

    context = await load_tailoring_context(
        session, profile_id=application.profile_id, job_id=application.job_id
    )

    writer = CoverLetterInferenceAudit(
        event_id=event_id, application_id=application.id, profile_id=application.profile_id
    )
    draft = await draft_cover_letter(
        session, context=context, client=client, note=note, writer=writer
    )

    refs = tuple(
        ClaimRef(
            section=BODY_SECTION,
            path=f"/{BODY_SECTION}/{index}",
            claim_text=paragraph.text,
            fact_ids=tuple(paragraph.fact_ids),
            numerals_used=tuple(paragraph.numerals_used),
        )
        for index, paragraph in enumerate(draft.paragraphs)
    )
    assert_unique_identities(refs)

    verified = tuple(
        [
            await verify_claim_for_handoff(
                session,
                claim=ref,
                context=context,
                aggregate_id=application.job_id,
                client=client,
                writer=writer,
                claim_index=index,
            )
            for index, ref in enumerate(refs)
        ]
    )
    if client.total_spent > client.max_cost:
        raise CostLimitError("cover-letter generation run budget exceeded")

    version = await _next_version(session, application_id)
    passed = not any(item.is_negative for item in verified)
    report = {**build_verification_report(verified), "triggering_event_id": str(event_id)}

    cover_letter = CoverLetter(
        id=uuid7(),
        profile_id=application.profile_id,
        application_id=application.id,
        version=version,
        content=build_content(verified),
        body_text="\n\n".join(item.claim.claim_text for item in verified),
        verification_status=PASSED if passed else FAILED,
        verification_report=report,
        model=client.model,
        prompt_version=draft.prompt_version,
    )
    session.add(cover_letter)
    await session.flush()

    # Tracked in a parallel list rather than read back off `cover_letter.claims`:
    # that relationship was never loaded on this freshly-added, not-yet-eager-
    # loaded object, so touching it here would be a lazy load in an async
    # session - a `MissingGreenlet` waiting for the first attribute read. M9
    # hit exactly this bug in `prepare_application` and fixed it the same way.
    created_claims: list[CoverLetterClaim] = []
    for item in verified:
        claim = CoverLetterClaim(
            id=uuid7(),
            cover_letter_id=cover_letter.id,
            section=item.claim.section,
            path=item.claim.path,
            claim_text=item.claim.claim_text,
            fact_ids=[UUID(fid) for fid in item.claim.fact_ids],
            numerals_used=list(item.claim.numerals_used),
            verdict=item.verdict,
            verdict_reason=item.explanation,
            verified_at=item.verified_at,
        )
        session.add(claim)
        created_claims.append(claim)
    await session.flush()

    await emit(
        session,
        type="cover_letter.generated",
        aggregate_type="resume",
        aggregate_id=cover_letter.id,
        correlation_id=application.id,
        causation_id=causation_id,
        actor_type="agent",
        actor_id="job_application",
        payload={
            "cover_letter_id": str(cover_letter.id),
            "application_id": str(application.id),
            "cover_letter_version": version,
            "claim_count": len(verified),
            "verified_count": sum(1 for item in verified if not item.is_negative),
        },
    )

    if passed:
        await _open_review(
            session, application=application, cover_letter=cover_letter, claims=created_claims
        )
    else:
        log.warning(
            "cover_letter.generate.verification_failed",
            application_id=str(application_id),
            cover_letter_id=str(cover_letter.id),
            version=version,
        )

    return cover_letter


async def _open_review(
    session: AsyncSession,
    *,
    application: Application,
    cover_letter: CoverLetter,
    claims: list[CoverLetterClaim],
) -> Approval:
    """Create the pending `COVER_LETTER` approval for a freshly-verified version.

    Called only from `generate_cover_letter`, never exposed as a separate API
    action: unlike resume review (where a human explicitly opens review),
    there is no owner-supplied input a cover letter needs before review can
    begin, so opening it is part of generation itself, not a second step.
    `uq_one_pending_approval` (application_id, kind) - the same partial
    unique index `ApprovalKind.RESUME` already relies on - guarantees at
    most one pending `COVER_LETTER` approval exists at a time; this function
    is reachable only once per version, immediately after that version is
    created, so it can never race a second call for the same version.
    """
    approval = Approval(
        id=uuid7(),
        application_id=application.id,
        kind=ApprovalKind.COVER_LETTER,
        status=ApprovalStatus.PENDING,
        subject_snapshot=build_cover_letter_subject_snapshot(
            application=application, cover_letter=cover_letter, claims=claims
        ),
        requested_at=utc_now(),
    )
    session.add(approval)
    await session.flush()
    await emit(
        session,
        type="approval.requested",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="agent",
        actor_id="job_application",
        payload={
            "approval_id": str(approval.id),
            "application_id": str(application.id),
            "kind": str(ApprovalKind.COVER_LETTER),
            "guardrail_flags": {},
            "human_required_answers": 0,
            "requested_at": approval.requested_at.isoformat(),
        },
    )
    return approval


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


NOT_STARTED = "not_started"
IN_REVIEW = "in_review"
APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"
FAILED_VERIFICATION = "failed_verification"


@dataclass(frozen=True)
class CoverLetterReviewState:
    """Everything a reviewer needs to see and act on for the cover letter."""

    application_id: UUID
    application_status: ApplicationStatus
    cover_letter_id: UUID | None = None
    cover_letter_version: int | None = None
    verification_status: str | None = None
    review_status: str = NOT_STARTED
    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    approval_is_current: bool = False
    decision_note: str | None = None
    changes_requested: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    decided_by_email: str | None = None
    can_approve: bool = False
    can_request_changes: bool = False
    blocked_reason: str | None = None


async def pending_cover_letter_approval(
    session: AsyncSession, application_id: UUID
) -> Approval | None:
    """The single pending `COVER_LETTER` approval, if one is open."""
    return (
        await session.execute(
            select(Approval).where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.COVER_LETTER,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).scalar_one_or_none()


async def cover_letter_approval(
    session: AsyncSession, application_id: UUID
) -> Approval | None:
    """The most recent `COVER_LETTER` approval, if any, newest first."""
    return (
        await session.execute(
            select(Approval)
            .where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.COVER_LETTER,
            )
            .order_by(Approval.requested_at.desc(), Approval.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _decider_email(session: AsyncSession, user_id: UUID | None) -> str | None:
    if user_id is None:
        return None
    return (
        await session.execute(select(User.email).where(User.id == user_id))
    ).scalar_one_or_none()


def _review_status(cover_letter: CoverLetter | None, approval: Approval | None) -> str:
    if cover_letter is None:
        return NOT_STARTED
    if cover_letter.verification_status == FAILED:
        return FAILED_VERIFICATION
    if approval is None:
        return NOT_STARTED
    if approval.status is ApprovalStatus.APPROVED:
        return APPROVED
    if approval.status is ApprovalStatus.CHANGES_REQUESTED:
        return CHANGES_REQUESTED
    return IN_REVIEW


async def cover_letter_review_state(
    session: AsyncSession, application_id: UUID
) -> CoverLetterReviewState:
    """The review read model for one application. Reads only; mutates nothing."""
    application = await _load_application(session, application_id, lock=False)
    cover_letter = await latest_cover_letter(session, application_id)
    approval = await cover_letter_approval(session, application_id)

    if cover_letter is None:
        return CoverLetterReviewState(
            application_id=application.id,
            application_status=application.status,
            blocked_reason="no cover letter has been generated for this application yet",
        )

    current = approval is not None and cover_letter_snapshot_matches(
        approval.subject_snapshot, cover_letter
    )
    decidable = (
        application.status is ApplicationStatus.AWAITING_APPROVAL
        and approval is not None
        and approval.status is ApprovalStatus.PENDING
        and current
    )

    blocked: str | None = None
    if cover_letter.verification_status == FAILED:
        blocked = "this cover-letter version failed grounding verification and cannot be reviewed"
    elif application.status is not ApplicationStatus.AWAITING_APPROVAL:
        blocked = f"no review action is available from {application.status}"
    elif approval is not None and not current:
        blocked = "a newer cover-letter version exists; this review is stale"
    elif approval is None:
        blocked = "no cover-letter approval is open"
    elif approval.status is not ApprovalStatus.PENDING:
        blocked = f"the current cover-letter approval is already {approval.status}"

    return CoverLetterReviewState(
        application_id=application.id,
        application_status=application.status,
        cover_letter_id=cover_letter.id,
        cover_letter_version=cover_letter.version,
        verification_status=cover_letter.verification_status,
        review_status=_review_status(cover_letter, approval),
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
        can_approve=decidable,
        can_request_changes=decidable,
        blocked_reason=blocked,
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def _decidable_approval(
    session: AsyncSession, application: Application, cover_letter: CoverLetter
) -> Approval:
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        raise CoverLetterStateError(
            f"a decision requires the application to be awaiting_approval, "
            f"not {application.status}"
        )
    approval = await pending_cover_letter_approval(session, application.id)
    if approval is None:
        raise CoverLetterStateError(
            f"application {application.id} has no pending cover-letter approval to decide"
        )
    if not cover_letter_snapshot_matches(approval.subject_snapshot, cover_letter):
        raise StaleCoverLetterApprovalError(
            "this approval was raised for cover letter "
            f"{approval.subject_snapshot.get('cover_letter_id')} "
            f"v{approval.subject_snapshot.get('cover_letter_version')}, but the current "
            f"cover letter is {cover_letter.id} v{cover_letter.version}"
        )
    return approval


async def approve_cover_letter(
    session: AsyncSession,
    *,
    application_id: UUID,
    actor: User,
    decision_note: str | None = None,
) -> CoverLetterReviewState:
    """Approve the current cover-letter version.

    M10 Phase 2: may move `applications.status` from `awaiting_approval` to
    `approved`, but only through the shared completion gate
    (`packages.core.services.review_completion.maybe_mark_application_approved`)
    and only when RESUME and (if required) ANSWERS are also current-approved -
    this call alone is never sufficient, the same way granting the RESUME
    approval alone is not. See that module's docstring.

    Idempotent by state: a retry that finds the same version already
    approved by this decision returns the current state untouched rather
    than raising, the same discipline `resume_review.approve` uses.
    """
    application = await _load_application(session, application_id, lock=True)
    cover_letter = await latest_cover_letter(session, application_id)
    if cover_letter is None:
        raise NoCoverLetterError(
            f"no cover letter has been generated for application {application_id}"
        )
    approval = await cover_letter_approval(session, application_id)
    if (
        approval is not None
        and approval.status is ApprovalStatus.APPROVED
        and cover_letter_snapshot_matches(approval.subject_snapshot, cover_letter)
    ):
        log.info("cover_letter.approve.idempotent", application_id=str(application_id))
        return await cover_letter_review_state(session, application_id)

    approval = await _decidable_approval(session, application, cover_letter)
    decided_at = utc_now()
    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = actor.id
    approval.decided_at = decided_at
    if decision_note is not None:
        approval.decision_note = decision_note.strip() or None
    await session.flush()

    granted = await emit(
        session,
        type="approval.granted",
        aggregate_type="approval",
        aggregate_id=approval.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={
            "approval_id": str(approval.id),
            "application_id": str(application.id),
            "kind": str(ApprovalKind.COVER_LETTER),
            "decided_by": str(actor.id),
            "decision_note": approval.decision_note,
        },
    )

    # Local import: review_completion imports back from this module for
    # cover_letter_snapshot_matches/latest_cover_letter.
    from packages.core.services.review_completion import maybe_mark_application_approved

    await maybe_mark_application_approved(
        session, application, actor=actor, causation_id=granted.id
    )
    return await cover_letter_review_state(session, application_id)


async def request_changes_cover_letter(
    session: AsyncSession, *, application_id: UUID, actor: User, note: str
) -> CoverLetterReviewState:
    """Record the rework request and ask the worker to regenerate.

    Unlike resume rework, nothing about `applications.status` changes here -
    this mirrors `ApprovalKind.ANSWERS`'s `request_changes_answers`, which
    also stays in `awaiting_approval` throughout, not `resume_review.
    request_changes`'s move to `changes_requested`: a cover letter is a
    packet component, not the artefact the wider state machine gates on.

    Regeneration is requested, not performed here: this function emits
    `cover_letter.generation.requested` (carrying the note) in the same
    transaction as the decision, and the worker drafts vN+1 asynchronously,
    exactly as the initial draft was requested from `resume_review.
    enter_review`. That is what keeps this action from needing C3's
    lease/fencing machinery - nothing here waits on the LLM call inside this
    request.
    """
    instructions = (note or "").strip()
    if not instructions:
        raise MissingNoteError(
            "requesting changes needs a note saying what should change"
        )

    application = await _load_application(session, application_id, lock=True)
    cover_letter = await latest_cover_letter(session, application_id)
    if cover_letter is None:
        raise NoCoverLetterError(
            f"no cover letter has been generated for application {application_id}"
        )
    approval = await cover_letter_approval(session, application_id)
    if (
        approval is not None
        and approval.status is ApprovalStatus.CHANGES_REQUESTED
        and cover_letter_snapshot_matches(approval.subject_snapshot, cover_letter)
        and approval.changes_requested == instructions
    ):
        log.info(
            "cover_letter.request_changes.idempotent", application_id=str(application_id)
        )
        return await cover_letter_review_state(session, application_id)

    cycles_used = (
        await session.execute(
            select(func.count(Approval.id)).where(
                Approval.application_id == application_id,
                Approval.kind == ApprovalKind.COVER_LETTER,
                Approval.status == ApprovalStatus.CHANGES_REQUESTED,
            )
        )
    ).scalar_one()
    if cycles_used >= MAX_REWORK_CYCLES:
        raise CoverLetterReworkLimitReachedError()

    approval = await _decidable_approval(session, application, cover_letter)
    decided_at = utc_now()
    approval.status = ApprovalStatus.CHANGES_REQUESTED
    approval.changes_requested = instructions
    approval.decision_note = instructions
    approval.decided_by = actor.id
    approval.decided_at = decided_at
    await session.flush()

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
            "kind": str(ApprovalKind.COVER_LETTER),
            "decided_by": str(actor.id),
            "instructions": instructions,
            "target": "cover_letter",
        },
    )
    await emit(
        session,
        type="cover_letter.generation.requested",
        aggregate_type="resume",
        aggregate_id=application.id,
        correlation_id=application.id,
        actor_type="human",
        actor_id=str(actor.id),
        payload={"application_id": str(application.id), "note": instructions},
    )
    return await cover_letter_review_state(session, application_id)
