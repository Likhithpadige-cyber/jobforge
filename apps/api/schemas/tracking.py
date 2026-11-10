"""M19 application tracking request/response bodies."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models.enums import ApplicationOutcome


class TrackingUpdate(BaseModel):
    """Only the fields present are changed; an explicit null clears a date
    or the notes. `extra="forbid"`: status, submitted_at and the approval
    columns are not tracking fields and cannot be smuggled in here."""

    model_config = ConfigDict(extra="forbid")

    outcome: ApplicationOutcome | None = None
    applied_on: date | None = None
    follow_up_on: date | None = None
    notes: str | None = Field(default=None, max_length=4000)


class TrackingRead(BaseModel):
    application_id: UUID
    status: str
    outcome: ApplicationOutcome
    applied_on: date | None
    follow_up_on: date | None
    notes: str | None
    submitted_at: datetime | None
    outcome_updated_at: datetime | None
    #: "jobforge" when the approved pipeline submitted it, "manual" when only
    #: the owner's applied date says it was sent, else null.
    applied_via: Literal["jobforge", "manual"] | None


class FollowUpRead(BaseModel):
    application_id: UUID
    job_title: str
    company_name: str | None
    outcome: ApplicationOutcome
    applied_on: date | None
    applied_via: Literal["jobforge", "manual"] | None
    follow_up_on: date
    days_overdue: int
