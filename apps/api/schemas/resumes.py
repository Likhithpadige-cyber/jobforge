"""Schemas for the Resume Studio read endpoints.

Every field is read from what B6 persisted. Nothing is computed on read except
the verdict counts, which are a deterministic tally of the claim rows returned
in the same response - so the header and the badges can never disagree.

Note what is deliberately absent: no job description, no profile payload, no
fact text. A claim carries its `fact_ids` so the UI can show provenance, and
resolving those ids to fact prose is a separate endpoint that does not exist
yet - this one will not leak it by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from apps.api.schemas.resume_review import ResumeReviewRead
from packages.core.models import ApplicationStatus, ClaimVerdict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ClaimRead(ORMModel):
    """One `resume_claims` row, as the studio needs it."""

    id: UUID
    section: str
    path: str
    claim_text: str
    fact_ids: list[UUID]
    numerals_used: list[str]
    verdict: ClaimVerdict
    verdict_reason: str | None
    verified_at: datetime | None


class VerdictCounts(BaseModel):
    """A tally of the claims in this response.

    Computed from the rows rather than read from `verification_report`, so a
    stale report can never make the UI show a count that contradicts the
    badges beside it.
    """

    supported: int = 0
    overreach: int = 0
    unsupported: int = 0
    not_checked: int = 0
    total: int = 0


class ResumeRead(ORMModel):
    """A persisted resume plus the application context the studio displays."""

    id: UUID
    application_id: UUID | None
    version: int
    kind: str
    verification_status: str
    summary_text: str | None
    content: dict[str, Any]
    created_at: datetime
    model: str | None
    prompt_version: str | None

    # Application context, joined for the header. Identifiers and titles only.
    application_status: ApplicationStatus | None = None
    job_id: UUID | None = None
    job_title: str | None = None
    job_company: str | None = None

    claims: list[ClaimRead] = []
    counts: VerdictCounts = VerdictCounts()

    # C2 review state, so the studio renders the resume and its approval from
    # one response rather than racing two.
    #
    # Null when this resume is not the current tailored version. A review is
    # always about the resume that would actually be submitted, and attaching
    # the live review to a superseded version would invite a decision on text
    # nobody is going to send.
    review: ResumeReviewRead | None = None


class ApplicationRead(ORMModel):
    """Enough of an application to list it and open its resume."""

    id: UUID
    job_id: UUID
    status: ApplicationStatus
    created_at: datetime
    updated_at: datetime
    job_title: str | None = None
    job_company: str | None = None
    resume_count: int = 0
    latest_resume_version: int | None = None
    latest_verification_status: str | None = None
