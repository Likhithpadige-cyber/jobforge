"""M10 Phase 1 - cover-letter review and rework endpoints.

    GET  /api/v1/applications/{id}/cover-letter-review
    POST /api/v1/applications/{id}/cover-letter-review/approve
    POST /api/v1/applications/{id}/cover-letter-review/request-changes

Mirrors `apps/api/routers/resume_review.py`'s shape: thin handlers that
establish the human from the session, delegate to
`packages.core.services.cover_letters`, and commit once.

**No "open review" or "generate" route.** Unlike resume review, a cover
letter needs no owner-supplied input before it can be drafted, so generation
- and opening its approval once verification passes - happens automatically
(triggered by `resume_review.enter_review` and, on rework, by the
request-changes handler below), not from a separate human action this API
exposes. Exposing a manual "generate" endpoint here would be surface added
for convenience, not because the lifecycle needs it.

Both routes that take an identifier check authentication before any lookup
and answer a missing/inaccessible resource identically to a nonexistent one,
the same non-enumeration discipline `apps/api/routers/artifacts.py`
documents - `authenticated_api` plus owner-or-reviewer access to the
application's own resources, matching every other application review route.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from apps.api.deps import (
    CsrfProtected,
    CurrentHumanActor,
    DbSession,
    authenticated_api,
    cover_letter_revision_limit,
    rate_limited,
)
from apps.api.schemas.cover_letter_review import (
    ApproveRequest,
    CoverLetterReviewRead,
    RequestChangesRequest,
)
from packages.core.services import cover_letters as service

router = APIRouter(
    prefix="/applications",
    tags=["cover-letter-review"],
    dependencies=[Depends(authenticated_api)],
)

REVIEW = "/{application_id}/cover-letter-review"


@router.get(
    REVIEW,
    response_model=CoverLetterReviewRead,
    summary="Review state of the current cover-letter version",
)
async def get_cover_letter_review(application_id: UUID, session: DbSession) -> Any:
    """Read-only. Says what has been decided and what may be done next."""
    return CoverLetterReviewRead.from_state(
        await service.cover_letter_review_state(session, application_id)
    )


@router.post(
    f"{REVIEW}/approve",
    response_model=CoverLetterReviewRead,
    summary="Approve the cover-letter version under review",
)
async def approve_cover_letter(
    application_id: UUID,
    payload: ApproveRequest,
    session: DbSession,
    user: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """Records the decision. Does not move `applications.status`."""
    state = await service.approve_cover_letter(
        session,
        application_id=application_id,
        actor=user,
        decision_note=payload.decision_note,
    )
    await session.commit()
    return CoverLetterReviewRead.from_state(state)


@router.post(
    f"{REVIEW}/request-changes",
    response_model=CoverLetterReviewRead,
    summary="Send the cover letter back with instructions",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limited(cover_letter_revision_limit))],
)
async def request_cover_letter_changes(
    application_id: UUID,
    payload: RequestChangesRequest,
    session: DbSession,
    user: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    """Records the decision and asks the worker to regenerate.

    Unlike resume rework's request-changes route, this handler commits
    normally: the service only records a decision and emits an event in the
    caller's transaction (mirroring `application.questions.requested`'s own
    request/result split) - it does not run the LLM call inline, so there is
    no separate durable-attempt boundary for the router to defer to.
    """
    state = await service.request_changes_cover_letter(
        session, application_id=application_id, actor=user, note=payload.note
    )
    await session.commit()
    return CoverLetterReviewRead.from_state(state)
