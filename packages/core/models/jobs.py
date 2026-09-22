"""Job domain models (DATABASE_DESIGN.md §5)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.core.models.enums import (
    IngestionMethod,
    JobStatus,
    Recommendation,
    SubmissionMethod,
    ingestion_method_enum,
    job_status_enum,
    recommendation_enum,
    submission_method_enum,
)

if TYPE_CHECKING:
    from packages.core.models.applications import Application
    from packages.core.models.identity import User


class SitePolicy(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """The ToS / legality gate (DATABASE_DESIGN.md §5.2, SECURITY.md §7.2).

    Every default is the most restrictive value. The absence of a row means
    blocked: the lookup helper returns a synthetic blocked policy for unknown
    domains rather than raising, so there is no fail-open path.
    """

    __tablename__ = "site_policies"

    domain: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    automation_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    ingestion_method: Mapped[IngestionMethod] = mapped_column(
        ingestion_method_enum, nullable=False, server_default="blocked"
    )
    submission_method: Mapped[SubmissionMethod] = mapped_column(
        submission_method_enum, nullable=False, server_default="human_handoff"
    )
    max_requests_per_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="10"
    )
    robots_respected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    requires_login: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    tos_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None]
    next_review_due: Mapped[date | None]

    reviewer: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint("max_requests_per_hour > 0", name="rate_limit_positive"),
        # Automation may only be enabled by a human who recorded a ToS review.
        CheckConstraint(
            "automation_allowed = false OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="automation_requires_review",
        ),
    )


class Source(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §5.1. Disabled until a policy review passes."""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    ingestion_method: Mapped[IngestionMethod] = mapped_column(
        ingestion_method_enum, nullable=False, server_default="blocked"
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_run_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]

    jobs: Mapped[list[Job]] = relationship(back_populates="source")

    __table_args__ = (Index("ix_sources_domain", "domain"),)


class Company(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §5.3."""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    domain: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    size_band: Mapped[str | None] = mapped_column(Text)
    hq_location: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    blocklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    jobs: Mapped[list[Job]] = relationship(back_populates="company")


class Job(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §5.4."""

    __tablename__ = "jobs"

    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    company_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL")
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str | None] = mapped_column(Text)
    seniority: Mapped[str | None] = mapped_column(String(16))
    location_raw: Mapped[str | None] = mapped_column(Text)
    location_city: Mapped[str | None] = mapped_column(Text)
    location_state: Mapped[str | None] = mapped_column(Text)
    location_country: Mapped[str | None] = mapped_column(Text)
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_hybrid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    employment_type: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(16))
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_hash: Mapped[str | None] = mapped_column(String(64))
    requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    posted_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum, nullable=False, server_default="discovered"
    )
    discard_reason: Mapped[str | None] = mapped_column(Text)
    duplicate_of_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    source: Mapped[Source] = relationship(back_populates="jobs")
    company: Mapped[Company | None] = relationship(back_populates="jobs")
    duplicate_of: Mapped[Job | None] = relationship(remote_side="Job.id")
    evaluations: Mapped[list[JobEvaluation]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[JobRawSnapshot]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    application: Mapped[Application | None] = relationship(back_populates="job", uselist=False)

    __table_args__ = (
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min",
            name="salary_range_ordered",
        ),
        CheckConstraint("duplicate_of_id IS NULL OR duplicate_of_id <> id",
                        name="not_own_duplicate"),
        CheckConstraint(
            "status <> 'duplicate' OR duplicate_of_id IS NOT NULL",
            name="duplicate_has_canonical",
        ),
        Index(
            "uq_jobs_source_external",
            "source_id",
            "external_id",
            unique=True,
            postgresql_where=sa_text("external_id IS NOT NULL"),
        ),
        Index(
            "idx_jobs_status_priority",
            "status",
            sa_text("priority DESC"),
            sa_text("posted_at DESC"),
        ),
        Index("idx_jobs_dedupe", "company_id", "normalized_title", "description_hash"),
        Index(
            "idx_jobs_title_trgm",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ),
    )


class JobRawSnapshot(UUIDPrimaryKeyMixin, Base):
    """Immutable capture of what was actually seen. 90-day content retention."""

    __tablename__ = "job_raw_snapshots"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_job_raw_snapshots_job_id", "job_id"),)


class JobEvaluation(UUIDPrimaryKeyMixin, Base):
    """Immutable fit evaluation (DATABASE_DESIGN.md §5.6).

    No ``updated_at``: re-evaluation creates a new row so the history of what
    was decided, and on which profile version, is never overwritten.
    """

    __tablename__ = "job_evaluations"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    assessment_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="assessed"
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    recommendation: Mapped[Recommendation | None] = mapped_column(recommendation_enum)
    match_matrix: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    matched_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    missing_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    gaps: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    rationale: Mapped[str | None] = mapped_column(Text)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    job: Mapped[Job] = relationship(back_populates="evaluations")

    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        CheckConstraint(
            "(assessment_status = 'assessed' AND score IS NOT NULL "
            "AND recommendation IS NOT NULL) OR "
            "(assessment_status = 'insufficient_evidence' AND score IS NULL "
            "AND recommendation IS NULL)",
            name="assessment_contract",
        ),
        Index("ix_job_evaluations_job_id", "job_id", sa_text("created_at DESC")),
    )
