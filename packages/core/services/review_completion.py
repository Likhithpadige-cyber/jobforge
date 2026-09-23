"""M10 Phase 2 stabilization - the review-completion gate.

**The bug this module fixes.** `resume_review.approve()` used to transition
`awaiting_approval -> approved` the instant the RESUME approval was granted,
without checking COVER_LETTER or ANSWERS. That made approval *order* matter:
approving the resume first silently closed the review window before a
reviewer had approved the cover letter or the answers package, and neither
of those can be decided once the application has left `awaiting_approval` -
so the application became permanently stuck at `approved`, unable to ever
reach `submission_ready`.

**The fix.** `awaiting_approval -> approved` now has exactly one path in:
`maybe_mark_application_approved`, called from all three approval-granting
functions (`resume_review.approve`, `cover_letters.approve_cover_letter`,
`packages.core.services.applications.approve_answers`) after each records its
own decision. Every call re-derives the full condition from scratch - current
RESUME approved, current COVER_LETTER approved, and (only if any questions
were ever prepared) current ANSWERS approved - so whichever of the three
happens to complete the set is the one that moves the application, in
whatever order a human decides them. Approval order stops mattering by
construction, not by a coordination protocol between the three call sites.

**Review completion vs. submission readiness - two gates, one set of
checks.** This module owns `awaiting_approval -> approved`, driven directly
from each approve function's own transaction. `packages.core.services.
submission_readiness` owns `approved -> submission_ready` and beyond, driven
by `approval.granted` on the worker. They ask the same three questions -
current RESUME/COVER_LETTER/ANSWERS approved - so the checking functions live
here, once, and `submission_readiness` imports them rather than keeping a
second copy. By the time an application reaches `approved`, the set was
already known-satisfied at the moment of that transition (nothing edits a
resume, a cover letter, or an answer once `awaiting_approval` has been left),
so `submission_readiness`'s own re-check is pure defense in depth against a
future bug, not a second source of truth.

**Never a stale approval.** Each check re-derives "current" using the same
snapshot-matching each source module already exposes for its own review UI -
`resume_review.snapshot_matches`, `cover_letters.cover_letter_snapshot_matches`,
and `applications.answers_snapshot_matches` (content equality, since an
`application_answers` row can be edited after its approval, unlike an
immutable resume or cover-letter version). An approval bound to an earlier
version, or answers content that has since changed, can never complete the
set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.models import Application, ApplicationStatus, Approval, ApprovalKind, User
from packages.core.state_machines.application import transition_application
from packages.shared.clock import utc_now
from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)


async def current_resume_approval(
    session: AsyncSession, application: Application
) -> Approval | None:
    """The granted RESUME approval bound to the current resume version, if any."""
    from sqlalchemy import select

    from packages.core.models import ApprovalStatus
    from packages.core.services.resume_review import latest_tailored_resume, snapshot_matches

    resume = await latest_tailored_resume(session, application.id)
    if resume is None:
        return None
    rows = (
        await session.scalars(
            select(Approval).where(
                Approval.application_id == application.id,
                Approval.kind == ApprovalKind.RESUME,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
    ).all()
    for approval in rows:
        if snapshot_matches(approval.subject_snapshot or {}, resume):
            return approval
    return None


async def current_cover_letter_approval(
    session: AsyncSession, application: Application
) -> Approval | None:
    """The granted COVER_LETTER approval bound to the current version, if any."""
    from sqlalchemy import select

    from packages.core.models import ApprovalStatus
    from packages.core.services.cover_letters import (
        cover_letter_snapshot_matches,
        latest_cover_letter,
    )

    cover_letter = await latest_cover_letter(session, application.id)
    if cover_letter is None:
        return None
    rows = (
        await session.scalars(
            select(Approval).where(
                Approval.application_id == application.id,
                Approval.kind == ApprovalKind.COVER_LETTER,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
    ).all()
    for approval in rows:
        if cover_letter_snapshot_matches(approval.subject_snapshot or {}, cover_letter):
            return approval
    return None


async def current_answers_approval(
    session: AsyncSession, application: Application
) -> Approval | None:
    """The granted ANSWERS approval matching the current answers content, if
    any. `None` here does not by itself mean "incomplete" - the caller must
    separately check whether any questions were ever prepared at all."""
    from sqlalchemy import select

    from packages.core.models import ApprovalStatus
    from packages.core.services.applications import answers_snapshot_matches, existing_questions

    questions = await existing_questions(session, application.id)
    rows = (
        await session.scalars(
            select(Approval).where(
                Approval.application_id == application.id,
                Approval.kind == ApprovalKind.ANSWERS,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
    ).all()
    for approval in rows:
        if answers_snapshot_matches(approval.subject_snapshot or {}, application, questions):
            return approval
    return None


async def review_is_complete(session: AsyncSession, application: Application) -> bool:
    """RESUME current+approved, COVER_LETTER current+approved, and - only if
    questions were ever prepared - ANSWERS current+approved. The one
    condition both `awaiting_approval -> approved` and `approved ->
    submission_ready` gate on."""
    from packages.core.services.applications import existing_questions

    if await current_resume_approval(session, application) is None:
        return False
    if await current_cover_letter_approval(session, application) is None:
        return False
    questions = await existing_questions(session, application.id)
    if not questions:
        return True
    return await current_answers_approval(session, application) is not None


async def maybe_mark_application_approved(
    session: AsyncSession,
    application: Application,
    *,
    actor: User,
    causation_id: UUID | None = None,
) -> bool:
    """`awaiting_approval -> approved`, only when the full set is satisfied.

    Called from all three approve functions after each records its own
    decision - safe to call from any of them, in any order, including when
    the set is not yet complete (a no-op) or when the application has
    already left `awaiting_approval` (also a no-op, idempotent on replay:
    the caller's own idempotency check already short-circuits before this is
    reached a second time for the same decision, and `transition_application`
    itself refuses a transition from anywhere but `awaiting_approval`).

    `actor` is attributed as the human who completed the set - whichever
    approval happened to be the last one, not necessarily the resume's -
    matching `applications.approved_by`'s existing requirement that it name
    a human with a real granted approval of *some* kind
    (`jobforge_require_real_approval` does not care which kind).

    Returns whether it actually transitioned.
    """
    if application.status is not ApplicationStatus.AWAITING_APPROVAL:
        return False
    if not await review_is_complete(session, application):
        return False

    decided_at = utc_now()
    application.approved_by = actor.id
    application.approved_at = decided_at
    await transition_application(
        session,
        application,
        ApplicationStatus.APPROVED,
        actor_type="human",
        actor_id=str(actor.id),
        reason="resume, cover letter, and answers package (if any) all approved",
        correlation_id=application.id,
        causation_id=causation_id,
    )
    log.info(
        "review_completion.approved",
        application_id=str(application.id),
        decided_by=str(actor.id),
    )
    return True
