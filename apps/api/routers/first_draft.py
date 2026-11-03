"""M7 private structured import and asynchronous owner-requested first drafts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from apps.api.deps import (
    CsrfProtected,
    DbSession,
    RequireOwner,
    authenticated_api,
    rate_limited,
    require_owner,
)
from packages.core.cache.rate_limit import Limit
from packages.core.models import Event, ProcessedEvent, Resume
from packages.core.services.first_draft import request_first_draft
from packages.core.services.master_resume import MasterResumeData, import_master
from packages.shared.config import Settings
from packages.shared.errors import NotFoundError

router = APIRouter(tags=["first-draft"], dependencies=[Depends(authenticated_api)])


def first_draft_limit(settings: Settings) -> Limit:
    return Limit(
        "first-draft",
        settings.rate_limit_revision_per_user,
        settings.rate_limit_revision_window_seconds,
    )


class FirstDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID
    master_resume_id: UUID
    evaluation_id: UUID


@router.post("/profiles/{profile_id}/master-resumes", status_code=201)
async def import_master_resume(
    profile_id: UUID,
    payload: MasterResumeData,
    session: DbSession,
    owner: RequireOwner,
    _csrf: CsrfProtected,
) -> dict[str, object]:
    resume = await import_master(session, actor=owner, profile_id=profile_id, data=payload)
    await session.commit()
    return {
        "resume_id": str(resume.id),
        "profile_id": str(resume.profile_id),
        "version": resume.version,
        "kind": "base",
    }


@router.post(
    "/jobs/{job_id}/first-draft",
    status_code=202,
    dependencies=[Depends(require_owner), Depends(rate_limited(first_draft_limit))],
)
async def queue_first_draft(
    job_id: UUID,
    payload: FirstDraftInput,
    session: DbSession,
    owner: RequireOwner,
    _csrf: CsrfProtected,
) -> dict[str, str]:
    result = await request_first_draft(
        session,
        actor=owner,
        job_id=job_id,
        profile_id=payload.profile_id,
        master_id=payload.master_resume_id,
        evaluation_id=payload.evaluation_id,
    )
    await session.commit()
    return {
        "application_id": str(result.application_id),
        "event_id": str(result.event_id),
        "profile_id": str(result.profile_id),
    }


@router.get("/applications/{application_id}/first-draft")
async def first_draft_status(application_id: UUID, session: DbSession) -> dict[str, object]:
    event = await session.scalar(
        select(Event)
        .where(
            Event.aggregate_id == application_id,
            Event.type == "resume.first_draft.requested",
        )
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(1)
    )
    if event is None:
        raise NotFoundError("first draft not found")
    processed = await session.get(ProcessedEvent, (event.id, "cg.first-draft"))
    resume = await session.scalar(
        select(Resume)
        .where(
            Resume.application_id == application_id,
            Resume.diff_vs_base["first_draft_event_id"].astext == str(event.id),
        )
        .limit(1)
    )
    status = "completed" if resume else "failed" if processed else "pending"
    return {
        "application_id": str(application_id),
        "event_id": str(event.id),
        "status": status,
        "resume_id": str(resume.id) if resume else None,
    }
