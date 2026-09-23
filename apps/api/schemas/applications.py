"""Schemas for the M9 application-preparation endpoints.

**Note what the request bodies do not contain.** No `answered_by`, no
`source`, no `fact_ids`, no `actor_user_id`. Provenance is never something a
caller may assert about their own request - `edit_answer` always sets
`source = 'human'` and `answered_by` from the server-side session, the same
discipline `apps/api/schemas/resume_review.py` documents for the decider
identity on every C2 endpoint. `extra="forbid"` turns an attempt to add one
into a 422 rather than a silently ignored field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models.enums import AnswerSource, ApprovalStatus, QuestionClass

if TYPE_CHECKING:
    from packages.core.models import ApplicationAnswer, ApplicationQuestion, Approval


class PrepareQuestionInput(BaseModel):
    """One owner-supplied question definition (AGENT_DESIGN.md §4.1's
    owner-supplied-capture sourcing method - M9 does not scrape a live ATS
    form)."""

    model_config = ConfigDict(extra="forbid")

    question_text: str = Field(min_length=1, max_length=2000)
    input_type: str = Field(
        pattern=r"^(text|textarea|select|multiselect|boolean|file|date|number)$"
    )
    external_field_id: str | None = Field(default=None, max_length=200)
    options: dict[str, Any] = Field(default_factory=dict)
    is_required: bool = False
    max_length: int | None = Field(default=None, ge=1)


class PrepareRequest(BaseModel):
    """Body for `POST /applications/{id}/prepare`.

    An empty list is a legitimate request: a job whose external form has no
    custom fields still needs the employment-type preference gate evaluated
    and the state machine's `questions_pending` step recorded.
    """

    model_config = ConfigDict(extra="forbid")

    questions: list[PrepareQuestionInput] = Field(default_factory=list, max_length=200)


class PrepareResponse(BaseModel):
    application_id: UUID
    event_id: UUID


class AnswerRead(BaseModel):
    id: UUID
    question_id: UUID
    value: str | None
    value_structured: dict[str, Any]
    source: AnswerSource
    fact_ids: list[UUID]
    confidence: str | None
    requires_human: bool
    answered_by: UUID | None
    edited_from_agent_value: str | None

    @classmethod
    def from_model(cls, answer: ApplicationAnswer) -> AnswerRead:
        return cls(
            id=answer.id,
            question_id=answer.question_id,
            value=answer.value,
            value_structured=answer.value_structured,
            source=answer.source,
            fact_ids=list(answer.fact_ids),
            confidence=str(answer.confidence) if answer.confidence is not None else None,
            requires_human=answer.requires_human,
            answered_by=answer.answered_by,
            edited_from_agent_value=answer.edited_from_agent_value,
        )


class QuestionRead(BaseModel):
    """One question and its current answer, if drafted.

    Never a storage key, a credential or anything beyond what
    `ApplicationQuestion`/`ApplicationAnswer` themselves hold - the same
    "metadata only, nothing an artifact row wouldn't already show" discipline
    `apps/api/schemas/artifacts.py` documents.
    """

    id: UUID
    question_text: str
    input_type: str
    options: dict[str, Any]
    is_required: bool
    max_length: int | None
    display_order: int
    question_class: QuestionClass
    class_reason: str | None
    answer: AnswerRead | None

    @classmethod
    def from_model(cls, question: ApplicationQuestion) -> QuestionRead:
        return cls(
            id=question.id,
            question_text=question.question_text,
            input_type=question.input_type,
            options=question.options,
            is_required=question.is_required,
            max_length=question.max_length,
            display_order=question.display_order,
            question_class=question.question_class,
            class_reason=question.class_reason,
            answer=AnswerRead.from_model(question.answer) if question.answer else None,
        )


class EditAnswerRequest(BaseModel):
    """A human sets or overwrites one answer's value.

    No `source` field: `edit_answer` always records `human`, from the
    session, never from this body.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(default=None, max_length=4000)
    value_structured: dict[str, Any] | None = None


class PackageApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_note: str | None = Field(default=None, max_length=4000)


class PackageRequestChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=4000)


class ApprovalRead(BaseModel):
    approval_id: UUID
    application_id: UUID
    status: ApprovalStatus
    decided_by: UUID | None
    decided_at: str | None
    decision_note: str | None
    changes_requested: str | None

    @classmethod
    def from_model(cls, approval: Approval) -> ApprovalRead:
        return cls(
            approval_id=approval.id,
            application_id=approval.application_id,
            status=approval.status,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at.isoformat() if approval.decided_at else None,
            decision_note=approval.decision_note,
            changes_requested=approval.changes_requested,
        )


class SubmitRequest(BaseModel):
    """Submit request payload.

    The destination email cannot be provided or overridden by the caller;
    it is derived solely from authoritative persisted job data.
    Any supplied body fields are forbidden and rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")


class SubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: UUID
    submission_id: UUID
    status: str
    attempt: int
