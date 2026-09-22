"""C2 - resume review and approval endpoints.

    GET  /api/v1/applications/{id}/resume-review
    POST /api/v1/applications/{id}/resume-review
    POST /api/v1/applications/{id}/resume-review/approve
    POST /api/v1/applications/{id}/resume-review/request-changes

The sub-resource shape mirrors the existing `/applications/{id}/resumes/latest`
convention: the application owns the review, so the review hangs off it.

These routes are thin on purpose. Each one does three things - establish the
human from the session, delegate to `packages.core.services.resume_review`, and
commit - and every rule about what may be approved lives in the service, where
it can be tested without a request.

**Identity.** `CurrentHumanActor` resolves the signed-in user from the
server-side session record. It is the only source of the reviewer's id here;
the request bodies have no field that could name one, and `extra="forbid"`
rejects an attempt to add one. `CsrfProtected` is attached to every mutating
route, so a cross-origin page cannot make the browser approve anything on the
user's behalf.

**Transactions.** The service performs the whole unit of work - approval row,
application status, history row and outbox event - without committing. These
handlers commit exactly once, at the end, so a refused precondition leaves the
database untouched.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from apps.api.deps import (
    CsrfProtected,
    CurrentHumanActor,
    DbSession,
    RevisionClient,
    authenticated_api,
    rate_limited,
    revision_limit,
)
from apps.api.schemas.resume_review import (
    ApproveRequest,
    RequestChangesRequest,
    ResumeReviewRead,
    ReviewRequest,
)
from packages.core.services import resume_review as service
from packages.core.services.resume_rework import request_changes_and_regenerate

router = APIRouter(
    prefix="/applications",
    tags=["resume-review"],
    dependencies=[Depends(authenticated_api)],
)

REVIEW = "/{application_id}/resume-review"


@router.get(
    REVIEW,
    response_model=ResumeReviewRead,
    summary="Review state of the current resume",
)
async def get_resume_review(application_id: UUID, session: DbSession) -> Any:
    """Read-only. Says what has been decided and what may be done next."""
    return ResumeReviewRead.from_state(await service.review_state(session, application_id))


@router.post(
    REVIEW,
    response_model=ResumeReviewRead,
    status_code=status.HTTP_200_OK,
    summary="Open human review of the current resume",
)
async def start_resume_review(
    application_id: UUID,
    payload: ReviewRequest,
    session: DbSession,
    user: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """`resume_ready -> awaiting_approval`, with a pending approval bound to
    the exact resume id and version on display.

    200 rather than 201 on purpose: a retry is idempotent and returns the same
    review, so this is not reliably a creation.
    """
    state = await service.enter_review(session, application_id=application_id, actor=user)
    await session.commit()
    return ResumeReviewRead.from_state(state)


@router.post(
    f"{REVIEW}/approve",
    response_model=ResumeReviewRead,
    summary="Approve the resume under review",
)
async def approve_resume(
    application_id: UUID,
    payload: ApproveRequest,
    session: DbSession,
    user: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """`awaiting_approval -> approved`, decided by the signed-in human."""
    state = await service.approve(
        session,
        application_id=application_id,
        actor=user,
        decision_note=payload.decision_note,
    )
    await session.commit()
    return ResumeReviewRead.from_state(state)


@router.post(
    f"{REVIEW}/request-changes",
    response_model=ResumeReviewRead,
    summary="Send the resume back with instructions",
    dependencies=[Depends(rate_limited(revision_limit))],
)
async def request_resume_changes(
    application_id: UUID,
    payload: RequestChangesRequest,
    session: DbSession,
    user: CurrentHumanActor,
    _csrf: CsrfProtected,
    client: RevisionClient,
) -> Any:
    """Record the human decision, then regenerate against a durable attempt.

    **This handler does not commit**, unlike every other route in this module.
    The decision and the revision are no longer one unit of work: the service
    owns T1-T5 explicitly (see `packages.core.services.resume_rework`) because
    a failed generation must leave the decision and the attempt record behind
    rather than erasing them. Committing again here would be a seventh,
    meaningless boundary on an already-committed session.

    A successful revision needs fresh human review; verification failure is
    persisted without opening approval. Neither inherits approval.
    """
    state = await request_changes_and_regenerate(
        session,
        application_id=application_id,
        approval_id=payload.approval_id,
        actor=user,
        note=payload.note,
        client=client,
    )
    return ResumeReviewRead.from_state(state)
