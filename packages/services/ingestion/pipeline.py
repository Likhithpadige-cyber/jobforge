"""Ingestion pipeline: policy gate -> normalize -> dedupe -> persist -> emit.

Deterministic throughout. No LLM call happens anywhere in this path, and none
should: the point of doing this work cheaply is that only surviving, deduped
postings ever reach the evaluator.

Job status walks `discovered -> normalized` here, or terminates at
`duplicate`. Evaluation (Phase 4) picks up from `normalized`.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.agents.resume_optimizer.requirements import extract_requirements
from packages.core.events import emit
from packages.core.models import IngestionMethod, Job, JobStatus, Source
from packages.core.repositories import (
    CompanyRepository,
    JobRepository,
    SitePolicyRepository,
    SourceRepository,
)
from packages.services.ingestion.dedupe import JobLike, find_duplicate
from packages.services.ingestion.normalize import NormalizedJob, is_stale, normalize_job
from packages.services.ingestion.policy import (
    PolicySnapshot,
    blocked_policy,
    evaluate_ingestion,
    extract_domain,
)
from packages.services.ingestion.types import (
    IngestOutcome,
    IngestReport,
    RawPosting,
)
from packages.shared.errors import PolicyBlockedError, ValidationError
from packages.shared.ids import uuid7
from packages.shared.logging import get_logger

log = get_logger(__name__)


def snapshot_policy(policy_row: object | None, domain: str) -> PolicySnapshot:
    """Build a decision input from a site_policies row, or the blocked default."""
    if policy_row is None:
        return blocked_policy(domain)
    return PolicySnapshot(
        domain=policy_row.domain,  # type: ignore[attr-defined]
        automation_allowed=policy_row.automation_allowed,  # type: ignore[attr-defined]
        ingestion_method=policy_row.ingestion_method,  # type: ignore[attr-defined]
        submission_method=policy_row.submission_method,  # type: ignore[attr-defined]
        max_requests_per_hour=policy_row.max_requests_per_hour,  # type: ignore[attr-defined]
        requires_login=policy_row.requires_login,  # type: ignore[attr-defined]
        reviewed_at=policy_row.reviewed_at,  # type: ignore[attr-defined]
        next_review_due=policy_row.next_review_due,  # type: ignore[attr-defined]
        exists=True,
    )


class IngestionPipeline:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.jobs = JobRepository(session)
        self.sources = SourceRepository(session)
        self.companies = CompanyRepository(session)
        self.policies = SitePolicyRepository(session)

    # -------------------------------------------------------------- gate
    async def check_policy(
        self, source: Source, method: IngestionMethod, url: str | None = None
    ) -> None:
        """Raise PolicyBlockedError unless this ingestion is permitted.

        Checked per source AND per posting URL, because a source can legally
        list jobs whose apply URLs point at domains we have not reviewed.
        """
        domain = extract_domain(url) if url else source.domain
        policy_row = await self.policies.get_by_domain(domain)
        verdict = evaluate_ingestion(
            snapshot_policy(policy_row, domain), method, source_enabled=source.enabled
        )
        if not verdict.permitted:
            log.warning(
                "ingestion.policy.blocked",
                domain=domain,
                reason=str(verdict.reason),
                source=source.slug,
            )
            raise PolicyBlockedError(verdict.detail, domain=domain, reason=str(verdict.reason))

    # ---------------------------------------------------------- one item
    async def ingest_one(
        self, source: Source, raw: RawPosting, *, max_age_days: int = 60
    ) -> IngestOutcome:
        try:
            normalized = normalize_job(
                title=raw.title,
                url=raw.url,
                company_name=raw.company_name,
                external_id=raw.external_id,
                location=raw.location,
                description=raw.description,
                salary_text=raw.salary_text,
                posted_at=raw.posted_at,
                expires_at=raw.expires_at,
            )
        except ValueError as exc:
            return IngestOutcome(raw.url, "rejected", reason=str(exc))

        if is_stale(normalized.posted_at, max_age_days):
            return IngestOutcome(
                raw.url,
                "rejected",
                reason=f"posting older than {max_age_days} days",
                warnings=normalized.warnings,
            )

        # A posting whose apply URL leaves the reviewed domain is not ingested.
        posting_domain = extract_domain(normalized.url)
        if posting_domain and posting_domain != source.domain:
            policy_row = await self.policies.get_by_domain(posting_domain)
            verdict = evaluate_ingestion(
                snapshot_policy(policy_row, posting_domain), source.ingestion_method
            )
            if not verdict.permitted:
                return IngestOutcome(
                    raw.url,
                    "rejected",
                    reason=f"posting domain {posting_domain} is not permitted: "
                    f"{verdict.reason}",
                    warnings=normalized.warnings,
                )

        # Same source + same external id = the same posting seen again.
        if normalized.external_id:
            existing = await self.jobs.get_by_source_external_id(source.id, normalized.external_id)
            if existing is not None:
                await self.jobs.touch_seen(existing)
                return IngestOutcome(
                    raw.url,
                    "updated",
                    job_id=existing.id,
                    reason="already seen from this source",
                    warnings=normalized.warnings,
                )

        company_id: UUID | None = None
        if normalized.company_name and normalized.normalized_company:
            company = await self.companies.get_or_create(
                normalized.company_name, normalized.normalized_company
            )
            company_id = company.id

        job = await self._persist(source, normalized, company_id)

        candidates = await self.jobs.dedupe_candidates(
            company_id=company_id,
            description_hash=normalized.description_hash,
            source_id=source.id,
        )
        # SQLAlchemy exposes scalar values on instances; structural matching
        # instead sees the model's Mapped descriptors. Job's seven fields match
        # JobLike, so annotate this ORM boundary without copying/changing rows.
        match = find_duplicate(
            cast(JobLike, job),
            [cast(JobLike, c) for c in candidates if c.id != job.id],
        )
        if match is not None:
            await self.jobs.mark_duplicate(job, match.canonical_id, match.note)
            return IngestOutcome(
                raw.url,
                "duplicate",
                job_id=job.id,
                canonical_id=match.canonical_id,
                reason=f"{match.reason}: {match.note}",
                warnings=normalized.warnings,
            )

        job.status = JobStatus.NORMALIZED
        await self.session.flush()
        await emit(
            self.session,
            type="job.normalized",
            aggregate_type="job",
            aggregate_id=job.id,
            correlation_id=job.id,
            actor_type="system",
            actor_id="ingestion",
            payload={"job_id": str(job.id)},
        )
        return IngestOutcome(raw.url, "created", job_id=job.id, warnings=normalized.warnings)

    async def _persist(
        self, source: Source, normalized: NormalizedJob, company_id: UUID | None
    ) -> Job:
        job = Job(
            id=uuid7(),
            source_id=source.id,
            company_id=company_id,
            external_id=normalized.external_id,
            url=normalized.url,
            title=normalized.title,
            normalized_title=normalized.normalized_title,
            seniority=normalized.seniority,
            location_raw=normalized.location_raw,
            location_city=normalized.location_city,
            location_state=normalized.location_state,
            location_country=normalized.location_country,
            is_remote=normalized.is_remote,
            is_hybrid=normalized.is_hybrid,
            employment_type=normalized.employment_type,
            salary_min=normalized.salary_min,
            salary_max=normalized.salary_max,
            salary_currency=normalized.salary_currency,
            salary_period=normalized.salary_period,
            description_raw=normalized.description_raw,
            description_hash=normalized.description_hash,
            # A1: read what the employer asked for, straight from the text.
            #
            # Deterministic and LLM-free by construction - extract_requirements
            # is regex over the already-cleaned description, and every item it
            # returns is a verbatim substring of the posting. Hostile text is
            # therefore stored as a quoted requirement at worst; there is no
            # path by which it becomes an instruction.
            #
            # A posting with no usable description yields an honest empty set
            # (confidence 0, needs_llm_fallback true) rather than failing
            # ingestion - a job we cannot parse is still a job worth keeping.
            requirements=extract_requirements(normalized.description_raw).as_dict(),
            posted_at=normalized.posted_at,
            expires_at=normalized.expires_at,
            status=JobStatus.DISCOVERED,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    # --------------------------------------------------------- batch
    async def ingest_batch(
        self, source_slug: str, postings: list[RawPosting], *, max_age_days: int = 60
    ) -> IngestReport:
        source = await self.sources.get_by_slug(source_slug)
        if source is None:
            raise ValidationError(f"unknown source {source_slug!r}")

        await self.check_policy(source, source.ingestion_method)

        report = IngestReport(source_slug=source_slug)
        for raw in postings:
            report.record(await self.ingest_one(source, raw, max_age_days=max_age_days))

        log.info(
            "ingestion.batch.complete",
            source=source_slug,
            total=report.total,
            created=report.created,
            duplicates=report.duplicates,
            rejected=report.rejected,
        )
        return report
