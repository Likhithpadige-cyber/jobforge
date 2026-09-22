"""Schemas for the job ingestion and browsing endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models import IngestionMethod, JobStatus, Recommendation


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ImportRequest(StrictModel):
    """Import postings from a pasted CSV or JSON payload.

    There is no URL field: Phase 3 fetches nothing. Importing means a human
    already had the content (SECURITY.md §7.3).
    """

    source_slug: Annotated[str, Field(min_length=1, max_length=100)]
    format: Literal["csv", "json"]
    content: Annotated[str, Field(min_length=1, max_length=5_000_000)]
    max_age_days: int = Field(default=60, ge=1, le=365)


class OutcomeRead(BaseModel):
    url: str
    result: str
    job_id: UUID | None = None
    canonical_id: UUID | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ImportResponse(BaseModel):
    source_slug: str
    total: int
    created: int
    duplicates: int
    updated: int
    rejected: int
    outcomes: list[OutcomeRead]


class JobRead(ORMModel):
    id: UUID
    source_id: UUID
    company_id: UUID | None
    external_id: str | None
    url: str
    title: str
    normalized_title: str | None
    seniority: str | None
    location_city: str | None
    location_state: str | None
    location_country: str | None
    is_remote: bool
    is_hybrid: bool
    employment_type: str | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    salary_period: str | None
    status: JobStatus
    discard_reason: str | None
    duplicate_of_id: UUID | None
    priority: int
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime


class JobDetail(JobRead):
    description_raw: str | None
    description_hash: str | None
    requirements: dict[str, Any]


class SourceCreate(StrictModel):
    """Create a source. Disabled and blocked by default, always."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")]
    domain: Annotated[str, Field(min_length=1, max_length=253)]
    ingestion_method: IngestionMethod = IngestionMethod.BLOCKED
    config: dict[str, Any] = Field(default_factory=dict)


class SourceRead(ORMModel):
    id: UUID
    name: str
    slug: str
    domain: str
    ingestion_method: IngestionMethod
    enabled: bool
    last_run_at: datetime | None
    last_success_at: datetime | None


class JobStatusCounts(BaseModel):
    counts: dict[str, int]
    total: int


class EvaluationRead(ORMModel):
    """The latest persisted evaluation for a job.

    Every field is read straight from the `job_evaluations` row A5 wrote.
    Nothing is computed on read and nothing is inferred.

    assessment_status is authoritative. Unresolved results carry neither a
    numeric score nor a recommendation; their evidence and reasons remain visible.
    """

    id: UUID
    job_id: UUID
    profile_version: int
    assessment_status: Literal["assessed", "insufficient_evidence"]
    score: float | None
    recommendation: Recommendation | None
    matched_requirements: dict[str, Any]
    missing_requirements: dict[str, Any]
    match_matrix: dict[str, Any]
    gaps: list[str]
    rationale: str | None
    score_breakdown: dict[str, Any]
    agent_run_id: UUID | None
    model: str | None
    prompt_version: str | None
    created_at: datetime
