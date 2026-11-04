"""M13 Phase 1 read models: approvals inbox, submission readiness, history.

Summaries only. None of these carries a recipient address, raw failure text,
packet content, note bodies, snapshots or credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from apps.api.schemas.resumes import ClaimRead
from packages.core.models import ApplicationStatus, ApprovalKind, ApprovalStatus


class ApprovalInboxItem(BaseModel):
    approval_id: UUID
    kind: ApprovalKind
    status: ApprovalStatus
    requested_at: datetime
    decided_at: datetime | None
    application_id: UUID
    application_status: ApplicationStatus
    job_title: str | None
    job_company: str | None


class ReadinessCheck(BaseModel):
    key: str
    passed: bool
    summary: str


class SubmissionGate(BaseModel):
    """Both switches, reported separately so the UI can say which one is off."""

    #: The deployment's configured mode: `dry_run` never sends.
    submission_mode: Literal["dry_run", "enabled"]
    #: The persisted operational switch (`settings.submission_enabled`).
    persisted_enabled: bool
    #: True only when both allow a real send.
    live: bool


class SubmissionReadinessRead(BaseModel):
    application_id: UUID
    application_status: ApplicationStatus
    ready: bool
    #: True only for an owner when every check passes. Reviewers never submit.
    can_submit: bool
    gate: SubmissionGate
    destination_domain: str | None
    attempt: int | None
    checks: list[ReadinessCheck]


SubmissionOutcome = Literal[
    "pending",
    "sent",
    "preflight_blocked",
    "ambiguous",
    "retry_scheduled",
    "failed",
    "handoff_required",
    "blocked_by_policy",
]


class SubmissionAttemptRead(BaseModel):
    submission_id: UUID
    attempt: int
    method: str
    target_domain: str
    result: str
    #: Derived only from structured fields; never from failure text.
    outcome: SubmissionOutcome
    smtp_status: int | None
    started_at: datetime | None
    finished_at: datetime | None
    next_attempt_after: datetime | None


class CoverLetterContentRead(BaseModel):
    """The current cover-letter version as a reviewer sees it: each paragraph
    is a claim with its cited facts and verification verdict."""

    cover_letter_id: UUID
    application_id: UUID
    version: int
    verification_status: str
    created_at: datetime
    claims: list[ClaimRead]
