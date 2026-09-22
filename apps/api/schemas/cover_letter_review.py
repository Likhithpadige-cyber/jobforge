"""Schemas for the M10 Phase 1 cover-letter review endpoints.

Mirrors `apps/api/schemas/resume_review.py`'s shape and the same discipline
it documents: no `reviewer_id`, no `actor_user_id`, no `decided_by` in any
request body - the deciding human comes only from the server-side session.
There is no "open review" request body here (unlike resume review): a cover
letter has no owner-supplied input to wait on, so generation - and opening
its approval - happens automatically once the draft passes verification, not
from a human action this API exposes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models import ApplicationStatus, ApprovalStatus

if TYPE_CHECKING:
    from packages.core.services.cover_letters import CoverLetterReviewState


class ApproveRequest(BaseModel):
    """Approve, optionally with a note explaining the decision."""

    model_config = ConfigDict(extra="forbid")

    decision_note: str | None = Field(default=None, max_length=4000)


class RequestChangesRequest(BaseModel):
    """Request changes to the cover-letter version under review.

    The note is the point, so it is required - `min_length=1` catches the
    empty string, and the service strips whitespace and refuses a note that
    is blank once trimmed.
    """

    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=4000)


class CoverLetterReviewRead(BaseModel):
    """The review state of one application's current cover-letter version."""

    application_id: UUID
    application_status: ApplicationStatus

    cover_letter_id: UUID | None = None
    cover_letter_version: int | None = None
    verification_status: str | None = None

    #: not_started | in_review | approved | changes_requested | failed_verification
    review_status: str

    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    #: Whether the approval describes the cover letter currently being displayed.
    approval_is_current: bool = False

    decision_note: str | None = None
    changes_requested: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    decided_by_email: str | None = None

    can_approve: bool = False
    can_request_changes: bool = False
    #: Why no action is offered, in a sentence a reviewer can act on.
    blocked_reason: str | None = None

    @classmethod
    def from_state(cls, state: CoverLetterReviewState) -> CoverLetterReviewRead:
        """Map the service's state object onto the wire schema, field by field."""
        return cls(
            application_id=state.application_id,
            application_status=state.application_status,
            cover_letter_id=state.cover_letter_id,
            cover_letter_version=state.cover_letter_version,
            verification_status=state.verification_status,
            review_status=state.review_status,
            approval_id=state.approval_id,
            approval_status=state.approval_status,
            approval_is_current=state.approval_is_current,
            decision_note=state.decision_note,
            changes_requested=state.changes_requested,
            requested_at=state.requested_at,
            decided_at=state.decided_at,
            decided_by=state.decided_by,
            decided_by_email=state.decided_by_email,
            can_approve=state.can_approve,
            can_request_changes=state.can_request_changes,
            blocked_reason=state.blocked_reason,
        )
