"""M19 application tracking: outcome, applied date, follow-up date, notes.

Reads are shared with reviewers (the same review access as the rest of an
application); writes are owner-only and CSRF-protected. Nothing here moves
the pipeline status or sends anything - see
packages/core/services/application_tracking.py.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from apps.api.deps import (
    CsrfProtected,
    CurrentHumanActor,
    DbSession,
    authenticated_api,
    require_owner,
)
from apps.api.schemas.tracking import FollowUpRead, TrackingRead, TrackingUpdate
from packages.core.models import Application
from packages.core.services.application_tracking import due_follow_ups, update_tracking
from packages.shared.clock import utc_today
from packages.shared.errors import NotFoundError

router = APIRouter(tags=["tracking"], dependencies=[Depends(authenticated_api)])


def _read(application: Application) -> TrackingRead:
    return TrackingRead(
        application_id=application.id,
        status=str(application.status),
        outcome=application.outcome,
        applied_on=application.applied_on,
        follow_up_on=application.follow_up_on,
        notes=application.notes,
        submitted_at=application.submitted_at,
        outcome_updated_at=application.outcome_updated_at,
    )


@router.get(
    "/applications/{application_id}/tracking",
    response_model=TrackingRead,
    summary="Owner-recorded outcome, dates and notes",
)
async def get_tracking(application_id: UUID, session: DbSession) -> Any:
    application = await session.get(Application, application_id)
    if application is None:
        raise NotFoundError("application not found")
    return _read(application)


@router.patch(
    "/applications/{application_id}/tracking",
    response_model=TrackingRead,
    summary="Record an outcome, applied date, follow-up date or notes",
    dependencies=[Depends(require_owner)],
)
async def patch_tracking(
    application_id: UUID,
    payload: TrackingUpdate,
    session: DbSession,
    owner: CurrentHumanActor,
    _csrf: CsrfProtected,
) -> Any:
    application = await update_tracking(
        session,
        actor=owner,
        application_id=application_id,
        changes=payload.model_dump(include=payload.model_fields_set),
    )
    await session.commit()
    return _read(application)


@router.get(
    "/follow-ups",
    response_model=list[FollowUpRead],
    summary="Open applications with a follow-up due",
)
async def list_follow_ups(
    session: DbSession,
    on: Annotated[date | None, Query(description="The caller's today; defaults to UTC")] = None,
    within_days: Annotated[int, Query(ge=0, le=60)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Any:
    rows = await due_follow_ups(session, on=on or utc_today(), within_days=within_days, limit=limit)
    return [FollowUpRead(**asdict(row)) for row in rows]
