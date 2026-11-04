"""M13 Phase 1 read-only review and submission status.

    GET /api/v1/approvals                                    owner + reviewer
    GET /api/v1/applications/{id}/submission-readiness       owner + reviewer
    GET /api/v1/applications/{id}/submissions                owner + reviewer
    GET /api/v1/applications/{id}/cover-letter               owner + reviewer

All three are strictly read-only: each runs in a PostgreSQL `READ ONLY`
transaction, so a write anywhere in the call path is refused by the database,
not merely avoided by convention.

Readiness reuses `evaluate_submit_preconditions`, the same routine the Submit
action and therefore every execution uses, so the checklist cannot drift from
what submission actually enforces. Submit still re-validates everything at
execution time; nothing here authorizes anything.

Authentication precedes any lookup, and a missing application answers 404
with the same message as every other application route (non-enumeration).
"""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import AppSettings, CurrentUser, DbSession, authenticated_api
from apps.api.schemas.resumes import ClaimRead
from apps.api.schemas.review_status import (
    ApprovalInboxItem,
    CoverLetterContentRead,
    ReadinessCheck,
    SubmissionAttemptRead,
    SubmissionGate,
    SubmissionOutcome,
    SubmissionReadinessRead,
)
from packages.core.models import (
    Application,
    ApprovalKind,
    ApprovalStatus,
    Company,
    Event,
    Job,
    Submission,
    SubmissionResult,
)
from packages.core.models import Approval as ApprovalModel
from packages.core.services.cover_letters import latest_cover_letter
from packages.core.services.settings import submission_dispatch_enabled
from packages.core.services.submission_execution import evaluate_submit_preconditions
from packages.shared.errors import NotFoundError

router = APIRouter(tags=["review-status"], dependencies=[Depends(authenticated_api)])

_MISSING = "application not found"


async def _read_only(session: AsyncSession) -> None:
    """End the autobegun transaction, then start a READ ONLY one."""
    await session.rollback()
    await session.execute(text("SET TRANSACTION READ ONLY"))


async def _application(session: AsyncSession, application_id: UUID) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise NotFoundError(_MISSING)
    return application


# ----------------------------------------------------------------- inbox
@router.get("/approvals", response_model=list[ApprovalInboxItem])
async def approvals_inbox(
    session: DbSession,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    kind: ApprovalKind | None = None,
    application_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> list[ApprovalInboxItem]:
    """Pending approvals oldest first (most urgent); decided ones newest first."""
    await _read_only(session)
    query = (
        select(
            ApprovalModel.id,
            ApprovalModel.kind,
            ApprovalModel.status,
            ApprovalModel.requested_at,
            ApprovalModel.decided_at,
            Application.id,
            Application.status,
            Job.title,
            Company.name,
        )
        .join(Application, Application.id == ApprovalModel.application_id)
        .join(Job, Job.id == Application.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(ApprovalModel.status == status)
    )
    if kind is not None:
        query = query.where(ApprovalModel.kind == kind)
    if application_id is not None:
        query = query.where(ApprovalModel.application_id == application_id)
    order = (
        (ApprovalModel.requested_at.asc(), ApprovalModel.id.asc())
        if status is ApprovalStatus.PENDING
        else (ApprovalModel.requested_at.desc(), ApprovalModel.id.desc())
    )
    rows = (await session.execute(query.order_by(*order).limit(limit).offset(offset))).all()
    await session.rollback()
    return [
        ApprovalInboxItem(
            approval_id=row[0],
            kind=row[1],
            status=row[2],
            requested_at=row[3],
            decided_at=row[4],
            application_id=row[5],
            application_status=row[6],
            job_title=row[7],
            job_company=row[8],
        )
        for row in rows
    ]


# ------------------------------------------------------------- readiness
@router.get(
    "/applications/{application_id}/submission-readiness",
    response_model=SubmissionReadinessRead,
)
async def submission_readiness(
    application_id: UUID,
    session: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> SubmissionReadinessRead:
    is_owner = user.role == "owner"
    await _read_only(session)
    application = await _application(session, application_id)
    report = await evaluate_submit_preconditions(session, application, settings)
    persisted = await submission_dispatch_enabled(session)
    result = SubmissionReadinessRead(
        application_id=application.id,
        application_status=application.status,
        ready=report.satisfied,
        can_submit=report.satisfied and is_owner,
        gate=SubmissionGate(
            submission_mode=cast(Literal["dry_run", "enabled"], str(settings.submission_mode)),
            persisted_enabled=persisted,
            live=settings.submission_enabled and persisted,
        ),
        destination_domain=report.destination_domain,
        attempt=report.submission.attempt if report.submission is not None else None,
        checks=[
            ReadinessCheck(key=check.key, passed=check.passed, summary=check.summary)
            for check in report.checks
        ],
    )
    await session.rollback()
    return result


# --------------------------------------------------------------- history
def _outcome(submission: Submission, ambiguous: bool) -> SubmissionOutcome:
    result = submission.result
    if result is SubmissionResult.PENDING:
        return "pending"
    if result is SubmissionResult.SUCCEEDED:
        return "sent"
    if result is SubmissionResult.HANDOFF_REQUIRED:
        return "handoff_required"
    if result is SubmissionResult.BLOCKED_BY_POLICY:
        return "blocked_by_policy"
    if submission.started_at is None:
        return "preflight_blocked"
    if ambiguous:
        return "ambiguous"
    if submission.next_attempt_after is not None:
        return "retry_scheduled"
    return "failed"


@router.get(
    "/applications/{application_id}/submissions",
    response_model=list[SubmissionAttemptRead],
)
async def submission_history(
    application_id: UUID, session: DbSession
) -> list[SubmissionAttemptRead]:
    await _read_only(session)
    await _application(session, application_id)
    attempts = (
        await session.scalars(
            select(Submission)
            .where(Submission.application_id == application_id)
            .order_by(Submission.attempt.asc())
        )
    ).all()
    # Ambiguity comes from the structured M11 event flag, never from text.
    ambiguous_ids = (
        set(
            (
                await session.scalars(
                    select(Event.aggregate_id).where(
                        Event.type == "submission.failed",
                        Event.aggregate_id.in_([attempt.id for attempt in attempts]),
                        Event.payload["ambiguous"].as_boolean().is_(True),
                    )
                )
            ).all()
        )
        if attempts
        else set()
    )
    result = [
        SubmissionAttemptRead(
            submission_id=attempt.id,
            attempt=attempt.attempt,
            method=str(attempt.method),
            target_domain=attempt.target_domain,
            result=str(attempt.result),
            outcome=_outcome(attempt, attempt.id in ambiguous_ids),
            smtp_status=attempt.http_status,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            next_attempt_after=attempt.next_attempt_after,
        )
        for attempt in attempts
    ]
    await session.rollback()
    return result


# ----------------------------------------------------------- cover letter
@router.get(
    "/applications/{application_id}/cover-letter",
    response_model=CoverLetterContentRead,
)
async def current_cover_letter(application_id: UUID, session: DbSession) -> CoverLetterContentRead:
    """The latest version and its claims, so the decision is made on the text.

    The review endpoint (`.../cover-letter-review`) reports state only; this is
    the content that state describes. Read-only.
    """
    await _read_only(session)
    await _application(session, application_id)
    letter = await latest_cover_letter(session, application_id)
    if letter is None:
        raise NotFoundError("no cover letter has been generated for this application")
    result = CoverLetterContentRead(
        cover_letter_id=letter.id,
        application_id=application_id,
        version=letter.version,
        verification_status=letter.verification_status,
        created_at=letter.created_at,
        claims=[
            ClaimRead.model_validate(claim)
            for claim in sorted(letter.claims, key=lambda claim: claim.path)
        ],
    )
    await session.rollback()
    return result
