"""Job-domain repositories."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from packages.core.models import (
    Company,
    Job,
    JobEvaluation,
    JobStatus,
    SitePolicy,
    Source,
)
from packages.core.repositories.base import BaseRepository
from packages.shared.clock import utc_now


class SitePolicyRepository(BaseRepository[SitePolicy]):
    model = SitePolicy

    async def get_by_domain(self, domain: str) -> SitePolicy | None:
        stmt = select(SitePolicy).where(SitePolicy.domain == domain.lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()


class SourceRepository(BaseRepository[Source]):
    model = Source

    async def get_by_slug(self, slug: str) -> Source | None:
        stmt = select(Source).where(Source.slug == slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_enabled(self) -> list[Source]:
        stmt = select(Source).where(Source.enabled.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())


class CompanyRepository(BaseRepository[Company]):
    model = Company

    async def get_or_create(self, name: str, normalized_name: str) -> Company:
        stmt = select(Company).where(Company.normalized_name == normalized_name)
        company = (await self.session.execute(stmt)).scalar_one_or_none()
        if company is not None:
            return company
        return await self.create(name=name, normalized_name=normalized_name)


class JobRepository(BaseRepository[Job]):
    model = Job

    async def get_by_source_external_id(self, source_id: UUID, external_id: str) -> Job | None:
        stmt = select(Job).where(Job.source_id == source_id, Job.external_id == external_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def dedupe_candidates(
        self,
        *,
        company_id: UUID | None,
        description_hash: str | None,
        source_id: UUID | None = None,
        window_days: int = 120,
    ) -> list[Job]:
        """Narrow the comparison set before running the dedupe strategies.

        Comparing a new posting against every job ever seen would be both slow
        and prone to false matches on long-closed roles.
        """
        since = utc_now() - timedelta(days=window_days)
        clauses = [Job.first_seen_at >= since, Job.status != JobStatus.DUPLICATE]
        stmt = select(Job).where(*clauses)

        if company_id is not None and description_hash is not None:
            stmt = stmt.where(
                (Job.company_id == company_id) | (Job.description_hash == description_hash)
            )
        elif company_id is not None:
            stmt = stmt.where(Job.company_id == company_id)
        elif description_hash is not None:
            stmt = stmt.where(Job.description_hash == description_hash)
        elif source_id is not None:
            stmt = stmt.where(Job.source_id == source_id)

        return list((await self.session.execute(stmt.limit(500))).scalars().all())

    async def list_by_status(
        self, status: JobStatus, limit: int = 100, offset: int = 0
    ) -> list[Job]:
        stmt = (
            select(Job)
            .where(Job.status == status)
            .order_by(Job.priority.desc(), Job.posted_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self) -> dict[str, int]:
        stmt = select(Job.status, func.count()).group_by(Job.status)
        rows = (await self.session.execute(stmt)).all()
        counts = {s.value: 0 for s in JobStatus}
        for status, count in rows:
            counts[str(status)] = count
        return counts

    async def mark_duplicate(self, job: Job, canonical_id: UUID, reason: str) -> Job:
        job.status = JobStatus.DUPLICATE
        job.duplicate_of_id = canonical_id
        job.discard_reason = reason
        await self.session.flush()
        return job

    async def touch_seen(self, job: Job) -> Job:
        """Record that a still-live posting was seen again."""
        job.last_seen_at = utc_now()
        await self.session.flush()
        return job


class JobEvaluationRepository(BaseRepository[JobEvaluation]):
    """Read access to persisted evaluations.

    Read-only by intent: `job_evaluations` is append-only (the
    trg_job_evaluations_append_only trigger rejects UPDATE and DELETE), and
    evaluations are written solely by A5. Nothing here computes a score.
    """

    model = JobEvaluation

    async def latest_for_job(self, job_id: UUID) -> JobEvaluation | None:
        """The most recent evaluation, or None if the job has never been scored.

        Ordered by created_at then id: re-evaluation appends a new row rather
        than overwriting (DATABASE_DESIGN.md §5.6), and two rows written in
        the same transaction share a timestamp. The id is UUIDv7 and therefore
        time-ordered, which breaks that tie in insertion order.
        """
        stmt = (
            select(JobEvaluation)
            .where(JobEvaluation.job_id == job_id)
            .order_by(JobEvaluation.created_at.desc(), JobEvaluation.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_job(self, job_id: UUID, limit: int = 20) -> list[JobEvaluation]:
        """Full history, newest first."""
        stmt = (
            select(JobEvaluation)
            .where(JobEvaluation.job_id == job_id)
            .order_by(JobEvaluation.created_at.desc(), JobEvaluation.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())
