"""Job ingestion and browsing endpoints.

Phase 3 exposes import and read only. There is no endpoint that fetches a URL:
every posting arrives from content a human already had, and the policy gate
still applies to the source and to each posting's domain.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from apps.api.deps import (
    DbSession,
    authenticated_api,
    import_limit,
    rate_limited,
    require_owner,
)
from apps.api.schemas.jobs import (
    EvaluationRead,
    ImportRequest,
    ImportResponse,
    JobDetail,
    JobRead,
    JobStatusCounts,
    SourceCreate,
    SourceRead,
)
from packages.core.models import JobStatus
from packages.core.repositories import (
    JobEvaluationRepository,
    JobRepository,
    SourceRepository,
)
from packages.services.ingestion import IngestionPipeline
from packages.services.ingestion.connectors import parse_csv, parse_json
from packages.shared.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(authenticated_api)])
sources_router = APIRouter(
    prefix="/sources", tags=["jobs"], dependencies=[Depends(authenticated_api)]
)


@router.post(
    "/import",
    response_model=ImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import postings from pasted CSV or JSON",
    dependencies=[Depends(require_owner), Depends(rate_limited(import_limit))],
)
async def import_jobs(payload: ImportRequest, session: DbSession) -> Any:
    postings = (
        parse_csv(payload.content)
        if payload.format == "csv"
        else parse_json(payload.content)
    )
    pipeline = IngestionPipeline(session)
    report = await pipeline.ingest_batch(
        payload.source_slug, postings, max_age_days=payload.max_age_days
    )
    await session.commit()
    return ImportResponse(
        source_slug=report.source_slug,
        total=report.total,
        created=report.created,
        duplicates=report.duplicates,
        updated=report.updated,
        rejected=report.rejected,
        outcomes=report.outcomes,
    )


@router.get("", response_model=list[JobRead], summary="List jobs")
async def list_jobs(
    session: DbSession,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    repo = JobRepository(session)
    if job_status is not None:
        return await repo.list_by_status(job_status, limit=limit, offset=offset)
    return await repo.list(limit=limit, offset=offset)


@router.get("/counts", response_model=JobStatusCounts, summary="Job counts by status")
async def job_counts(session: DbSession) -> Any:
    counts = await JobRepository(session).counts_by_status()
    return JobStatusCounts(counts=counts, total=sum(counts.values()))


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, session: DbSession) -> Any:
    return await JobRepository(session).get_or_404(job_id)


@router.get(
    "/{job_id}/evaluation",
    response_model=EvaluationRead,
    summary="Latest persisted evaluation for a job",
)
async def get_job_evaluation(job_id: UUID, session: DbSession) -> Any:
    """Return the most recent evaluation, if one exists.

    Strictly read-only. This does not score the job: evaluations are written
    by A5 and this endpoint reads what is already there. A GET that computed
    an evaluation would write a row, move the job's status and emit events,
    which is not something a read should ever do.

    404 when the job does not exist, and 404 when it exists but has never
    been evaluated - the two are distinguished by the message rather than by
    the status code, matching how the rest of the API reports a missing
    resource.
    """
    await JobRepository(session).get_or_404(job_id)

    evaluation = await JobEvaluationRepository(session).latest_for_job(job_id)
    if evaluation is None:
        raise NotFoundError(f"job {job_id} has not been evaluated yet")
    return evaluation


# ------------------------------------------------------------------ sources
@sources_router.get(
    "",
    response_model=list[SourceRead],
    dependencies=[Depends(require_owner)],
)
async def list_sources(session: DbSession) -> Any:
    return await SourceRepository(session).list(limit=200)


@sources_router.post(
    "",
    response_model=SourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a source (disabled by default)",
    dependencies=[Depends(require_owner)],
)
async def create_source(payload: SourceCreate, session: DbSession) -> Any:
    repo = SourceRepository(session)
    if await repo.get_by_slug(payload.slug) is not None:
        raise ValidationError(f"source {payload.slug!r} already exists")
    # `enabled` is deliberately not settable here. A source is switched on
    # only after a human records a ToS review against its domain.
    source = await repo.create(**payload.model_dump(), enabled=False)
    await session.commit()
    return source
