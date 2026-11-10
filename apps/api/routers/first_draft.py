"""M7 private structured import and asynchronous owner-requested first drafts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
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
from packages.core.models import Event, ProcessedEvent, Profile, Resume
from packages.core.services.first_draft import request_first_draft
from packages.core.services.master_resume import MasterResumeData, import_master
from packages.core.services.resume_text_extraction import (
    DOCX_MIME,
    MAX_DOCX_BYTES,
    ExtractionError,
    extract_docx_text,
)
from packages.shared.config import Settings
from packages.shared.errors import NotFoundError, ValidationError

router = APIRouter(tags=["first-draft"], dependencies=[Depends(authenticated_api)])


def first_draft_limit(settings: Settings) -> Limit:
    return Limit(
        "first-draft",
        settings.rate_limit_revision_per_user,
        settings.rate_limit_revision_window_seconds,
    )


def resume_extract_limit(settings: Settings) -> Limit:
    return Limit(
        "resume-extract",
        settings.rate_limit_import_per_user,
        settings.rate_limit_import_window_seconds,
    )


class FirstDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID
    master_resume_id: UUID
    evaluation_id: UUID


@router.get("/profiles/{profile_id}/master-resumes/latest")
async def latest_master_resume(
    profile_id: UUID, session: DbSession, _owner: RequireOwner
) -> dict[str, object]:
    """The newest imported master resume for a profile.

    Owner-only, matching `POST /profiles/{id}/master-resumes` and the
    approved shared-workspace boundary (M7). An unknown profile and "none
    imported yet" give the same 404. Contact-bearing exports keep their
    stricter per-profile-owner check (M16.6).
    """
    resume = await session.scalar(
        select(Resume)
        .where(Resume.profile_id == profile_id, Resume.kind == "base")
        .order_by(Resume.version.desc())
        .limit(1)
    )
    if resume is None:
        raise NotFoundError("no master resume has been imported for this profile")
    content = resume.content or {}
    return {
        "resume_id": str(resume.id),
        "version": resume.version,
        "created_at": resume.created_at.isoformat(),
        "source_label": content.get("source_label"),
    }


@router.post(
    "/profiles/{profile_id}/master-resumes/extract-text",
    dependencies=[Depends(rate_limited(resume_extract_limit))],
)
async def extract_master_resume_text(
    profile_id: UUID,
    request: Request,
    session: DbSession,
    _owner: RequireOwner,
    _csrf: CsrfProtected,
) -> dict[str, object]:
    """Owner-only: plain text from a DOCX for the editable import preview.

    Stores nothing. The body is the raw .docx (Content-Type DOCX); size and
    archive limits are enforced before parsing. PDF is not accepted here.
    """
    if await session.get(Profile, profile_id) is None:
        raise NotFoundError("profile not found")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != DOCX_MIME:
        raise ValidationError("only .docx files can be read here; paste PDF text instead")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_DOCX_BYTES:
        raise ExtractionError("the file is larger than 2 MB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_DOCX_BYTES:
            raise ExtractionError("the file is larger than 2 MB")
    text = extract_docx_text(bytes(body))
    return {"text": text, "lines": text.count("\n") + 1, "source": "docx"}


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
