"""Resume Studio read endpoints.

Strictly read-only. These endpoints display what B6 persisted; none of them
generates, verifies, re-verifies or mutates anything. A GET that produced a
resume would write rows, move the application and emit events, which is not
something a read should ever do.

A failed resume is returned exactly like a passing one. Hiding it would defeat
the point: the whole reason B6 persists a failed verification instead of
discarding it is so a human can read the text that failed and see why.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from apps.api.deps import DbSession, authenticated_api
from apps.api.schemas.resume_review import ResumeReviewRead
from apps.api.schemas.resumes import (
    ApplicationRead,
    ClaimRead,
    ResumeRead,
    VerdictCounts,
)
from packages.core.models import (
    Application,
    ClaimVerdict,
    Company,
    Job,
    Resume,
    ResumeClaim,
)
from packages.core.services import resume_review
from packages.shared.errors import NotFoundError

router = APIRouter(prefix="/resumes", tags=["resumes"], dependencies=[Depends(authenticated_api)])
applications_router = APIRouter(
    prefix="/applications", tags=["resumes"], dependencies=[Depends(authenticated_api)]
)

#: Sections in the order a resume reads, not the order rows come back in.
#: Anything not listed is appended alphabetically, so an unexpected section
#: still renders rather than vanishing.
SECTION_ORDER = (
    "summary",
    "experience",
    "projects",
    "skills",
    "certifications",
    "education",
)


def _counts(claims: list[ResumeClaim]) -> VerdictCounts:
    """Tally verdicts from the rows themselves."""
    tally = VerdictCounts(total=len(claims))
    for claim in claims:
        if claim.verdict is ClaimVerdict.SUPPORTED:
            tally.supported += 1
        elif claim.verdict is ClaimVerdict.OVERREACH:
            tally.overreach += 1
        elif claim.verdict is ClaimVerdict.UNSUPPORTED:
            tally.unsupported += 1
        else:
            tally.not_checked += 1
    return tally


def _sort_key(claim: ResumeClaim) -> tuple[int, int, str]:
    """Deterministic claim order: section order, then numeric path index."""
    try:
        section_rank = SECTION_ORDER.index(claim.section)
    except ValueError:
        section_rank = len(SECTION_ORDER)
    tail = claim.path.rsplit("/", 1)[-1]
    return (section_rank, int(tail) if tail.isdigit() else 0, claim.path)


async def _job_context(
    session: DbSession, application: Application | None
) -> tuple[UUID | None, str | None, str | None]:
    """Job title and company name for the header - identifiers and names only."""
    if application is None:
        return None, None, None
    row = (
        await session.execute(
            select(Job.id, Job.title, Company.name)
            .select_from(Job)
            .outerjoin(Company, Job.company_id == Company.id)
            .where(Job.id == application.job_id)
        )
    ).first()
    if row is None:
        return application.job_id, None, None
    return row[0], row[1], row[2]


async def _review(session: DbSession, resume: Resume) -> ResumeReviewRead | None:
    """C2 review state, but only when `resume` is the current tailored version.

    Still a read: `review_state` opens no lock, writes nothing and emits
    nothing. It is attached here so the studio can render the resume and its
    approval from one response instead of racing two requests whose answers
    could disagree.

    An older version returns null rather than the live review. A reviewer
    looking at superseded text should not be shown - let alone offered - a
    decision about the text that replaced it.
    """
    if resume.application_id is None:
        return None
    latest = await resume_review.latest_tailored_resume(session, resume.application_id)
    if latest is None or latest.id != resume.id:
        return None
    return ResumeReviewRead.from_state(
        await resume_review.review_state(session, resume.application_id)
    )


async def _read_model(session: DbSession, resume: Resume) -> ResumeRead:
    claims = sorted(resume.claims, key=_sort_key)
    application = None
    if resume.application_id is not None:
        application = (
            await session.execute(
                select(Application).where(Application.id == resume.application_id)
            )
        ).scalar_one_or_none()
    job_id, job_title, job_company = await _job_context(session, application)
    return ResumeRead(
        id=resume.id,
        application_id=resume.application_id,
        version=resume.version,
        kind=resume.kind,
        verification_status=resume.verification_status,
        summary_text=resume.summary_text,
        content=resume.content,
        created_at=resume.created_at,
        model=resume.model,
        prompt_version=resume.prompt_version,
        application_status=application.status if application else None,
        job_id=job_id,
        job_title=job_title,
        job_company=job_company,
        claims=[ClaimRead.model_validate(claim) for claim in claims],
        counts=_counts(claims),
        review=await _review(session, resume),
    )


@router.get("/{resume_id}", response_model=ResumeRead, summary="One persisted resume")
async def get_resume(resume_id: UUID, session: DbSession) -> Any:
    resume = (
        await session.execute(
            select(Resume)
            .options(selectinload(Resume.claims))
            .where(Resume.id == resume_id)
        )
    ).scalar_one_or_none()
    if resume is None:
        raise NotFoundError(f"resumes {resume_id} not found")
    return await _read_model(session, resume)


@applications_router.get(
    "",
    response_model=list[ApplicationRead],
    summary="Applications, newest first",
)
async def list_applications(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """The list the studio opens from, so no one has to type a UUID.

    Deliberately small: enough to identify an application and say whether it
    has a resume worth opening.
    """
    applications = (
        (
            await session.execute(
                select(Application)
                .order_by(Application.created_at.desc(), Application.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    out: list[ApplicationRead] = []
    for application in applications:
        summary = (
            await session.execute(
                select(
                    func.count(Resume.id),
                    func.max(Resume.version),
                ).where(
                    Resume.application_id == application.id, Resume.kind == "tailored"
                )
            )
        ).first()
        count, latest_version = (summary or (0, None))[0], (summary or (0, None))[1]
        latest_status = None
        if latest_version is not None:
            latest_status = (
                await session.execute(
                    select(Resume.verification_status).where(
                        Resume.application_id == application.id,
                        Resume.kind == "tailored",
                        Resume.version == latest_version,
                    )
                )
            ).scalar_one_or_none()
        job_id, job_title, job_company = await _job_context(session, application)
        out.append(
            ApplicationRead(
                id=application.id,
                job_id=job_id or application.job_id,
                status=application.status,
                created_at=application.created_at,
                updated_at=application.updated_at,
                job_title=job_title,
                job_company=job_company,
                resume_count=count or 0,
                latest_resume_version=latest_version,
                latest_verification_status=latest_status,
            )
        )
    return out


@applications_router.get(
    "/{application_id}/resumes/latest",
    response_model=ResumeRead,
    status_code=status.HTTP_200_OK,
    summary="Latest tailored resume for an application",
)
async def latest_resume(application_id: UUID, session: DbSession) -> Any:
    """The highest tailored version for this application.

    A `failed` resume is returned like any other: it is the latest attempt,
    and its text is exactly what a reviewer needs to see.

    404 when the application does not exist, and 404 when it exists but has
    never had a resume generated - distinguished by the message rather than by
    the status code, matching the rest of the API.
    """
    application = (
        await session.execute(
            select(Application).where(Application.id == application_id)
        )
    ).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"applications {application_id} not found")

    resume = (
        await session.execute(
            select(Resume)
            .options(selectinload(Resume.claims))
            .where(Resume.application_id == application_id, Resume.kind == "tailored")
            .order_by(Resume.version.desc(), Resume.created_at.desc(), Resume.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if resume is None:
        raise NotFoundError(f"no resume has been generated for {application_id}")
    return await _read_model(session, resume)
