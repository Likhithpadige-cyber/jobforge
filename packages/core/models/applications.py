"""Application domain models (DATABASE_DESIGN.md §6).

Two constraints in this module carry most of the safety weight:

* ``chk_approved_requires_human`` on ``applications`` - a bug in application
  code cannot produce a submitted application without a recorded human approver.
* ``chk_claim_has_facts`` on ``resume_claims`` - an uncited claim cannot be
  persisted at all.

Both are reinforced by triggers created in the Phase 2 migration; see
docs/DATABASE_IMPLEMENTATION.md for why the CHECKs alone were not enough.
"""

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
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.core.models.enums import (
    AnswerSource,
    ApplicationOutcome,
    ApplicationStatus,
    ApprovalKind,
    ApprovalStatus,
    ClaimVerdict,
    QuestionClass,
    SubmissionMethod,
    SubmissionResult,
    answer_source_enum,
    application_outcome_enum,
    application_status_enum,
    approval_kind_enum,
    approval_status_enum,
    claim_verdict_enum,
    question_class_enum,
    submission_method_enum,
    submission_result_enum,
)

if TYPE_CHECKING:
    from packages.core.models.identity import Profile, User
    from packages.core.models.jobs import Job, SitePolicy

#: Statuses in which a job may still be re-applied to, so the
#: one-live-application-per-job unique index excludes them.
TERMINAL_FAILED_STATUSES = ("abandoned", "error", "policy_blocked")


class Application(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.1."""

    __tablename__ = "applications"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    evaluation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("job_evaluations.id", ondelete="SET NULL")
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        application_status_enum, nullable=False, server_default="created"
    )
    outcome: Mapped[ApplicationOutcome] = mapped_column(
        application_outcome_enum, nullable=False, server_default="pending"
    )
    # use_alter: applications <-> resumes is a genuine cycle, so this FK is
    # added after both tables exist rather than inline.
    resume_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "resumes.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_applications_resume_id_resumes",
        ),
    )
    cover_letter_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "cover_letters.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_applications_cover_letter_id_cover_letters",
        ),
    )
    submission_method: Mapped[SubmissionMethod | None] = mapped_column(submission_method_enum)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    approved_at: Mapped[datetime | None]
    approved_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    submitted_at: Mapped[datetime | None]
    outcome_updated_at: Mapped[datetime | None]
    error_reason: Mapped[str | None] = mapped_column(Text)
    abandon_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # M19 tracking (0011): owner-recorded, never written by agents.
    applied_on: Mapped[date | None]
    follow_up_on: Mapped[date | None]
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    job: Mapped[Job] = relationship(back_populates="application")
    profile: Mapped[Profile] = relationship()
    approver: Mapped[User | None] = relationship(foreign_keys=[approved_by])
    resume: Mapped[Resume | None] = relationship(foreign_keys=[resume_id], post_update=True)
    cover_letter: Mapped[CoverLetter | None] = relationship(
        foreign_keys=[cover_letter_id], post_update=True
    )
    questions: Mapped[list[ApplicationQuestion]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    status_history: Mapped[list[ApplicationStatusHistory]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status NOT IN ('approved','submission_ready','submitting','submitted',"
            "'manual_handoff') "
            "OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_requires_human",
        ),
        CheckConstraint(
            "status <> 'submitted' OR submitted_at IS NOT NULL",
            name="submitted_requires_timestamp",
        ),
        Index(
            "uq_application_per_job",
            "job_id",
            unique=True,
            postgresql_where=sa_text("status NOT IN ('abandoned', 'error', 'policy_blocked')"),
        ),
        Index("idx_applications_status", "status", sa_text("updated_at DESC")),
        Index("idx_applications_outcome", "outcome", sa_text("submitted_at DESC")),
        CheckConstraint(
            "outcome = 'pending' OR applied_on IS NOT NULL",
            name="outcome_requires_applied",
        ),
        Index(
            "idx_applications_follow_up",
            "follow_up_on",
            postgresql_where=sa_text("follow_up_on IS NOT NULL"),
        ),
    )


class Resume(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.2. ``content`` is structured, never a text blob."""

    __tablename__ = "resumes"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text)
    diff_vs_base: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    verification_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)

    claims: Mapped[list[ResumeClaim]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("kind IN ('base', 'tailored')", name="kind_allowed"),
        CheckConstraint(
            "verification_status IN ('pending', 'passed', 'failed')",
            name="verification_status_allowed",
        ),
        CheckConstraint(
            "kind <> 'tailored' OR application_id IS NOT NULL",
            name="tailored_has_application",
        ),
        Index("ix_resumes_application_id", "application_id"),
    )


class ResumeClaim(UUIDPrimaryKeyMixin, Base):
    """Traceability from a rendered sentence back to verified facts.

    ``fact_ids`` is a UUID array, so PostgreSQL cannot enforce referential
    integrity on it directly. A trigger created in the Phase 2 migration does
    that job: every referenced fact must exist and be verified.
    """

    __tablename__ = "resume_claims"

    resume_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
    )
    section: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    fact_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False)
    numerals_used: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    verdict: Mapped[ClaimVerdict] = mapped_column(
        claim_verdict_enum, nullable=False, server_default="not_checked"
    )
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    resume: Mapped[Resume] = relationship(back_populates="claims")

    __table_args__ = (
        CheckConstraint("cardinality(fact_ids) > 0", name="claim_has_facts"),
        Index("idx_claims_resume", "resume_id", "verdict"),
    )


class CoverLetter(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """Mirrors ``resumes`` with the same claim discipline."""

    __tablename__ = "cover_letters"

    profile_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    verification_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)

    claims: Mapped[list[CoverLetterClaim]] = relationship(
        back_populates="cover_letter", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "verification_status IN ('pending', 'passed', 'failed')",
            name="verification_status_allowed",
        ),
        Index("ix_cover_letters_application_id", "application_id"),
    )


class CoverLetterClaim(UUIDPrimaryKeyMixin, Base):
    """Same shape and same rules as ``resume_claims``."""

    __tablename__ = "cover_letter_claims"

    cover_letter_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cover_letters.id", ondelete="CASCADE"), nullable=False
    )
    section: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    fact_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False)
    numerals_used: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    verdict: Mapped[ClaimVerdict] = mapped_column(
        claim_verdict_enum, nullable=False, server_default="not_checked"
    )
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    cover_letter: Mapped[CoverLetter] = relationship(back_populates="claims")

    __table_args__ = (
        CheckConstraint("cardinality(fact_ids) > 0", name="claim_has_facts"),
        Index("idx_cover_letter_claims_letter", "cover_letter_id", "verdict"),
    )


class ApplicationQuestion(UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.4."""

    __tablename__ = "application_questions"

    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    external_field_id: Mapped[str | None] = mapped_column(Text)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_type: Mapped[str] = mapped_column(String(16), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    max_length: Mapped[int | None] = mapped_column(Integer)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    question_class: Mapped[QuestionClass] = mapped_column(question_class_enum, nullable=False)
    class_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    application: Mapped[Application] = relationship(back_populates="questions")
    answer: Mapped[ApplicationAnswer | None] = relationship(
        back_populates="question", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        CheckConstraint(
            "input_type IN ('text','textarea','select','multiselect','boolean',"
            "'file','date','number')",
            name="input_type_allowed",
        ),
        Index("ix_application_questions_application_id", "application_id", "display_order"),
    )


class ApplicationAnswer(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.4.

    ``chk_sensitive_human_only`` plus the question classifier mean a sensitive
    or judgement question cannot hold an agent-authored answer without a
    human's user id attached to it.
    """

    __tablename__ = "application_answers"

    question_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("application_questions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str | None] = mapped_column(Text)
    value_structured: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    source: Mapped[AnswerSource] = mapped_column(answer_source_enum, nullable=False)
    fact_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    requires_human: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    answered_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    edited_from_agent_value: Mapped[str | None] = mapped_column(Text)

    question: Mapped[ApplicationQuestion] = relationship(back_populates="answer")

    __table_args__ = (
        CheckConstraint(
            "NOT (requires_human = true AND source = 'agent' AND answered_by IS NULL)",
            name="sensitive_human_only",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        # M9. Mirrors ck_resume_claims_claim_has_facts, scoped to 'agent'
        # only - a 'derived' answer cites a profile column or preference via
        # value_structured, not a profile_facts row, so requiring fact_ids
        # there would mean fabricating a fact for something that is not one.
        CheckConstraint(
            "source <> 'agent' OR cardinality(fact_ids) > 0",
            name="agent_requires_facts",
        ),
        Index("ix_application_answers_application_id", "application_id"),
    )


class Approval(TimestampMixin, UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.5.

    ``subject_snapshot`` records exactly what the reviewer was shown, so the
    audit trail answers "approved on the basis of what", not just "approved".
    """

    __tablename__ = "approvals"

    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ApprovalKind] = mapped_column(approval_kind_enum, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        approval_status_enum, nullable=False, server_default="pending"
    )
    subject_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    guardrail_flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    decision_note: Mapped[str | None] = mapped_column(Text)
    changes_requested: Mapped[str | None] = mapped_column(Text)
    edits_applied: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )

    application: Mapped[Application] = relationship(back_populates="approvals")
    decider: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'superseded') "
            "OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decided_has_actor",
        ),
        Index(
            "uq_one_pending_approval",
            "application_id",
            "kind",
            unique=True,
            postgresql_where=sa_text("status = 'pending'"),
        ),
        Index(
            "idx_approvals_pending",
            "requested_at",
            postgresql_where=sa_text("status = 'pending'"),
        ),
        Index(
            "ix_approvals_resume_rework_cycles",
            "application_id",
            postgresql_where=sa_text("kind = 'resume' AND status = 'changes_requested'"),
        ),
    )


class Submission(UUIDPrimaryKeyMixin, Base):
    """DATABASE_DESIGN.md §6.6.

    ``blocked_reason`` records CAPTCHA and bot-detection encounters as
    terminal facts. They are metrics to review, never inputs to a workaround.
    """

    __tablename__ = "submissions"

    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[SubmissionMethod] = mapped_column(submission_method_enum, nullable=False)
    target_domain: Mapped[str] = mapped_column(Text, nullable=False)
    policy_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("site_policies.id", ondelete="SET NULL")
    )
    result: Mapped[SubmissionResult] = mapped_column(
        submission_result_enum, nullable=False, server_default="pending"
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    external_reference: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    packet_artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    #: M11 Phase 1 (migration 0010). NULL means no retry is scheduled for
    #: this attempt. Nothing reads or writes this yet - it exists so a
    #: later scheduler poll step (Phase 3, not built) has somewhere durable
    #: to find attempts due for an automatic retry, without a second table.
    next_attempt_after: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    application: Mapped[Application] = relationship(back_populates="submissions")
    policy: Mapped[SitePolicy | None] = relationship()

    __table_args__ = (
        CheckConstraint("attempt > 0", name="attempt_positive"),
        UniqueConstraint("application_id", "attempt", name="uq_submissions_application_id_attempt"),
        Index("ix_submissions_application_id", "application_id"),
    )


class ApplicationStatusHistory(UUIDPrimaryKeyMixin, Base):
    """Append-only. Written by the state machine only; no UPDATE/DELETE grants."""

    __tablename__ = "application_status_history"

    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[ApplicationStatus | None] = mapped_column(application_status_enum)
    to_status: Mapped[ApplicationStatus] = mapped_column(application_status_enum, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    application: Mapped[Application] = relationship(back_populates="status_history")

    __table_args__ = (
        CheckConstraint("actor_type IN ('agent', 'human', 'system')", name="actor_type_allowed"),
        Index(
            "ix_application_status_history_application_id",
            "application_id",
            sa_text("created_at DESC"),
        ),
    )


class Artifact(UUIDPrimaryKeyMixin, Base):
    """File metadata. The bytes live in MinIO/S3, never in PostgreSQL."""

    __tablename__ = "artifacts"

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE")
    )
    resume_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("resumes.id", ondelete="CASCADE")
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    # --- M8 render binding (migration 0008) ------------------------------
    # Nullable here because this table also holds evidence uploads and handoff
    # packets, which have no resume, no approval and no template. The CHECK
    # below narrows all three to NOT NULL for the two rendered-resume kinds,
    # so the contract binds exactly where it applies.
    #
    # `resume_version` is denormalised from the resume on purpose. `resume_id`
    # gives the version transitively today, but an artifact must be able to
    # say which version it rendered without depending on the resume row still
    # holding that value.
    resume_version: Mapped[int | None] = mapped_column(Integer)
    approval_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("approvals.id", ondelete="SET NULL")
    )
    template_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "kind IN ('resume_pdf','resume_docx','cover_letter_pdf','handoff_packet','evidence')",
            name="kind_allowed",
        ),
        CheckConstraint(
            "kind NOT IN ('resume_pdf', 'resume_docx') OR ("
            "resume_id IS NOT NULL AND resume_version IS NOT NULL "
            "AND approval_id IS NOT NULL AND template_version IS NOT NULL "
            "AND content_type IS NOT NULL AND size_bytes IS NOT NULL "
            "AND sha256 IS NOT NULL)",
            name="render_binding",
        ),
        UniqueConstraint("bucket", "storage_key", name="uq_artifacts_bucket_storage_key"),
        Index(
            "uq_artifacts_resume_render",
            "resume_id",
            "kind",
            "template_version",
            unique=True,
            postgresql_where=sa_text("resume_id IS NOT NULL AND template_version IS NOT NULL"),
        ),
    )
