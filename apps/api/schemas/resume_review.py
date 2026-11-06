"""Schemas for the C2 resume-review endpoints.

**Note what the request bodies do not contain.** There is no `reviewer_id`, no
`actor_user_id`, no `decided_by` and no `on_behalf_of`. The deciding human comes
from the server-side session and from nowhere else, so a field for naming a
reviewer would be an impersonation hole with a friendly name. `extra="forbid"`
makes an attempt to add one a 422 rather than a silently ignored field.

The response carries three `can_*` booleans. They are computed by the same
service the endpoints use, so the buttons the studio enables and the actions the
API will accept cannot disagree.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models import ApplicationStatus, ApprovalStatus

if TYPE_CHECKING:
    from packages.core.services.resume_review import ReviewState


class ReviewRequest(BaseModel):
    """Body for opening review. Deliberately empty of identity."""

    model_config = ConfigDict(extra="forbid")


class ApproveRequest(BaseModel):
    """Approve, optionally with a note explaining the decision."""

    model_config = ConfigDict(extra="forbid")

    decision_note: str | None = Field(default=None, max_length=4000)
    export_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RequestChangesRequest(BaseModel):
    """Request changes to the approval displayed to the human.

    The approval ID binds retries to that review, never to a newer review.
    The note is the point, so it is required.

    `min_length=1` catches the empty string at the edge; the service strips
    whitespace and refuses a note that is blank once trimmed, so a body of
    `{"note": "   "}` is rejected too rather than stored as instructions.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: UUID
    note: str = Field(min_length=1, max_length=4000)


class ResumeReviewRead(BaseModel):
    """The review state of one application's current resume.

    Identifiers, the decision and its note. `decided_by_email` is the only
    user-derived field, and it is the same one `GET /auth/me` already returns.
    """

    application_id: UUID
    application_status: ApplicationStatus
    resume_id: UUID
    resume_version: int

    #: not_started | in_review | approved | changes_requested
    review_status: str

    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    #: Whether the approval describes the resume currently being displayed.
    approval_is_current: bool = False

    decision_note: str | None = None
    changes_requested: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    decided_by_email: str | None = None

    can_start_review: bool = False
    can_approve: bool = False
    can_request_changes: bool = False
    #: Why no action is offered, in a sentence a reviewer can act on.
    blocked_reason: str | None = None

    @classmethod
    def from_state(cls, state: ReviewState) -> ResumeReviewRead:
        """Map the service's state object onto the wire schema.

        Written out field by field rather than by `**vars(state)`, so a field
        added to the internal state does not start being served to clients
        merely by existing. One mapper, used by every endpoint that returns
        review state, so the review shape cannot drift between them.
        """
        return cls(
            application_id=state.application_id,
            application_status=state.application_status,
            resume_id=state.resume_id,
            resume_version=state.resume_version,
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
            can_start_review=state.can_start_review,
            can_approve=state.can_approve,
            can_request_changes=state.can_request_changes,
            blocked_reason=state.blocked_reason,
        )
